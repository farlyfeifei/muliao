from __future__ import annotations

import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import action_gate  # noqa: E402


def _ok_jev(answers: dict):
    """构造一个成功的 jev_ask 桩。"""
    async def _jev(state, questions, timeout=25.0):
        return {"ok": True, "answers": answers, "model": "jev-test", "ms": 5,
                "code": None, "status": 200, "attempts": 1}
    return _jev


def _fail_jev(code="timeout"):
    """构造一个失败的 jev_ask 桩（模拟超时/断网/额度）。"""
    async def _jev(state, questions, timeout=25.0):
        return {"ok": False, "answers": {}, "code": code, "need_topup": code == "quota",
                "status": None, "ms": 25000, "attempts": 1}
    return _jev


def _raise_jev():
    """构造一个抛异常的 jev_ask 桩（模拟网络层炸掉）。"""
    async def _jev(state, questions, timeout=25.0):
        raise RuntimeError("boom")
    return _jev


def _answers(risk=2.0, reversible=True, needs_confirm=False, matches_intent=True):
    return {
        "risk_score": {"type": "score", "score": risk},
        "reversible": {"type": "noul", "noul": 1.0 if reversible else 0.0},
        "needs_confirm": {"type": "noul", "noul": 1.0 if needs_confirm else 0.0},
        "matches_intent": {"type": "noul", "noul": 1.0 if matches_intent else 0.0},
    }


class LocalDangerTests(unittest.TestCase):
    def test_danger_terms_match_with_boundaries(self):
        self.assertTrue(action_gate.local_danger("press_keys", {"keys": "alt+f4"}))
        self.assertTrue(action_gate.local_danger("open_application", {"name_or_path": "format c:"}))
        self.assertTrue(action_gate.local_danger("type_text", {"text": "删除全部文件"}))
        self.assertTrue(action_gate.local_danger("run", {"cmd": "rm -rf /"}))

    def test_no_false_positive_on_payload(self):
        # 'payload' 含 'pay' 子串，但带边界后不应误命中
        self.assertFalse(action_gate.local_danger("type_text", {"text": "the payload is large"}))
        self.assertFalse(action_gate.local_danger("focus_window", {"title": "Payroll Dashboard"}))

    def test_safe_action_not_dangerous(self):
        self.assertFalse(action_gate.local_danger("list_windows", {"limit": 20}))
        self.assertFalse(action_gate.local_danger("focus_window", {"title": "记事本"}))


class EvaluateAllowConfirmTests(unittest.IsolatedAsyncioTestCase):
    async def test_readonly_low_risk_allows(self):
        d = await action_gate.evaluate(
            tool_name="list_windows", args={"limit": 20}, user_text="看看有哪些窗口",
            control=False, jev_ask=_ok_jev(_answers(risk=1.5)),
        )
        self.assertEqual(d.action, "allow")
        self.assertTrue(d.jev_ok)
        self.assertEqual(d.source, "jev")
        self.assertFalse(d.needs_confirmation)

    async def test_control_high_risk_requires_confirm(self):
        d = await action_gate.evaluate(
            tool_name="close_window", args={"title": "Word"}, user_text="关掉Word",
            control=True, jev_ask=_ok_jev(_answers(risk=7.0)),
        )
        self.assertEqual(d.action, "confirm")
        self.assertTrue(d.needs_confirmation)
        self.assertEqual(d.source, "jev")

    async def test_control_needs_confirm_flag_triggers_confirm_even_low_score(self):
        d = await action_gate.evaluate(
            tool_name="focus_window", args={"title": "X"}, user_text="切过去",
            control=True, jev_ask=_ok_jev(_answers(risk=2.0, needs_confirm=True)),
        )
        self.assertEqual(d.action, "confirm")

    async def test_irreversible_triggers_confirm(self):
        d = await action_gate.evaluate(
            tool_name="type_text", args={"text": "hi"}, user_text="输入",
            control=True, jev_ask=_ok_jev(_answers(risk=3.0, reversible=False)),
        )
        self.assertEqual(d.action, "confirm")
        self.assertIn("不可逆", d.reason)

    async def test_intent_mismatch_triggers_confirm(self):
        d = await action_gate.evaluate(
            tool_name="click_element", args={"element_name": "删除"}, user_text="今天天气",
            control=True, jev_ask=_ok_jev(_answers(risk=2.0, matches_intent=False)),
        )
        self.assertEqual(d.action, "confirm")
        self.assertIn("意图", d.reason)

    async def test_local_danger_overrides_jev_low_risk_for_control(self):
        # Jev 判低风险，但参数含 rm -rf 且是控制类 → 仍必须 confirm（defense in depth）
        d = await action_gate.evaluate(
            tool_name="open_application", args={"name_or_path": "cmd /c rm -rf /"},
            user_text="清理", control=True, jev_ask=_ok_jev(_answers(risk=1.0)),
        )
        self.assertEqual(d.action, "confirm")
        self.assertTrue(d.local_danger)
        self.assertIn("危险动作", d.reason)

    async def test_local_danger_does_not_force_confirm_for_readonly(self):
        # 只读工具即便参数文本里出现敏感词，也不因本地词表升级为 confirm（交给 Jev 分数）
        d = await action_gate.evaluate(
            tool_name="search_ai_logs", args={"keyword": "password"}, user_text="查历史",
            control=False, jev_ask=_ok_jev(_answers(risk=1.0)),
        )
        self.assertEqual(d.action, "allow")
        self.assertTrue(d.local_danger)


