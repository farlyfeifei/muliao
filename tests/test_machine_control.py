"""machine_control 离线测试 · 无真实 GUI、无 pywinauto 也能全绿

策略：monkeypatch 模块级 seam `machine_control._backend`，注入假 backend，
覆盖三类路径：
  · backend 为 None（库缺失 / 非 Win）→ 每个函数返回 control_unavailable 且不抛异常
  · 参数校验 → invalid_args（校验先于 backend 查询，缺参/空值/非法枚举都挡住）
  · 假 backend 的成功与失败路径 → ok / target_not_found / action_failed
并断言所有返回值都是含 "ok" 字段的 dict。
"""
from __future__ import annotations

import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import machine_control as mc  # noqa: E402


# ============ 假 backend：实现 seam 协议，可注入各种结果 ============
class _FakeWindow:
    """假窗口 token。"""

    def __init__(self, title="Notepad", pid=4242):
        self.title = title
        self.pid = pid


class FakeBackend:
    """假执行 backend。通过类属性 / 实例属性配置每次调用的返回与异常。"""

    def __init__(self):
        self.calls: list[tuple] = []
        # 默认配置
        self.windows = [{"title": "Notepad", "pid": 4242, "process": "notepad.exe", "hwnd": 1}]
        self.resolve_result = _FakeWindow()       # None 表示找不到窗口
        self.launch_result = {"pid": 999, "name": "notepad.exe"}
        # 各动作可设置为 Exception 实例 → 调用时抛出
        self.activate_exc = None
        self.close_exc = None
        self.launch_exc = None
        self.click_exc = None
        self.type_exc = None
        self.send_keys_exc = None

    def enum_windows(self, limit):
        self.calls.append(("enum_windows", limit))
        return self.windows[:limit]

    def resolve_window(self, title, pid):
        self.calls.append(("resolve_window", title, pid))
        return self.resolve_result

    def activate(self, win):
        self.calls.append(("activate", win))
        if self.activate_exc:
            raise self.activate_exc

    def close(self, win):
        self.calls.append(("close", win))
        if self.close_exc:
            raise self.close_exc

    def launch(self, name_or_path, args):
        self.calls.append(("launch", name_or_path, args))
        if self.launch_exc:
            raise self.launch_exc
        return self.launch_result

    def click(self, win, element_name, automation_id, button):
        self.calls.append(("click", element_name, automation_id, button))
        if self.click_exc:
            raise self.click_exc

    def type_text(self, win, text, element_name, enter):
        self.calls.append(("type_text", text, element_name, enter))
        if self.type_exc:
            raise self.type_exc

    def send_keys(self, win, keys):
        self.calls.append(("send_keys", keys))
        if self.send_keys_exc:
            raise self.send_keys_exc


class _BackendMixin:
    """提供 backend 注入 / 还原的通用 setUp。"""

    def install_backend(self, backend):
        self._prev_backend = mc._backend
        mc._backend = lambda: backend

    def install_none_backend(self):
        self.install_backend(None)

    def tearDown(self):
        mc._backend = getattr(self, "_prev_backend", mc._backend)
        if hasattr(self, "_prev_backend"):
            del self._prev_backend


# ============ 1. backend 为 None → control_unavailable，绝不抛异常 ============
class UnavailableTests(_BackendMixin, unittest.TestCase):
    def setUp(self):
        self.install_none_backend()

    def test_every_function_returns_control_unavailable(self):
        cases = [
            ("list_windows", {}),
            ("focus_window", {"title": "Notepad"}),
            ("close_window", {"pid": 4242}),
            ("open_application", {"name_or_path": "notepad"}),
            ("click_element", {"element_name": "OK"}),
            ("type_text", {"text": "hello"}),
            ("press_keys", {"keys": "ctrl+s"}),
        ]
        for name, kw in cases:
            with self.subTest(fn=name):
                r = getattr(mc, name)(**kw)
                self.assertIsInstance(r, dict)
                self.assertFalse(r["ok"])
                self.assertEqual(r["error"], "control_unavailable")
                self.assertIn("hint", r)

    def test_unavailable_does_not_raise_even_with_valid_args(self):
        # 合法参数 + 无 backend，仍应优雅降级
        r = mc.focus_window(title="x", pid=None)
        self.assertEqual(r["error"], "control_unavailable")


