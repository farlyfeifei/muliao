from __future__ import annotations

import asyncio
import json
import pathlib
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

import httpx
import uvicorn


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server  # noqa: E402
import machine_control  # noqa: E402

# 模块加载时捕获真实 httpx.AsyncClient；测试 client 用它，server 内部的被 patch 成假上游。
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sse(obj) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False)


def _tool_round_lines():
    """第一轮上游：请求调用 close_window（控制类工具）。"""
    return [
        _sse({"choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "id": "call_c1", "type": "function",
            "function": {"name": "close_window", "arguments": json.dumps({"title": "记事本"})},
        }]}, "finish_reason": "tool_calls"}]}),
        "data: [DONE]",
    ]


def _text_round_lines():
    """第二轮上游：普通文本收尾。"""
    return [
        _sse({"choices": [{"index": 0, "delta": {"content": "已处理"}, "finish_reason": "stop"}]}),
        "data: [DONE]",
    ]


class _FakeUpstreamResponse:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class _FakeStreamCM:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeUpstreamClient:
    """替身：server 内部 `httpx.AsyncClient(timeout=...)`。第 1 次 stream 给工具轮，之后给文本轮。"""

    def __init__(self, *args, **kwargs):
        self._round = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, json=None, headers=None):
        self._round += 1
        lines = _tool_round_lines() if self._round == 1 else _text_round_lines()
        return _FakeStreamCM(_FakeUpstreamResponse(lines))


class _FakeBackend:
    """替身 machine_control backend：记录 close 是否真的被调用。"""

    def __init__(self):
        self.closed = []
        self.activated = []

    def enum_windows(self, limit):
        return []

    def resolve_window(self, title, pid):
        return object()  # 非 None 即「找到窗口」

    def activate(self, win):
        self.activated.append(win)

    def close(self, win):
        self.closed.append(win)

    def launch(self, name, args):
        return {"pid": 1, "name": name}

    def click(self, *a):
        pass

    def type_text(self, *a):
        pass

    def send_keys(self, *a):
        pass


def _jev(answers, ok=True, code=None):
    async def _fake(state, questions, timeout=25.0):
        return {"ok": ok, "answers": answers, "code": code, "status": 200 if ok else None,
                "model": "jev-test", "ms": 5, "attempts": 1}
    return _fake


_HIGH_RISK = {
    "risk_score": {"type": "score", "score": 8.0},
    "reversible": {"type": "noul", "noul": 0.0},
    "needs_confirm": {"type": "noul", "noul": 1.0},
    "matches_intent": {"type": "noul", "noul": 1.0},
}
_LOW_RISK = {
    "risk_score": {"type": "score", "score": 1.0},
    "reversible": {"type": "noul", "noul": 1.0},
    "needs_confirm": {"type": "noul", "noul": 0.0},
    "matches_intent": {"type": "noul", "noul": 1.0},
}


def _parse_events(text_lines):
    events = []
    for line in text_lines:
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                pass
    return events


