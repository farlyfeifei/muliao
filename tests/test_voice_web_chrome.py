"""voice.web_chrome 真实 CDP 后端测试。

无浏览器的环境自动跳过真机用例；契约/降级/纯逻辑用例始终跑。
真机用例覆盖：起 Edge headless → observe 提取元素 → type/click 真副作用 →
dry-run 零副作用 → stale 目标 → ObservationBuilder 契约兼容 → close 幂等。
"""
from __future__ import annotations

import json
import os
import shutil
import unittest

from voice.web_backend import BrowserBackend
from voice.web_chrome import ChromeDomBackend, ChromeLaunchError, _find_browser
from voice.web_contracts import CommitState, WebErrorCode, WebOperation

_HAS_BROWSER = bool(_find_browser(os.environ.get("MULIAO_TEST_BROWSER", "")))

_TEST_PAGE = """data:text/html,<html><head><title>WebChromeTest</title></head><body>
<h1>Muliao Web Test</h1>
<input id='q' type='text' aria-label='query box'>
<input id='pw' type='password'>
<button id='go' onclick="document.getElementById('out').textContent='CLICKED:'+document.getElementById('q').value">Go</button>
<a id='lk' href='https://example.com'>Example</a>
<div id='out'>none</div>
</body></html>"""


class ProtocolTests(unittest.TestCase):
    def test_implements_browser_backend_protocol(self):
        b = ChromeDomBackend()
        self.assertIsInstance(b, BrowserBackend)

    def test_not_started_act_raises_nothing_but_close_is_safe(self):
        b = ChromeDomBackend()
        b.close()  # 未 start 直接 close 必须安全幂等
        b.close()

    def test_find_browser_explicit_missing_falls_back(self):
        # 显式路径不存在时应回落到系统候选，而不是返回该无效路径
        got = _find_browser(r"C:\definitely\not\here.exe")
        self.assertNotEqual(got, r"C:\definitely\not\here.exe")


@unittest.skipUnless(_HAS_BROWSER, "no Edge/Chrome on this machine")
class RealBrowserTests(unittest.TestCase):
    def setUp(self):
        self.backend = ChromeDomBackend(headless=True, start_url=_TEST_PAGE)
        self.backend.start()

    def tearDown(self):
        self.backend.close()
        self.backend.close()  # close 必须幂等

    def test_observe_extracts_interactive_elements_and_filters_password(self):
        doc = self.backend.observe()
        self.assertEqual(doc.title, "WebChromeTest")
        roles = sorted(e.get("role") for e in doc.raw_elements)
        self.assertIn("textbox", roles)
        self.assertIn("button", roles)
        self.assertIn("link", roles)
        # 密码框绝不暴露
        self.assertFalse(any(e.get("input_type") == "password" for e in doc.raw_elements))
        # backend_id 文档内唯一
        ids = [e.get("backend_id") for e in doc.raw_elements]
        self.assertEqual(len(ids), len(set(ids)))

    def test_type_and_click_have_real_side_effects(self):
        doc = self.backend.observe()
        box = next(e["backend_id"] for e in doc.raw_elements if e.get("role") == "textbox")
        btn = next(e["backend_id"] for e in doc.raw_elements if e.get("role") == "button")

        r1 = self.backend.act(operation=WebOperation.TYPE_TEXT, target_backend_id=box,
                              text="hello muliao", dry_run=False)
        self.assertTrue(r1.ok)
        self.assertEqual(r1.commit_state, CommitState.COMMITTED)

        r2 = self.backend.act(operation=WebOperation.CLICK, target_backend_id=btn, dry_run=False)
        self.assertTrue(r2.ok)
        # 页面副作用：onclick 把输入值写进 #out
        out = self.backend._eval("document.getElementById('out').textContent")
        self.assertEqual(out, "CLICKED:hello muliao")

    def test_dry_run_commits_nothing(self):
        doc = self.backend.observe()
        btn = next(e["backend_id"] for e in doc.raw_elements if e.get("role") == "button")
        r = self.backend.act(operation=WebOperation.CLICK, target_backend_id=btn, dry_run=True)
        self.assertTrue(r.ok)
        self.assertEqual(r.commit_state, CommitState.NOT_COMMITTED)
        out = self.backend._eval("document.getElementById('out').textContent")
        self.assertEqual(out, "none")

    def test_stale_backend_id_returns_target_stale(self):
        r = self.backend.act(operation=WebOperation.CLICK, target_backend_id="b9999", dry_run=False)
        self.assertFalse(r.ok)
        self.assertEqual(r.error_code, WebErrorCode.TARGET_STALE)

    def test_scroll_and_wait_commit(self):
        r1 = self.backend.act(operation=WebOperation.SCROLL_DOWN, dry_run=False)
        self.assertTrue(r1.ok and r1.commit_state == CommitState.COMMITTED)
        r2 = self.backend.act(operation=WebOperation.WAIT, dry_run=False)
        self.assertTrue(r2.ok and r2.commit_state == CommitState.COMMITTED)

    def test_navigate_changes_document_token(self):
        before = self.backend.observe().document_token
        page2 = ("data:text/html,<html><head><title>Second</title></head>"
                 "<body><button>x</button></body></html>")
        r = self.backend.navigate(page2)
        self.assertTrue(r.ok)
        after = self.backend.observe()
        self.assertNotEqual(before, after.document_token)
        self.assertEqual(after.title, "Second")

    def test_observation_builder_contract(self):
        """observe 的 RawDocument 必须能被 ObservationBuilder 消费（契约兼容）。"""
        from voice.web_observation import ObservationBuilder
        doc = self.backend.observe()
        builder = ObservationBuilder(session_id="s", tab_id="t")
        obs, unsupported = builder.build(
            observation_id="o1", origin=doc.origin, title=doc.title, text=doc.text,
            loading_state=doc.loading_state, raw_elements=doc.raw_elements,
            document_token=doc.document_token,
        )
        self.assertGreaterEqual(len(obs.targets), 3)  # textbox+button+link
        # 每个 target 都带 backend_id 元数据（act 往返定位用）
        for t in obs.targets:
            self.assertTrue(str(t.metadata.get("backend_id", "")).startswith("b"))

    def test_targeted_act_without_id_is_invalid_proposal(self):
        r = self.backend.act(operation=WebOperation.CLICK, target_backend_id="", dry_run=False)
        self.assertFalse(r.ok)
        self.assertEqual(r.error_code, WebErrorCode.INVALID_PROPOSAL)

    def test_unsupported_operation_rejected(self):
        doc = self.backend.observe()
        btn = next(e["backend_id"] for e in doc.raw_elements if e.get("role") == "button")
        r = self.backend.act(operation="teleport", target_backend_id=btn, dry_run=False)
        self.assertFalse(r.ok)
        self.assertEqual(r.error_code, WebErrorCode.INVALID_PROPOSAL)


if __name__ == "__main__":
    unittest.main()