# ============ 2. 参数校验 → invalid_args（先于 backend 查询） ============
class ValidationTests(_BackendMixin, unittest.TestCase):
    def setUp(self):
        # 即便 backend 可用，非法参数也应先被挡住 → 这里装真 backend 验证优先级
        self.install_backend(FakeBackend())

    def test_list_windows_bad_limit(self):
        for bad in (0, -5, "abc", True):
            with self.subTest(limit=bad):
                r = mc.list_windows(limit=bad)
                self.assertFalse(r["ok"])
                self.assertEqual(r["error"], "invalid_args")

    def test_focus_window_requires_title_or_pid(self):
        r = mc.focus_window()
        self.assertEqual(r["error"], "invalid_args")

    def test_focus_window_bad_pid(self):
        for bad in (-1, 0, "abc", True):
            with self.subTest(pid=bad):
                r = mc.focus_window(pid=bad)
                self.assertEqual(r["error"], "invalid_args")

    def test_close_window_requires_title_or_pid(self):
        self.assertEqual(mc.close_window()["error"], "invalid_args")

    def test_close_window_bad_pid(self):
        self.assertEqual(mc.close_window(pid="not-an-int")["error"], "invalid_args")

    def test_open_application_empty_name(self):
        for bad in ("", "   ", None):
            with self.subTest(name=bad):
                self.assertEqual(mc.open_application(bad)["error"], "invalid_args")

    def test_open_application_bad_args_type(self):
        for bad in (123, {"a": 1}, [1, 2]):
            with self.subTest(args=bad):
                r = mc.open_application("notepad", args=bad)
                self.assertEqual(r["error"], "invalid_args")

    def test_click_element_requires_locator(self):
        r = mc.click_element()
        self.assertEqual(r["error"], "invalid_args")

    def test_click_element_bad_button(self):
        for bad in ("sideways", "", "LEFTT", None, 5):
            with self.subTest(button=bad):
                r = mc.click_element(element_name="OK", button=bad)
                self.assertEqual(r["error"], "invalid_args")

    def test_type_text_empty_or_non_str(self):
        self.assertEqual(mc.type_text("")["error"], "invalid_args")
        self.assertEqual(mc.type_text("   ")["error"], "invalid_args")
        self.assertEqual(mc.type_text(None)["error"], "invalid_args")
        self.assertEqual(mc.type_text(123)["error"], "invalid_args")

    def test_press_keys_empty_or_non_str(self):
        self.assertEqual(mc.press_keys("")["error"], "invalid_args")
        self.assertEqual(mc.press_keys("  ")["error"], "invalid_args")
        self.assertEqual(mc.press_keys(None)["error"], "invalid_args")
        self.assertEqual(mc.press_keys(42)["error"], "invalid_args")

    def test_validation_beats_unavailable(self):
        # 无 backend + 非法参数 → 应报 invalid_args（校验优先级更高）
        self.install_none_backend()
        self.assertEqual(mc.focus_window()["error"], "invalid_args")
        self.assertEqual(mc.type_text("")["error"], "invalid_args")
        self.assertEqual(mc.click_element(element_name="OK", button="nope")["error"], "invalid_args")


