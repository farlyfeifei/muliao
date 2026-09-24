from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.machine_adapter import (
    MachineControlActionExecutor,
    MachineControlError,
    MachineControlFastAdapter,
)
from voice.perception import UIElement


class FakeControl:
    """Records calls and replays scripted ``machine_control`` style dicts."""

    def __init__(self, *, unavailable: bool = False) -> None:
        self.calls: list[tuple] = []
        self.unavailable = unavailable
        self.responses: dict[str, dict] = {}

    def _record(self, name: str, *args) -> dict:
        self.calls.append((name, *args))
        if name in self.responses:
            return self.responses[name]
        if self.unavailable:
            return {"ok": False, "error": "control_unavailable", "hint": "no pywinauto"}
        return {"ok": True}

    def open_application(self, name_or_path, args=None):
        return self._record("open_application", name_or_path, args)

    def type_text(self, text, **kwargs):
        return self._record("type_text", text, kwargs)

    def press_keys(self, keys, **kwargs):
        return self._record("press_keys", keys, kwargs)

    def click_element(self, **kwargs):
        return self._record("click_element", kwargs)


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.control = FakeControl()
        self.adapter = MachineControlFastAdapter(control=self.control)

    def test_open_app_maps_logical_token_to_executable(self):
        self.adapter.open_app("word")
        self.assertEqual(self.control.calls[-1], ("open_application", "winword.exe", None))
        self.adapter.open_app("notepad")
        self.assertEqual(self.control.calls[-1], ("open_application", "notepad.exe", None))
        self.adapter.open_app("excel")
        self.assertEqual(self.control.calls[-1], ("open_application", "excel.exe", None))

    def test_type_unicode_routes_to_control_type_text(self):
        self.adapter.type_unicode("你好世界")
        self.assertEqual(self.control.calls[-1][0], "type_text")
        self.assertEqual(self.control.calls[-1][1], "你好世界")

    def test_empty_type_is_a_noop(self):
        self.adapter.type_unicode("")
        self.assertEqual([c for c in self.control.calls if c[0] == "type_text"], [])

    def test_shortcut_joins_keys_into_a_combo(self):
        self.adapter.shortcut(("ctrl", "s"))
        self.assertEqual(self.control.calls[-1][0], "press_keys")
        self.assertEqual(self.control.calls[-1][1], "ctrl+s")

    def test_browser_and_settings_use_inherited_launch_path(self):
        # These cannot be expressed as a pywinauto start(); they must NOT call
        # machine_control, and instead go through the inherited url_opener.
        opened = []
        adapter = MachineControlFastAdapter(
            control=self.control, url_opener=opened.append, browser_url="https://x.test/"
        )
        adapter.open_app("browser")
        self.assertEqual([c for c in self.control.calls if c[0] == "open_application"], [])
        self.assertEqual(opened, ["https://x.test/"])

    def test_unknown_app_is_rejected(self):
        with self.assertRaises(ValueError):
            self.adapter.open_app("calc.exe /c rm -rf")


class ErrorHandlingTests(unittest.TestCase):
    def test_target_not_found_raises_machine_control_error(self):
        control = FakeControl()
        control.responses["type_text"] = {
            "ok": False, "error": "target_not_found", "hint": "no window"
        }
        adapter = MachineControlFastAdapter(control=control)
        with self.assertRaises(MachineControlError) as raised:
            adapter.type_unicode("x")
        self.assertEqual(raised.exception.code, "target_not_found")

    def test_open_app_failure_raises(self):
        control = FakeControl()
        control.responses["open_application"] = {
            "ok": False, "error": "action_failed", "hint": "launch failed"
        }
        adapter = MachineControlFastAdapter(control=control)
        with self.assertRaises(MachineControlError):
            adapter.open_app("word")


