"""蜂群纠偏闭环测试：Jev 复核意见必须真正送达被重试的蜂。

回归的核心 bug：swarm.py 把 correction 放进 bee context，但 server._swarm_bee_runner
调用 runtime.run() 时没传它，run() 也没这个参数。结果 Jev 算出了纠偏意见、前端也收到
bee.correct 事件，可那只被要求重试的蜂拿到的 prompt 与上一轮逐字节相同——纠偏空转，
重试耗尽后 retry_exhausted，整个 run 失败。
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import swarm_runtime as sr  # noqa: E402
from swarm_models import BeeSpec  # noqa: E402


class CorrectionNoteTests(unittest.TestCase):
    def test_none_and_empty_return_none(self):
        self.assertIsNone(sr._correction_note(None))
        self.assertIsNone(sr._correction_note({}))
        self.assertIsNone(sr._correction_note("not-a-mapping"))

    def test_all_pass_accept_returns_none(self):
        """全通过 + accept：没有要整改的，不注入噪声。"""
        c = {"action": "accept", "on_contract": True, "evidence_sufficient": True,
             "safe_to_continue": True, "conflict_present": False}
        self.assertIsNone(sr._correction_note(c))

    def test_evidence_insufficient_listed(self):
        c = {"action": "retry", "match_score": 4.18, "lesson_tier": "neutral",
             "on_contract": True, "evidence_sufficient": False,
             "safe_to_continue": True, "conflict_present": False, "reason": "证据不足"}
        note = sr._correction_note(c)
        self.assertEqual(note["action"], "retry")
        self.assertEqual(note["previous_match_score"], 4.18)
        self.assertEqual(note["failed_checks"], ["证据是否足以支撑下一阶段"])
        self.assertIn("4.18", note["instruction"])
        self.assertIn("证据不足", note["instruction"])

    def test_conflict_present_polarity(self):
        """conflict_present=True 才是问题（正向语义），别和其余三项搞反。"""
        with_conflict = sr._correction_note({
            "action": "retry", "on_contract": True, "evidence_sufficient": True,
            "safe_to_continue": True, "conflict_present": True})
        self.assertEqual(with_conflict["failed_checks"], ["存在未解决的冲突"])

        without_conflict = sr._correction_note({
            "action": "retry", "on_contract": True, "evidence_sufficient": False,
            "safe_to_continue": True, "conflict_present": False})
        self.assertNotIn("存在未解决的冲突", without_conflict["failed_checks"])

    def test_contract_breach_listed(self):
        note = sr._correction_note({
            "action": "retry", "on_contract": False, "evidence_sufficient": True,
            "safe_to_continue": True, "conflict_present": False})
        self.assertEqual(note["failed_checks"], ["是否守在任务契约范围内"])

    def test_multiple_failures_all_listed(self):
        note = sr._correction_note({
            "action": "retry", "on_contract": False, "evidence_sufficient": False,
            "safe_to_continue": True, "conflict_present": True})
        self.assertEqual(len(note["failed_checks"]), 3)

    def test_reason_only_still_produces_note(self):
        note = sr._correction_note({"action": "retry", "reason": "偏离了目标"})
        self.assertIsNotNone(note)
        self.assertIn("偏离了目标", note["instruction"])


class BaseMessagesCorrectionTests(unittest.TestCase):
    def _runtime(self):
        return sr.BeeRuntime(llm_base="http://x", key="k", model="m", system_prompt="SYS")

    def _spec(self):
        return BeeSpec(bee_id="compiler", role_prompt="你是 compiler", allowed_tools=())

    def test_no_correction_leaves_messages_unchanged(self):
        rt = self._runtime()
        base = rt._base_messages(self._spec(), "目标", {}, [])
        with_none = rt._base_messages(self._spec(), "目标", {}, [], correction=None)
        self.assertEqual(len(base), 3)          # system + role + user
        self.assertEqual(len(with_none), 3)
        self.assertNotIn("correction", base[-1]["content"])

    def test_correction_is_injected_into_prompt(self):
        """核心回归：纠偏意见必须真的进入蜂的 prompt。"""
        rt = self._runtime()
        correction = {"action": "retry", "match_score": 3.2, "lesson_tier": "poor",
                      "on_contract": True, "evidence_sufficient": False,
                      "safe_to_continue": True, "conflict_present": False,
                      "reason": "只给了结论没给证据"}
        msgs = rt._base_messages(self._spec(), "目标", {}, [], correction=correction)
        # 比无纠偏时多一条显式指令
        self.assertEqual(len(msgs), 4)
        joined = "\n".join(m["content"] for m in msgs)
        self.assertIn("要求重做", joined)
        self.assertIn("只给了结论没给证据", joined)
        self.assertIn("3.2", joined)
        # SYSTEM_PROMPT 位置的消息绝不能被改（冻结哈希约束）
        self.assertEqual(msgs[0]["content"], "SYS")
        self.assertEqual(msgs[0]["role"], "system")
        # 最后一条是给蜂的显式整改指令
        self.assertEqual(msgs[-1]["role"], "user")
        self.assertIn("未通过项", msgs[-1]["content"])

    def test_correction_embedded_in_json_payload_too(self):
        rt = self._runtime()
        msgs = rt._base_messages(
            self._spec(), "目标", {}, [],
            correction={"action": "retry", "reason": "再取证"})
        # 第三条（input_message JSON）里应含 correction 字段
        self.assertIn('"correction"', msgs[2]["content"])

    def test_run_signature_accepts_correction_kwarg(self):
        """确保 server._swarm_bee_runner 能按关键字传 correction（接口契约）。"""
        import inspect
        sig = inspect.signature(sr.BeeRuntime.run)
        self.assertIn("correction", sig.parameters)
        # 默认 None：不传也能跑，兼容旧调用方
        self.assertIsNone(sig.parameters["correction"].default)


if __name__ == "__main__":
    unittest.main()