# ============ 3. 假 backend 的成功 / 失败路径 ============
class FakeBackendTests(_BackendMixin, unittest.TestCase):
    def setUp(self):
        self.be = FakeBackend()
        self.install_backend(self.be)

    # ---- list_windows ----
    def test_list_windows_success(self):
        r = mc.list_windows(limit=5)
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["windows"][0]["title"], "Notepad")
        self.assertIn(("enum_windows", 5), self.be.calls)

    def test_list_windows_caps_limit(self):
        r = mc.list_windows(limit=10 ** 9)
        self.assertTrue(r["ok"])
        # 上限防呆：传给 backend 的 limit 被封顶
        _, used = self.be.calls[-1]
        self.assertLessEqual(used, mc._MAX_LIMIT)

    # ---- focus_window ----
    def test_focus_window_hit(self):
        r = mc.focus_window(title="Notepad")
        self.assertTrue(r["ok"])
        self.assertEqual(r["title"], "Notepad")
        self.assertIn(("activate", self.be.resolve_result), self.be.calls)

    def test_focus_window_by_pid(self):
        r = mc.focus_window(pid=4242)
        self.assertTrue(r["ok"])
        self.assertEqual(r["pid"], 4242)

    def test_focus_window_miss(self):
        self.be.resolve_result = None
        r = mc.focus_window(title="DoesNotExist")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "target_not_found")

    def test_focus_window_activate_raises(self):
        self.be.activate_exc = RuntimeError("boom")
        r = mc.focus_window(title="Notepad")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "action_failed")
        self.assertIn("detail", r)

    # ---- close_window ----
    def test_close_window_hit(self):
        r = mc.close_window(title="Notepad")
        self.assertTrue(r["ok"])
        self.assertIn(("close", self.be.resolve_result), self.be.calls)

    def test_close_window_miss(self):
        self.be.resolve_result = None
        self.assertEqual(mc.close_window(title="x")["error"], "target_not_found")

    def test_close_window_raises(self):
        self.be.close_exc = RuntimeError("nope")
        self.assertEqual(mc.close_window(title="x")["error"], "action_failed")

    # ---- open_application ----
    def test_open_application_success(self):
        r = mc.open_application("notepad")
        self.assertTrue(r["ok"])
        self.assertEqual(r["pid"], 999)
        self.assertEqual(r["name"], "notepad.exe")

    def test_open_application_with_args_list(self):
        r = mc.open_application("notepad", args=["a.txt", "b.txt"])
        self.assertTrue(r["ok"])
        self.assertIn(("launch", "notepad", ["a.txt", "b.txt"]), self.be.calls)

    def test_open_application_launch_raises(self):
        self.be.launch_exc = RuntimeError("cannot start")
        r = mc.open_application("notepad")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "action_failed")

    # ---- click_element ----
    def test_click_element_by_name(self):
        r = mc.click_element(window_title="Notepad", element_name="OK")
        self.assertTrue(r["ok"])
        self.assertEqual(r["element"], "OK")
        self.assertEqual(r["button"], "left")
        self.assertIn(("click", "OK", None, "left"), self.be.calls)

    def test_click_element_by_auto_id_right_button(self):
        r = mc.click_element(automation_id="btnSave", button="right")
        self.assertTrue(r["ok"])
        self.assertEqual(r["auto_id"], "btnSave")
        self.assertEqual(r["button"], "right")

    def test_click_element_window_miss(self):
        self.be.resolve_result = None
        self.assertEqual(
            mc.click_element(window_title="x", element_name="OK")["error"],
            "target_not_found",
        )

    def test_click_element_not_found(self):
        self.be.click_exc = mc._TargetNotFound("no such control")
        r = mc.click_element(element_name="Missing")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "target_not_found")

    def test_click_element_action_failed(self):
        self.be.click_exc = RuntimeError("click blew up")
        self.assertEqual(mc.click_element(element_name="OK")["error"], "action_failed")

    # ---- type_text ----
    def test_type_text_success(self):
        r = mc.type_text("hello world")
        self.assertTrue(r["ok"])
        self.assertEqual(r["chars"], 11)
        self.assertFalse(r["enter"])

    def test_type_text_with_enter_and_window(self):
        r = mc.type_text("hi", window_title="Notepad", enter=True)
        self.assertTrue(r["ok"])
        self.assertTrue(r["enter"])
        self.assertEqual(r["window"], "Notepad")
        self.assertIn(("type_text", "hi", None, True), self.be.calls)

    def test_type_text_window_miss(self):
        self.be.resolve_result = None
        self.assertEqual(mc.type_text("hi")["error"], "target_not_found")

    def test_type_text_element_not_found(self):
        self.be.type_exc = mc._TargetNotFound("no input box")
        self.assertEqual(
            mc.type_text("hi", element_name="Missing")["error"], "target_not_found"
        )

    def test_type_text_action_failed(self):
        self.be.type_exc = RuntimeError("keyboard stuck")
        self.assertEqual(mc.type_text("hi")["error"], "action_failed")

    # ---- press_keys ----
    def test_press_keys_success(self):
        r = mc.press_keys("ctrl+s")
        self.assertTrue(r["ok"])
        self.assertEqual(r["keys"], "ctrl+s")

    def test_press_keys_passes_keys_to_backend(self):
        mc.press_keys("alt+f4", window_title="Notepad")
        self.assertIn(("send_keys", "alt+f4"), self.be.calls)

    def test_press_keys_window_miss(self):
        self.be.resolve_result = None
        self.assertEqual(mc.press_keys("enter")["error"], "target_not_found")

    def test_press_keys_target_not_found(self):
        self.be.send_keys_exc = mc._TargetNotFound("window gone")
        self.assertEqual(mc.press_keys("enter")["error"], "target_not_found")

    def test_press_keys_action_failed(self):
        self.be.send_keys_exc = RuntimeError("hook failed")
        self.assertEqual(mc.press_keys("enter")["error"], "action_failed")


