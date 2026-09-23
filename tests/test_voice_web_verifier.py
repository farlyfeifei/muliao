from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.web_contracts import (
    CommitState,
    Observation,
    WebTarget,
)
from voice.web_verifier import (
    CONTRADICTED,
    SATISFIED,
    UNVERIFIED,
    Criteria,
    answer_from_page,
    criteria_from_specs,
    verify,
)


def make_observation(**overrides) -> Observation:
    base = {
        "observation_id": "obs-1",
        "page_revision": "rev-1",
        "session_id": "sess-1",
        "tab_id": "tab-1",
        "origin": "https://shop.example/cart",
        "title": "购物车 - Shop",
        "text": "",
        "targets": (),
    }
    base.update(overrides)
    return Observation(**base)


class OriginTitleTests(unittest.TestCase):
    def test_origin_match_is_satisfied(self):
        observation = make_observation(origin="https://shop.example/done")
        result = verify([Criteria("origin", "https://shop.example")], observation)
        self.assertEqual(result.status, SATISFIED)
        self.assertTrue(result.satisfied)

    def test_origin_mismatch_is_contradicted(self):
        observation = make_observation(origin="https://other.example")
        result = verify([Criteria("origin", "https://shop.example")], observation)
        self.assertEqual(result.status, CONTRADICTED)

    def test_empty_observable_field_is_unverified_not_contradicted(self):
        # A title check against an empty title has no observable signal, so it
        # is a verification gap (unverified), not a contradiction.
        result = verify([Criteria("title", "订单确认")], make_observation(title=""))
        self.assertEqual(result.status, UNVERIFIED)

    def test_title_evidence_present_and_absent(self):
        satisfied = verify(
            [Criteria("title", "订单确认")], make_observation(title="订单确认页")
        )
        self.assertEqual(satisfied.status, SATISFIED)
        contradicted = verify(
            [Criteria("title", "订单确认")], make_observation(title="首页")
        )
        # A non-empty title that lacks the evidence is a contradiction.
        self.assertEqual(contradicted.status, CONTRADICTED)


class TextEvidenceTests(unittest.TestCase):
    def test_query_answer_requires_visible_page_evidence(self):
        observation = make_observation(text="库存仅剩 3 件，请尽快下单。")
        hit = verify([Criteria("text", "库存仅剩 3 件")], observation)
        self.assertEqual(hit.status, SATISFIED)

    def test_absent_evidence_in_nonempty_text_is_contradicted(self):
        observation = make_observation(text="页面加载完成，无搜索结果。")
        miss = verify([Criteria("text", "3 件")], observation)
        self.assertEqual(miss.status, CONTRADICTED)

    def test_empty_text_is_unverified_not_contradicted(self):
        observation = make_observation(text="")
        result = verify([Criteria("text", "anything")], observation)
        self.assertEqual(result.status, UNVERIFIED)


class ControlStateTests(unittest.TestCase):
    def test_checked_state_satisfied(self):
        observation = make_observation(
            targets=(WebTarget("e1", role="checkbox", label="同意条款", checked=True),)
        )
        result = verify(
            [Criteria("control_state", "true", target_label="同意条款", attribute="checked")],
            observation,
        )
        self.assertEqual(result.status, SATISFIED)

    def test_opposite_state_is_contradicted(self):
        observation = make_observation(
            targets=(WebTarget("e1", role="checkbox", label="同意条款", checked=False),)
        )
        result = verify(
            [Criteria("control_state", "true", target_label="同意条款", attribute="checked")],
            observation,
        )
        self.assertEqual(result.status, CONTRADICTED)

    def test_absent_control_is_unverified(self):
        observation = make_observation(targets=())
        result = verify(
            [Criteria("control_state", "true", target_label="同意条款", attribute="checked")],
            observation,
        )
        self.assertEqual(result.status, UNVERIFIED)

    def test_unavailable_state_is_unverified(self):
        observation = make_observation(
            targets=(WebTarget("e1", role="checkbox", label="同意条款", checked=None),)
        )
        result = verify(
            [Criteria("control_state", "true", target_label="同意条款", attribute="checked")],
            observation,
        )
        self.assertEqual(result.status, UNVERIFIED)


