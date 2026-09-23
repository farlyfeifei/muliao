from __future__ import annotations

from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.contracts import RouteDecision
from voice.fast_actions import DryRunFastAdapter, FastActionExecutor, WindowsFastAdapter
from voice.spans import SpanExtractor, select_text_span


class SpanSelectionTests(unittest.TestCase):
    def test_selects_chinese_and_english_quoted_text_verbatim(self):
        chinese = '输入“你好，Claude！保留  两个空格”然后截图'
        span = select_text_span(chinese, "type_text")
        self.assertIsNotNone(span)
        self.assertEqual(span.text, "你好，Claude！保留  两个空格")
        self.assertEqual(chinese[span.start : span.end], span.text)
        self.assertTrue(span.quoted)

        english = 'type "Hello, 世界 -- AS-IS" then take a screenshot'
        span = SpanExtractor().extract(english, "type_text")
        self.assertIsNotNone(span)
        self.assertEqual(span.text, "Hello, 世界 -- AS-IS")
        self.assertEqual(english[span.start : span.end], span.text)

    def test_search_span_stops_before_compound_tail(self):
        commands = (
            "搜索 Python SendInput Unicode，然后打开记事本",
            "搜索 Python SendInput Unicode，并打开记事本",
            "搜索 Python SendInput Unicode，再打开记事本",
            "搜索 Python SendInput Unicode，打开记事本",
        )
        for command in commands:
            with self.subTest(command=command):
                span = select_text_span(command, "search")
                self.assertIsNotNone(span)
                self.assertEqual(span.text, "Python SendInput Unicode")
                self.assertEqual(command[span.start : span.end], span.text)

    def test_type_span_stops_before_english_compound_tail(self):
        command = "type 原文 MixedCase then open https://example.com/docs?q=1"
        span = select_text_span(command, "type_text")
        self.assertIsNotNone(span)
        self.assertEqual(span.text, "原文 MixedCase")

    def test_open_url_selects_exact_url_and_not_next_command(self):
        command = "打开 https://Example.com/a/%E4%B8%AD?q=A%2BB#原文，然后截图"
        span = select_text_span(command, "open_url")
        self.assertIsNotNone(span)
        self.assertEqual(span.text, "https://Example.com/a/%E4%B8%AD?q=A%2BB#原文")
        self.assertEqual(command[span.start : span.end], span.text)

    def test_quoted_connector_is_data_not_a_compound_boundary(self):
        command = '输入“先写 A，然后打开 B”然后截图'
        span = select_text_span(command, "type_text")
        self.assertIsNotNone(span)
        self.assertEqual(span.text, "先写 A，然后打开 B")

    def test_no_generation_or_fallback_for_missing_span(self):
        self.assertIsNone(select_text_span("搜索，然后打开记事本", "search"))
        self.assertIsNone(select_text_span("打开记事本", "open_url"))
        self.assertIsNone(select_text_span("随便说一句", "type_text"))


class RecordingAdapter(DryRunFastAdapter):
    pass