class ChatGateE2ETests(unittest.IsolatedAsyncioTestCase):
    """端到端：/api/chat 真实工具调用链路上的 Jev 门控 + 确认流。

    用真实 uvicorn（独立线程 + 真 TCP 流），因为 httpx 的 ASGITransport 会缓冲整个
    响应体、无法在流中途拿到 tool_confirm 事件去回确认。真实服务器与生产同构：
    确认 POST 与聊天流是两个并发请求，唤醒挂起的工具循环。
    """

    def setUp(self):
        self.backend = _FakeBackend()
        server._pending_confirms.clear()
        server._sessions.clear()
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(server.app, host="127.0.0.1", port=self.port,
                                log_level="critical", access_log=False, lifespan="off")
        self.uvi = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.uvi.run, daemon=True)
        self.thread.start()
        # 等服务器起来
        for _ in range(100):
            if self.uvi.started:
                break
            time.sleep(0.05)
        self.assertTrue(self.uvi.started, "uvicorn did not start")

    def tearDown(self):
        self.uvi.should_exit = True
        self.thread.join(timeout=5)
        server._pending_confirms.clear()
        server._sessions.clear()

    async def _run_chat(self, decision):
        """跑一次 /api/chat，遇到 tool_confirm 时按 decision 确认。返回事件列表。"""
        collected = []
        async with _REAL_ASYNC_CLIENT(base_url=self.base, timeout=30.0) as client:
            async with client.stream(
                "POST", "/api/chat",
                json={"messages": [{"role": "user", "content": "帮我关闭记事本"}],
                      "session_id": "gate-e2e"},
            ) as resp:
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    collected.append(ev)
                    if ev.get("type") == "tool_confirm" and decision is not None:
                        # 与生产同构：另一个并发请求 POST 确认，唤醒挂起的工具循环。
                        await client.post("/api/chat/confirm", json={
                            "confirm_id": ev["confirm_id"], "decision": decision,
                        })
        return collected

    async def test_high_risk_control_confirms_then_executes(self):
        with (
            patch.object(server.httpx, "AsyncClient", _FakeUpstreamClient),
            patch.object(server, "jev_ask", _jev(_HIGH_RISK)),
            patch.object(server.permissions, "is_granted", lambda scope: True),
            patch.object(server.permissions, "granted_scopes", lambda: ["computer_control"]),
            patch.object(machine_control, "_backend", lambda: self.backend),
        ):
            events = await self._run_chat("allow")

        kinds = [e.get("type") for e in events]
        self.assertIn("tool_gate", kinds)
        self.assertIn("tool_confirm", kinds)
        self.assertIn("tool_result", kinds)
        gate = next(e for e in events if e["type"] == "tool_gate")
        self.assertEqual(gate["action"], "confirm")
        self.assertEqual(gate["name"], "close_window")
        result = next(e for e in events if e["type"] == "tool_result")
        self.assertFalse(result["denied"], "用户确认后动作应执行")
        self.assertEqual(len(self.backend.closed), 1, "确认后 close_window 必须真的执行")

    async def test_high_risk_control_denied_does_not_execute(self):
        with (
            patch.object(server.httpx, "AsyncClient", _FakeUpstreamClient),
            patch.object(server, "jev_ask", _jev(_HIGH_RISK)),
            patch.object(server.permissions, "is_granted", lambda scope: True),
            patch.object(server.permissions, "granted_scopes", lambda: ["computer_control"]),
            patch.object(machine_control, "_backend", lambda: self.backend),
        ):
            events = await self._run_chat("deny")

        result = next(e for e in events if e["type"] == "tool_result")
        self.assertTrue(result["denied"], "用户拒绝后动作不得执行")
        self.assertEqual(len(self.backend.closed), 0, "拒绝后 close_window 绝不能执行")

    async def test_control_jev_unavailable_denies_without_confirm(self):
        # Jev 不可用 + 控制类 → fail-closed：直接 deny，不发 tool_confirm，不执行。
        with (
            patch.object(server.httpx, "AsyncClient", _FakeUpstreamClient),
            patch.object(server, "jev_ask", _jev({}, ok=False, code="timeout")),
            patch.object(server.permissions, "is_granted", lambda scope: True),
            patch.object(server.permissions, "granted_scopes", lambda: ["computer_control"]),
            patch.object(machine_control, "_backend", lambda: self.backend),
        ):
            events = await self._run_chat(None)

        kinds = [e.get("type") for e in events]
        self.assertIn("tool_gate", kinds)
        self.assertNotIn("tool_confirm", kinds)
        gate = next(e for e in events if e["type"] == "tool_gate")
        self.assertEqual(gate["action"], "deny")
        self.assertEqual(gate["source"], "fail_closed")
        result = next(e for e in events if e["type"] == "tool_result")
        self.assertTrue(result["denied"])
        self.assertEqual(len(self.backend.closed), 0, "Jev 不可用时控制动作必须被拒绝")

    async def test_confirm_timeout_denies(self):
        # 不确认（decision=None）→ 服务端确认超时 → deny。用极短超时驱动。
        with (
            patch.object(server.httpx, "AsyncClient", _FakeUpstreamClient),
            patch.object(server, "jev_ask", _jev(_HIGH_RISK)),
            patch.object(server.permissions, "is_granted", lambda scope: True),
            patch.object(server.permissions, "granted_scopes", lambda: ["computer_control"]),
            patch.object(machine_control, "_backend", lambda: self.backend),
            patch.object(server, "_CONFIRM_TIMEOUT", 0.3),
        ):
            events = await self._run_chat(None)

        self.assertIn("tool_confirm", [e.get("type") for e in events])
        result = next(e for e in events if e["type"] == "tool_result")
        self.assertTrue(result["denied"], "确认超时必须按拒绝处理")
        self.assertEqual(len(self.backend.closed), 0)


if __name__ == "__main__":
    unittest.main()