class FieldValueTests(unittest.TestCase):
    def test_written_value_matches(self):
        observation = make_observation(
            targets=(WebTarget("e1", role="textbox", label="昵称",
                               metadata={"value": "小明"}),)
        )
        result = verify(
            [Criteria("field_value", "小明", target_label="昵称")], observation
        )
        self.assertEqual(result.status, SATISFIED)

    def test_written_value_differs_is_contradicted(self):
        observation = make_observation(
            targets=(WebTarget("e1", role="textbox", label="昵称",
                               metadata={"value": "别人"}),)
        )
        result = verify(
            [Criteria("field_value", "小明", target_label="昵称")], observation
        )
        self.assertEqual(result.status, CONTRADICTED)

    def test_value_not_observable_is_unverified(self):
        observation = make_observation(
            targets=(WebTarget("e1", role="textbox", label="昵称"),)
        )
        result = verify(
            [Criteria("field_value", "小明", target_label="昵称")], observation
        )
        self.assertEqual(result.status, UNVERIFIED)


class CommitUnknownTests(unittest.TestCase):
    def test_unknown_commit_never_satisfied(self):
        observation = make_observation(origin="https://shop.example/done")
        result = verify(
            [Criteria("origin", "https://shop.example")],
            observation,
            commit_state=CommitState.UNKNOWN,
        )
        self.assertNotEqual(result.status, SATISFIED)
        self.assertEqual(result.status, UNVERIFIED)
        self.assertEqual(result.commit_state, CommitState.UNKNOWN)
        self.assertFalse(result.satisfied)

    def test_committed_action_can_be_satisfied(self):
        observation = make_observation(origin="https://shop.example/done")
        result = verify(
            [Criteria("origin", "https://shop.example")],
            observation,
            commit_state=CommitState.COMMITTED,
        )
        self.assertEqual(result.status, SATISFIED)


class AggregateTests(unittest.TestCase):
    def test_all_checks_must_pass_for_satisfied(self):
        observation = make_observation(
            origin="https://shop.example/done",
            text="订单已提交",
        )
        result = verify(
            [
                Criteria("origin", "https://shop.example"),
                Criteria("text", "订单已提交"),
            ],
            observation,
        )
        self.assertEqual(result.status, SATISFIED)

    def test_one_contradiction_makes_whole_result_contradicted(self):
        observation = make_observation(
            origin="https://shop.example/done",
            text="支付失败",
        )
        result = verify(
            [
                Criteria("origin", "https://shop.example"),
                Criteria("text", "订单已提交"),
            ],
            observation,
        )
        self.assertEqual(result.status, CONTRADICTED)

    def test_empty_criteria_is_unverified(self):
        result = verify([], make_observation())
        self.assertEqual(result.status, UNVERIFIED)

    def test_criteria_from_specs_builds_typed_criteria(self):
        specs = criteria_from_specs(
            [{"kind": "origin", "expected": "https://x"}, {"kind": "text", "expected": "ok"}]
        )
        self.assertEqual(len(specs), 2)
        self.assertEqual(specs[0].kind, "origin")
        self.assertEqual(specs[1].expected, "ok")


class AnswerFromPageTests(unittest.TestCase):
    def test_grounded_answer_is_a_verbatim_substring(self):
        observation = make_observation(text="订单号是 8842。请在三天内付款。")
        answer, evidence, ok = answer_from_page("订单号是多少", observation)
        self.assertTrue(ok)
        self.assertIn(evidence, observation.text)
        self.assertTrue(answer)

    def test_empty_page_yields_no_answer(self):
        answer, evidence, ok = answer_from_page("订单号", make_observation(text=""))
        self.assertFalse(ok)
        self.assertEqual(answer, "")
        self.assertEqual(evidence, "")

    def test_evidence_is_always_a_substring_of_page_text(self):
        text = "第一句。第二句！第三句?"
        observation = make_observation(text=text)
        _, evidence, ok = answer_from_page("随便问", observation)
        self.assertTrue(ok)
        self.assertIn(evidence, text)


if __name__ == "__main__":
    unittest.main()
