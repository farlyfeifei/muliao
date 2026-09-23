from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.context import (
    ActionContext,
    AppContext,
    ElementSummary,
    MAX_ELEMENTS,
    MAX_ELEMENT_TEXT,
    MAX_STATE_CHARS,
    MAX_TRANSCRIPT_CHARS,
    VoiceContext,
    build_jev_state,
    serialize_jev_state,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class StateBuilderTests(unittest.TestCase):
    def test_trims_transcript_element_count_labels_and_total_state(self):
        elements = [
            ElementSummary(f"e{index:03d}", "button", "控件" + ("甲" * 200))
            for index in range(150)
        ]
        candidates = {f"c{index:03d}": "候选" + ("乙" * 400) for index in range(100)}
        candidates.update({f"huge{index:03d}": "丙" * 400 for index in range(100)})
        state = build_jev_state(
            "说" * 1_000,
            foreground_app={"name": "notepad.exe", "window_title": "标题" * 100},
            elements=elements,
            candidates=candidates,
            now=100.0,
        )
        encoded = serialize_jev_state(state)

        self.assertEqual(len(state["utterance"]), MAX_TRANSCRIPT_CHARS)
        self.assertLessEqual(len(state["elements"]), MAX_ELEMENTS)
        self.assertTrue(state["elements"])
        rendered_label = state["elements"][0].split('"', 1)[1].rsplit('"', 1)[0]
        self.assertLessEqual(len(rendered_label), MAX_ELEMENT_TEXT)
        self.assertLessEqual(len(encoded), MAX_STATE_CHARS)

    def test_filters_password_and_sensitive_labels_and_forbidden_sources(self):
        state = build_jev_state(
            "点保存",
            elements=[
                {"id": "e01", "role": "button", "name": "保存"},
                {"id": "e02", "role": "password box", "name": "登录凭据"},
                {"id": "e03", "role": "edit", "name": "API token"},
                {"id": "e04", "role": "button", "name": "cookie 设置"},
                {"id": "e05", "role": "link", "name": "昨天页面", "source": "browser_history"},
                {"id": "e06", "role": "link", "name": "收藏页", "source": "bookmarks"},
                {"id": "e07", "role": "document", "name": "正文", "source": "file_content"},
                {"id": "e08", "role": "edit", "name": "普通输入", "is_password": True},
            ],
            pending_confirmation={
                "action": "delete",
                "history": ["private"],
                "file_content": "正文内容",
            },
            now=100.0,
        )
        encoded = serialize_jev_state(state).lower()

        self.assertEqual(state["elements"], ['e01 button "保存"'])
        self.assertEqual(state["pending_confirmation"], {"action": "delete"})
        for forbidden in ("api token", "cookie 设置", "private", "正文内容", "收藏页", "昨天页面"):
            self.assertNotIn(forbidden.lower(), encoded)

    def test_serialization_is_stable_and_key_order_independent(self):
        state_a = build_jev_state(
            "打开它",
            candidates={"c2": "二", "c1": "一"},
            foreground_app={"window_title": "记事本", "name": "notepad.exe"},
            now=100.0,
        )
        state_b = build_jev_state(
            "打开它",
            candidates={"c1": "一", "c2": "二"},
            foreground_app={"name": "notepad.exe", "window_title": "记事本"},
            now=100.0,
        )
        self.assertEqual(serialize_jev_state(state_a), serialize_jev_state(state_b))
        self.assertEqual(json.loads(serialize_jev_state(state_a)), state_a)


class VoiceContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.context = VoiceContext(ttl_seconds=10, confirmation_ttl_seconds=3, clock=self.clock)

    def test_keeps_previous_app_and_last_action_target_for_reference(self):
        self.context.set_foreground_app(AppContext("browser.exe", "搜索结果"))
        self.context.set_foreground_app(AppContext("notepad.exe", "新建 文本文档"))
        self.context.record_success(said="打开记事本", action="open_app:notepad", target="notepad")
        self.clock.advance(2)

        state = self.context.build_state("在里面新建一个")

        self.assertEqual(state["foreground_app"]["name"], "notepad.exe")
        self.assertEqual(state["context"]["previous"]["app"], "browser.exe")
        self.assertEqual(state["context"]["last_target"], "notepad")
        self.assertEqual(state["context"]["recent_actions"][-1]["target"], "notepad")
        self.assertEqual(state["context"]["recent_actions"][-1]["seconds_ago"], 2)

    def test_records_success_failure_and_only_latest_three_actions(self):
        for index in range(5):
            if index == 3:
                self.context.record_failure(
                    said=f"命令{index}",
                    action="click",
                    target=f"e{index}",
                    detail="not found",
                )
            else:
                self.context.record_success(
                    said=f"命令{index}",
                    action="click",
                    target=f"e{index}",
                )
            self.clock.advance(1)

        actions = self.context.build_state("不是那个，换一个")["context"]["recent_actions"]

        self.assertEqual([item["said"] for item in actions], ["命令2", "命令3", "命令4"])
        self.assertEqual(actions[1]["outcome"], "failed")
        self.assertEqual(actions[-1]["target"], "e4")

    def test_ttl_purge_clear_and_confirmation_ttl(self):
        self.context.set_foreground_app({"name": "browser.exe", "title": "页面"})
        self.context.set_elements([{"id": "e01", "role": "button", "name": "确认"}])
        self.context.record_success(said="打开页面", action="open_url", target="https://example.test")
        self.context.set_pending_confirmation({"action": "close_window", "target": "browser.exe"})

        self.clock.advance(3)
        self.assertTrue(self.context.purge())
        state = self.context.build_state("继续")
        self.assertIsNone(state["pending_confirmation"])
        self.assertIsNotNone(state["foreground_app"])
        self.assertTrue(state["elements"])
        self.assertTrue(state["context"]["recent_actions"])

        self.clock.advance(7)
        self.context.purge()
        expired = self.context.build_state("继续")
        self.assertIsNone(expired["foreground_app"])
        self.assertIsNone(expired["context"]["previous"])
        self.assertEqual(expired["elements"], [])
        self.assertEqual(expired["context"]["recent_actions"], [])
        self.assertIsNone(expired["context"]["last_target"])

        self.context.set_foreground_app({"name": "notepad.exe"})
        self.context.record_success(said="打开", action="open_app", target="notepad")
        self.context.clear()
        cleared = self.context.build_state("继续")
        self.assertIsNone(cleared["foreground_app"])
        self.assertEqual(cleared["context"]["recent_actions"], [])

    def test_thread_safe_recording_never_exceeds_three_actions(self):
        barrier = threading.Barrier(9)

        def worker(index: int) -> None:
            barrier.wait()
            self.context.record_action(
                said=f"命令{index}",
                action="click",
                target=f"e{index}",
                ok=index % 2 == 0,
            )

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

        snapshot = self.context.snapshot()
        self.assertEqual(len(snapshot.actions), 3)
        self.assertIn(snapshot.last_target, {f"e{index}" for index in range(8)})

    def test_pure_builder_preserves_explicit_previous_and_last_target(self):
        actions = (
            ActionContext("打开浏览器", "open_app", "browser", "ok", at=95.0),
            ActionContext("点第一个结果", "click", "e01", "failed", at=99.0),
        )
        state = build_jev_state(
            "不是这个，换另一个",
            foreground_app=AppContext("browser.exe", "结果页"),
            previous_app=AppContext("notepad.exe", "笔记"),
            recent_actions=actions,
            now=100.0,
        )
        self.assertEqual(state["context"]["previous"]["app"], "notepad.exe")
        self.assertEqual(state["context"]["last_target"], "e01")
        self.assertEqual(state["context"]["recent_actions"][-1]["outcome"], "failed")


if __name__ == "__main__":
    unittest.main()