class DegradationTests(unittest.TestCase):
    def test_control_unavailable_falls_back_to_sendinput_type(self):
        sent = []

        def send_input(events, count, size):
            sent.append((events, count, size))
            return count

        control = FakeControl(unavailable=True)
        adapter = MachineControlFastAdapter(control=control, send_input=send_input)
        original = adapter._ensure_windows
        adapter._ensure_windows = lambda: None
        try:
            adapter.type_unicode("中A")
        finally:
            adapter._ensure_windows = original
        # No control success, so it degraded to SendInput (4 UTF-16 events: down/up x2).
        self.assertTrue(sent)
        self.assertEqual(sent[0][1], 4)

    def test_control_unavailable_open_app_falls_back_to_launcher(self):
        launched = []
        control = FakeControl(unavailable=True)
        adapter = MachineControlFastAdapter(
            control=control, launcher=launched.append
        )
        adapter.open_app("notepad")
        self.assertEqual(launched, [("notepad.exe",)])


class GoalExecutorTests(unittest.TestCase):
    def setUp(self):
        self.control = FakeControl()
        self.executor = MachineControlActionExecutor(control=self.control)

    def test_type_action_sends_verbatim_text_and_declares_changed(self):
        element = UIElement(text="评论框", role="EditControl", bounds=(1, 2, 30, 40))
        outcome = self.executor.execute(element, "type_text", "用户原话")
        self.assertTrue(outcome["ok"])
        self.assertTrue(outcome["changed"])
        self.assertEqual(self.control.calls[-1][0], "type_text")
        self.assertEqual(self.control.calls[-1][1], "用户原话")

    def test_click_action_locates_by_name(self):
        element = UIElement(text="保存", role="Button", bounds=(1, 2, 30, 40))
        outcome = self.executor.execute(element, "click")
        self.assertTrue(outcome["ok"])
        self.assertEqual(self.control.calls[-1][0], "click_element")
        self.assertEqual(self.control.calls[-1][1]["element_name"], "保存")

    def test_enter_action_presses_key(self):
        element = UIElement(text="框", role="EditControl", bounds=(1, 2, 30, 40))
        self.executor.execute(element, "enter")
        self.assertEqual(self.control.calls[-1][0], "press_keys")
        self.assertEqual(self.control.calls[-1][1], "enter")

    def test_type_without_text_fails_honestly(self):
        element = UIElement(text="框", role="EditControl", bounds=(1, 2, 30, 40))
        outcome = self.executor.execute(element, "type_text", "")
        self.assertFalse(outcome["ok"])

    def test_control_failure_returns_not_ok(self):
        self.control.responses["click_element"] = {
            "ok": False, "error": "target_not_found", "hint": "gone"
        }
        element = UIElement(text="保存", role="Button", bounds=(1, 2, 30, 40))
        outcome = self.executor.execute(element, "click")
        self.assertFalse(outcome["ok"])
        self.assertFalse(outcome["changed"])


class RuntimeActGateTests(unittest.TestCase):
    @staticmethod
    def settings(**changes):
        from voice.config import VoiceSettings

        base = VoiceSettings(
            sensevoice_dir=Path("C:/models/sensevoice"),
            jev_url="https://example.test/systemone",
            jev_key="",
            jev_model="jev-latest",
        )
        return replace(base, **changes)

    def _build(self, **settings_changes):
        from unittest import mock

        from voice.runtime import build_runtime

        with mock.patch("voice.runtime.SapiSpeaker"), mock.patch(
            "voice.runtime.SenseVoiceRecognizer"
        ):
            engine, resources = build_runtime(
                self.settings(**settings_changes),
                mode="goal", act=True, speak=False, enable_asr=False,
            )
        return engine, resources

    def test_act_true_but_flag_off_stays_dry_run(self):
        engine, resources = self._build(voice_goal_act_enabled=False)
        try:
            self.assertFalse(engine.goal_act)
            self.assertTrue(engine.orchestrator.goal_loop.dry_run)
        finally:
            resources.close()

    def test_act_true_and_flag_on_uses_real_executor(self):
        engine, resources = self._build(voice_goal_act_enabled=True)
        try:
            self.assertTrue(engine.goal_act)
            self.assertFalse(engine.orchestrator.goal_loop.dry_run)
            self.assertIsInstance(
                engine.orchestrator.goal_loop.action, MachineControlActionExecutor
            )
        finally:
            resources.close()


if __name__ == "__main__":
    unittest.main()