class FastActionExecutorTests(unittest.TestCase):
    def setUp(self):
        self.adapter = RecordingAdapter()
        self.executor = FastActionExecutor(self.adapter)

    def test_default_executor_is_dry_run_and_has_zero_system_side_effects(self):
        executor = FastActionExecutor()
        self.assertIsInstance(executor.adapter, DryRunFastAdapter)
        result = executor.execute(RouteDecision(True, "open_app", "notepad", 0.99))
        self.assertTrue(result.ok)
        self.assertEqual(executor.adapter.calls, [("open_app", "notepad")])

    def test_open_app_allowlist_accepts_only_named_targets(self):
        for app in ("notepad", "browser", "explorer", "settings"):
            with self.subTest(app=app):
                result = self.executor.execute(RouteDecision(True, "open_app", app, 0.99))
                self.assertTrue(result.ok)
        calls_before = list(self.adapter.calls)
        blocked = self.executor.execute(RouteDecision(True, "open_app", "cmd.exe /c calc", 0.99))
        self.assertFalse(blocked.ok)
        self.assertIn("not allowlisted", blocked.detail)
        self.assertEqual(self.adapter.calls, calls_before)

    def test_open_url_allows_http_https_and_rejects_scheme_injection(self):
        ok = self.executor.execute(
            RouteDecision(True, "open_url", "ignored", 0.99, raw={"url": "https://例子.测试/a?q=原文"})
        )
        self.assertTrue(ok.ok)
        self.assertEqual(self.adapter.calls[-1], ("open_url", "https://例子.测试/a?q=原文"))

        calls_before = list(self.adapter.calls)
        for bad in (
            "file:///C:/Windows/System32/calc.exe",
            "javascript:alert(1)",
            "https://example.com\nfile:///etc/passwd",
            "http://user:pass@example.com/",
            "http://localhost/admin",
        ):
            with self.subTest(url=bad):
                result = self.executor.execute(
                    RouteDecision(True, "open_url", "ignored", 0.99, raw={"url": bad})
                )
                self.assertFalse(result.ok)
        self.assertEqual(self.adapter.calls, calls_before)

    def test_search_preserves_query_and_encodes_it_as_data(self):
        query = "幕僚 SendInput & x=1 #原文"
        result = self.executor.execute(
            RouteDecision(True, "search", "web", 0.99, raw={"query": query})
        )
        self.assertTrue(result.ok)
        opened = self.adapter.calls[-1][1]
        self.assertEqual(parse_qs(urlsplit(opened).query)["q"], [query])
        self.assertNotIn("& x=1", opened)

    def test_volume_mute_media_shortcut_screenshot_and_chinese_type(self):
        decisions = [
            RouteDecision(True, "volume", "up", 0.99, raw={"steps": 3}),
            RouteDecision(True, "mute", "toggle", 0.99),
            RouteDecision(True, "media", "next", 0.99),
            RouteDecision(True, "shortcut", "copy", 0.99),
            RouteDecision(True, "screenshot", "screen", 0.99),
            RouteDecision(True, "type_text", "ignored", 0.99, raw={"text": "中文输入，原样保留。"}),
        ]
        self.assertTrue(all(self.executor.execute(decision).ok for decision in decisions))
        self.assertEqual(
            self.adapter.calls,
            [
                ("volume", "up", 3),
                ("mute", None),
                ("media", "next"),
                ("shortcut", ("ctrl", "c")),
                ("screenshot",),
                ("type_text", "中文输入，原样保留。"),
            ],
        )

    def test_rejects_unaccepted_destructive_incomplete_or_unknown_actions(self):
        decisions = [
            RouteDecision(False, "open_app", "notepad", 0.99),
            RouteDecision(True, "open_app", "notepad", 0.99, destructive=True),
            RouteDecision(True, "open_app", "notepad", 0.99, complete=False),
            RouteDecision(True, "shell", "rm -rf /", 0.99),
            RouteDecision(True, "shortcut", "win+r", 0.99),
            RouteDecision(True, "media", "eject", 0.99),
            RouteDecision(True, "volume", "up", 0.99, raw={"steps": 999}),
        ]
        for decision in decisions:
            with self.subTest(decision=decision):
                self.assertFalse(self.executor.execute(decision).ok)
        self.assertEqual(self.adapter.calls, [])

    def test_rejects_injection_control_characters_and_oversized_text(self):
        malicious = self.executor.execute(
            RouteDecision(True, "type_text", "ignored", 0.99, raw={"text": "hello\x00world"})
        )
        oversized = self.executor.execute(
            RouteDecision(True, "type_text", "ignored", 0.99, raw={"text": "中" * 4097})
        )
        oversized_query = self.executor.execute(
            RouteDecision(True, "search", "web", 0.99, raw={"query": "q" * 1025})
        )
        self.assertFalse(malicious.ok)
        self.assertFalse(oversized.ok)
        self.assertFalse(oversized_query.ok)
        self.assertEqual(self.adapter.calls, [])

    def test_type_text_passes_data_to_unicode_interface_not_shortcut(self):
        text = "中文 ^%+() {ENTER} 保持文本"
        result = self.executor.execute(
            RouteDecision(True, "type_text", "ignored", 0.99, raw={"text": text})
        )
        self.assertTrue(result.ok)
        self.assertEqual(self.adapter.calls, [("type_text", text)])

    def test_adapter_exception_is_reported_without_retry(self):
        class FailingAdapter(RecordingAdapter):
            def screenshot(self):
                self.calls.append(("screenshot",))
                raise OSError("test failure")

        adapter = FailingAdapter()
        result = FastActionExecutor(adapter).execute(
            RouteDecision(True, "screenshot", "screen", 0.99)
        )
        self.assertFalse(result.ok)
        self.assertIn("OSError", result.detail)
        self.assertEqual(adapter.calls, [("screenshot",)])


class WindowsUnicodeAdapterTests(unittest.TestCase):
    def test_unicode_typing_uses_sendinput_utf16_events(self):
        sent = []

        def send_input(events, count, size):
            sent.append((events, count, size))
            return count

        adapter = WindowsFastAdapter(send_input=send_input)
        original = adapter._ensure_windows
        adapter._ensure_windows = lambda: None
        try:
            adapter.type_unicode("中A😀")
        finally:
            adapter._ensure_windows = original
        # 中 and A are one UTF-16 unit each; 😀 is a surrogate pair. Each unit
        # has a key-down and key-up event.
        self.assertEqual(sent[0][1], 8)
        self.assertGreater(sent[0][2], 0)


if __name__ == "__main__":
    unittest.main()