class FailClosedTests(unittest.IsolatedAsyncioTestCase):
    async def test_control_jev_timeout_denies(self):
        d = await action_gate.evaluate(
            tool_name="close_window", args={"title": "X"}, user_text="关掉",
            control=True, jev_ask=_fail_jev("timeout"),
        )
        self.assertEqual(d.action, "deny")
        self.assertEqual(d.source, "fail_closed")
        self.assertFalse(d.jev_ok)
        self.assertEqual(d.jev_code, "timeout")
        self.assertIn("拒绝", d.reason)

    async def test_control_jev_exception_denies(self):
        d = await action_gate.evaluate(
            tool_name="press_keys", args={"keys": "alt+f4"}, user_text="关闭",
            control=True, jev_ask=_raise_jev(),
        )
        self.assertEqual(d.action, "deny")
        self.assertEqual(d.source, "fail_closed")

    async def test_control_jev_quota_denies(self):
        d = await action_gate.evaluate(
            tool_name="focus_window", args={"title": "X"}, user_text="切换",
            control=True, jev_ask=_fail_jev("quota"),
        )
        self.assertEqual(d.action, "deny")
        self.assertEqual(d.jev_code, "quota")

    async def test_readonly_jev_failure_allows_with_fallback(self):
        d = await action_gate.evaluate(
            tool_name="get_running_processes", args={}, user_text="看进程",
            control=False, jev_ask=_fail_jev("network"),
        )
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.source, "fallback")
        self.assertFalse(d.jev_ok)
        self.assertIn("降级", d.reason)


class GateDecisionShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_decision_is_frozen_with_all_fields(self):
        d = await action_gate.evaluate(
            tool_name="list_windows", args={}, user_text="x",
            control=False, jev_ask=_ok_jev(_answers(risk=1.0)),
        )
        for field in ("action", "risk", "reason", "source", "needs_confirmation",
                      "jev_ok", "jev_code", "local_danger"):
            self.assertTrue(hasattr(d, field), f"missing field {field}")
        with self.assertRaises(Exception):
            d.action = "deny"  # frozen

    async def test_action_values_are_constrained(self):
        for control, jev, expect in [
            (False, _ok_jev(_answers(risk=1.0)), "allow"),
            (True, _ok_jev(_answers(risk=8.0)), "confirm"),
            (True, _fail_jev("timeout"), "deny"),
        ]:
            d = await action_gate.evaluate(
                tool_name="t", args={}, user_text="x", control=control, jev_ask=jev,
            )
            self.assertEqual(d.action, expect)
            self.assertIn(d.action, ("allow", "confirm", "deny"))


if __name__ == "__main__":
    unittest.main()