# ============ 4. 所有返回值都是含 ok 字段的 dict ============
class ReturnShapeTests(_BackendMixin, unittest.TestCase):
    def test_all_returns_are_dicts_with_ok(self):
        self.install_backend(FakeBackend())
        results = [
            mc.list_windows(),
            mc.focus_window(title="Notepad"),
            mc.close_window(title="Notepad"),
            mc.open_application("notepad"),
            mc.click_element(element_name="OK"),
            mc.type_text("hi"),
            mc.press_keys("enter"),
            # 失败路径也必须是 dict
            mc.focus_window(),
            mc.type_text(""),
            mc.click_element(element_name="OK", button="bad"),
        ]
        for r in results:
            self.assertIsInstance(r, dict)
            self.assertIn("ok", r)
            self.assertIsInstance(r["ok"], bool)
            if not r["ok"]:
                self.assertIn("error", r)
                self.assertIn("hint", r)

    def test_error_detail_is_sanitized(self):
        # 异常文本里的本机路径 / 用户名不得原样泄漏
        be = FakeBackend()
        be.activate_exc = RuntimeError(r"C:\Users\AliceSecret\file.txt blew up")
        self.install_backend(be)
        r = mc.focus_window(title="Notepad")
        self.assertEqual(r["error"], "action_failed")
        self.assertNotIn("AliceSecret", r["detail"])


# ============ 5. 纯函数：组合键翻译 / 转义 / 脱敏 ============
class PureFunctionTests(unittest.TestCase):
    def test_combo_translation(self):
        self.assertEqual(mc._combo_to_keys("ctrl+s"), "^s")
        self.assertEqual(mc._combo_to_keys("alt+f4"), "%{F4}")
        self.assertEqual(mc._combo_to_keys("enter"), "{ENTER}")
        self.assertEqual(mc._combo_to_keys("ctrl+shift+n"), "^+n")
        self.assertEqual(mc._combo_to_keys("esc"), "{ESC}")
        self.assertEqual(mc._combo_to_keys("tab"), "{TAB}")

    def test_escape_literal_special_chars(self):
        out = mc._escape_literal("a+b{c}")
        self.assertNotIn("a+b", out)          # + 被转义
        self.assertIn("{{}", out)             # { 被转义

    def test_scrub_removes_user_path(self):
        msg = mc._scrub(r"failed at C:\Users\BobName\secret.txt")
        self.assertNotIn("BobName", msg)


if __name__ == "__main__":
    unittest.main()
