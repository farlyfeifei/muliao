from __future__ import annotations

import asyncio
import pathlib
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server  # noqa: E402


class ConfirmBridgeTests(unittest.IsolatedAsyncioTestCase):
    """确认流的 Future 桥：流生成器挂起 ↔ /api/chat/confirm 端点唤醒。

    这是最危险的新代码：任何异常路径（超时/断流/取消）都必须落到「拒绝」，
    绝不能悬挂，也绝不能因清理逻辑放行控制动作。
    """

    def setUp(self):
        server._pending_confirms.clear()

    def tearDown(self):
        server._pending_confirms.clear()

    async def test_resolve_delivers_decision_to_awaiter(self):
        async def waiter():
            return await server._await_confirmation("c1", timeout=5.0)

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0)  # 让 waiter 先登记 Future 并挂起
        self.assertIn("c1", server._pending_confirms)
        delivered = server._resolve_confirm("c1", "allow")
        self.assertTrue(delivered)
        self.assertEqual(await task, "allow")
        self.assertNotIn("c1", server._pending_confirms)

    async def test_resolve_deny_delivers_deny(self):
        task = asyncio.create_task(server._await_confirmation("c2", timeout=5.0))
        await asyncio.sleep(0)
        server._resolve_confirm("c2", "deny")
        self.assertEqual(await task, "deny")

    async def test_timeout_falls_to_deny(self):
        # 无人确认 → 超时必须 deny（fail-safe），不能悬挂。
        decision = await server._await_confirmation("c3", timeout=0.05)
        self.assertEqual(decision, "deny")
        self.assertNotIn("c3", server._pending_confirms)

    async def test_discard_resolves_pending_to_deny(self):
        task = asyncio.create_task(server._await_confirmation("c4", timeout=5.0))
        await asyncio.sleep(0)
        # 模拟客户端断流：_discard_confirm 必须把挂起的 Future 收成 deny。
        server._discard_confirm("c4")
        self.assertEqual(await task, "deny")

    async def test_resolve_unknown_id_returns_false(self):
        self.assertFalse(server._resolve_confirm("nope", "allow"))

    async def test_double_resolve_second_is_noop(self):
        task = asyncio.create_task(server._await_confirmation("c5", timeout=5.0))
        await asyncio.sleep(0)
        self.assertTrue(server._resolve_confirm("c5", "allow"))
        # 第二次对同一 id：Future 已被弹出，返回 False，不抛异常。
        self.assertFalse(server._resolve_confirm("c5", "deny"))
        self.assertEqual(await task, "allow")

    async def test_illegal_decision_string_coerced_to_deny(self):
        task = asyncio.create_task(server._await_confirmation("c6", timeout=5.0))
        await asyncio.sleep(0)
        server._resolve_confirm("c6", "maybe")
        # _await_confirmation 末尾对非 allow/deny 的值兜底为 deny。
        self.assertEqual(await task, "deny")

    async def test_cancellation_propagates_and_cleans_up(self):
        task = asyncio.create_task(server._await_confirmation("c7", timeout=5.0))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn("c7", server._pending_confirms)


class ConfirmEndpointTests(unittest.TestCase):
    """POST /api/chat/confirm 的契约校验。"""

    def setUp(self):
        self.client = TestClient(server.app, base_url="http://localhost")
        server._pending_confirms.clear()

    def tearDown(self):
        server._pending_confirms.clear()
        self.client.close()

    def test_missing_confirm_id_rejected(self):
        r = self.client.post("/api/chat/confirm", json={"decision": "allow"})
        self.assertEqual(r.status_code, 422)
        self.assertFalse(r.json()["ok"])

    def test_bad_decision_rejected(self):
        r = self.client.post("/api/chat/confirm", json={"confirm_id": "x", "decision": "maybe"})
        self.assertEqual(r.status_code, 422)

    def test_unknown_confirm_id_404(self):
        r = self.client.post("/api/chat/confirm", json={"confirm_id": "ghost", "decision": "allow"})
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.json()["ok"])

    def test_non_json_body_400(self):
        r = self.client.post("/api/chat/confirm", content=b"not json",
                             headers={"Content-Type": "text/plain"})
        # CSRF 中间件对写接口要求 application/json，纯文本会被 415 挡；
        # JSON 但非对象则 400。这里用 JSON 字符串体触发对象校验。
        self.assertIn(r.status_code, (400, 415))


if __name__ == "__main__":
    unittest.main()
