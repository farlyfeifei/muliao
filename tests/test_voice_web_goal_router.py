from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.web_contracts import Observation, WebErrorCode, WebOperation, WebTarget
from voice.web_goal_router import (
    OPERATION_CONFIDENCE_FLOOR,
    WebGoalRouter,
    build_questions,
)


def make_observation(targets=None, **overrides) -> Observation:
    base = {
        "observation_id": "obs-1",
        "page_revision": "rev-1",
        "session_id": "sess-1",
        "tab_id": "tab-1",
        "origin": "https://example.com",
        "title": "Example",
        "targets": tuple(
            targets
            if targets is not None
            else [
                WebTarget("e1", role="button", label="Submit"),
                WebTarget("e2", role="textbox", label="Comment"),
            ]
        ),
    }
    base.update(overrides)
    return Observation(**base)


def probs(choice: str, ids, top: float = 0.9) -> dict:
    """A valid probability distribution peaked at ``choice`` over ``ids``."""

    others = [item for item in ids if item != choice]
    rest = round((1.0 - top) / len(others), 6) if others else 0.0
    values = {item: rest for item in others}
    values[choice] = round(top - rest * len(others), 6) if others else top
    # Fix any rounding drift so the sum is within tolerance.
    drift = round(1.0 - sum(values.values()), 6)
    values[choice] = round(values[choice] + drift, 6)
    return values


OPERATION_IDS = [
    "click", "type_text", "select", "scroll_down", "scroll_up",
    "wait", "navigate", "done", "blocked", "none",
]


def op_answer(choice: str, confidence: float = 0.9) -> dict:
    return {
        "choice": choice,
        "confidence": confidence,
        "probabilities": probs(choice, OPERATION_IDS, top=confidence),
    }


def target_answer(choice: str, ids, confidence: float = 0.9) -> dict:
    return {
        "choice": choice,
        "confidence": confidence,
        "probabilities": probs(choice, ids, top=confidence),
    }


def fake_ask(answers, calls=None):
    def ask(state, questions):
        if calls is not None:
            calls.append({"state": state, "questions": questions})
        return answers

    return ask


class TransportGatingTests(unittest.TestCase):
    def test_no_transport_returns_capability_unavailable_without_network(self):
        router = WebGoalRouter()
        decision = router.route("提交评论", make_observation())
        self.assertFalse(decision.ok)
        self.assertEqual(decision.error_code, WebErrorCode.CAPABILITY_UNAVAILABLE)

    def test_client_without_key_does_not_call_network(self):
        calls = []

        class Client:
            def _ask(self, state, questions):
                calls.append(1)
                return {}

        router = WebGoalRouter(client=Client(), api_key="")
        decision = router.route("g", make_observation())
        self.assertEqual(decision.error_code, WebErrorCode.CAPABILITY_UNAVAILABLE)
        self.assertEqual(calls, [], "must not hit the network without an api_key")

    def test_with_ask_callable_and_key_it_routes(self):
        answers = {
            "operation": op_answer("click"),
            "click_target": target_answer("e1", ["e1", "e2", "none"]),
        }
        router = WebGoalRouter(ask=fake_ask(answers), api_key="k")
        decision = router.route("点提交", make_observation())
        self.assertTrue(decision.ok)
        self.assertEqual(decision.operation, WebOperation.CLICK)

    def test_transport_exception_is_capability_error_not_crash(self):
        def boom(state, questions):
            raise RuntimeError("network down")

        router = WebGoalRouter(ask=boom, api_key="k")
        decision = router.route("g", make_observation())
        self.assertFalse(decision.ok)
        self.assertEqual(decision.error_code, WebErrorCode.CAPABILITY_UNAVAILABLE)
        self.assertIn("jev_error", decision.reason)


class QuestionBuildingTests(unittest.TestCase):
    def test_one_target_head_per_targeted_operation(self):
        questions = build_questions(make_observation(), None)
        self.assertIn("operation", questions)
        for operation in ("click", "type_text", "select"):
            self.assertIn(f"{operation}_target", questions)

    def test_text_candidate_head_only_when_candidates_exist(self):
        self.assertNotIn("text_candidate", build_questions(make_observation(), None))
        self.assertIn("text_candidate", build_questions(make_observation(), {"c0": "hi"}))

    def test_target_criteria_are_observation_ids_plus_none(self):
        questions = build_questions(make_observation(), None)
        criteria = questions["click_target"]["criteria"]
        self.assertEqual(set(criteria), {"e1", "e2", "none"})

    def test_state_excludes_full_url(self):
        calls = []
        answers = {"operation": op_answer("done")}
        router = WebGoalRouter(ask=fake_ask(answers, calls), api_key="k")
        router.route("g", make_observation())
        page = calls[0]["state"]["page"]
        self.assertNotIn("url", page)
        self.assertEqual(page["origin"], "https://example.com")


class ClickRoutingTests(unittest.TestCase):
    def test_valid_click_produces_bound_proposal(self):
        answers = {
            "operation": op_answer("click"),
            "click_target": target_answer("e1", ["e1", "e2", "none"]),
        }
        router = WebGoalRouter(ask=fake_ask(answers), api_key="k")
        decision = router.route("点提交", make_observation())
        self.assertTrue(decision.ok)
        self.assertIsNotNone(decision.proposal)
        self.assertEqual(decision.proposal.operation, WebOperation.CLICK)
        self.assertEqual(decision.proposal.target_id, "e1")
        self.assertTrue(decision.proposal.binds(make_observation()))

    def test_out_of_range_target_is_invalid_proposal(self):
        answers = {
            "operation": op_answer("click"),
            "click_target": target_answer("e999", ["e1", "e2", "none"]),
        }
        router = WebGoalRouter(ask=fake_ask(answers), api_key="k")
        decision = router.route("g", make_observation())
        self.assertFalse(decision.ok)
        self.assertEqual(decision.error_code, WebErrorCode.INVALID_PROPOSAL)

    def test_probability_sum_off_is_invalid(self):
        bad = {
            "choice": "e1",
            "confidence": 0.9,
            "probabilities": {"e1": 0.9, "e2": 0.9, "none": 0.9},  # sums to 2.7
        }
        answers = {"operation": op_answer("click"), "click_target": bad}
        router = WebGoalRouter(ask=fake_ask(answers), api_key="k")
        decision = router.route("g", make_observation())
        self.assertFalse(decision.ok)
        self.assertEqual(decision.error_code, WebErrorCode.INVALID_PROPOSAL)

    def test_choice_not_argmax_is_invalid(self):
        bad = {
            "choice": "e2",
            "confidence": 0.9,
            "probabilities": {"e1": 0.8, "e2": 0.1, "none": 0.1},  # argmax is e1
        }
        answers = {"operation": op_answer("click"), "click_target": bad}
        router = WebGoalRouter(ask=fake_ask(answers), api_key="k")
        decision = router.route("g", make_observation())
        self.assertFalse(decision.ok)
        self.assertEqual(decision.error_code, WebErrorCode.INVALID_PROPOSAL)

    def test_target_none_is_needs_input(self):
        answers = {
            "operation": op_answer("click"),
            "click_target": target_answer("none", ["e1", "e2", "none"]),
        }
        router = WebGoalRouter(ask=fake_ask(answers), api_key="k")
        decision = router.route("g", make_observation())
        self.assertFalse(decision.ok)
        self.assertEqual(decision.error_code, WebErrorCode.NEEDS_INPUT)

    def test_low_target_probability_is_needs_input(self):
        answers = {
            "operation": op_answer("click"),
            "click_target": target_answer("e1", ["e1", "e2", "none"], confidence=0.34),
        }
        router = WebGoalRouter(ask=fake_ask(answers), api_key="k")
        decision = router.route("g", make_observation())
        self.assertFalse(decision.ok)
        self.assertEqual(decision.error_code, WebErrorCode.NEEDS_INPUT)
        self.assertEqual(decision.reason, "low_target_confidence")


class OnlyChosenHeadConsumedTests(unittest.TestCase):
    def test_type_text_head_is_ignored_when_operation_is_click(self):
        # The type_text_target head returns a value, but operation=click must not
        # consume it — no text is ever attached to a click.
        answers = {
            "operation": op_answer("click"),
            "click_target": target_answer("e1", ["e1", "e2", "none"]),
            "type_text_target": target_answer("e2", ["e1", "e2", "none"]),
            "text_candidate": {"choice": "c0", "confidence": 0.9,
                               "probabilities": probs("c0", ["c0", "none"])},
        }
        router = WebGoalRouter(ask=fake_ask(answers), api_key="k")
        decision = router.route("g", make_observation(), candidates={"c0": "hello"})
        self.assertTrue(decision.ok)
        self.assertEqual(decision.operation, WebOperation.CLICK)
        self.assertEqual(decision.proposal.text_candidate_id, "")
        self.assertEqual(decision.proposal.target_id, "e1")


class DoneBlockedTests(unittest.TestCase):
    def test_done_has_no_proposal(self):
        answers = {"operation": op_answer("done")}
        router = WebGoalRouter(ask=fake_ask(answers), api_key="k")
        decision = router.route("g", make_observation())
        self.assertTrue(decision.ok)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.proposal)

    def test_blocked_is_not_ok(self):
        answers = {"operation": op_answer("blocked")}
        router = WebGoalRouter(ask=fake_ask(answers), api_key="k")
        decision = router.route("g", make_observation())
        self.assertFalse(decision.ok)
        self.assertTrue(decision.blocked)
        self.assertIsNone(decision.proposal)

    def test_low_operation_confidence_is_needs_input(self):
        answers = {"operation": op_answer("click", confidence=OPERATION_CONFIDENCE_FLOOR - 0.05)}
        router = WebGoalRouter(ask=fake_ask(answers), api_key="k")
        decision = router.route("g", make_observation())
        self.assertFalse(decision.ok)
        self.assertEqual(decision.error_code, WebErrorCode.NEEDS_INPUT)
        self.assertEqual(decision.reason, "low_operation_confidence")


class SelectNotGenerateTests(unittest.TestCase):
    def test_type_text_without_candidates_is_needs_input_never_fabricated(self):
        answers = {
            "operation": op_answer("type_text"),
            "type_text_target": target_answer("e2", ["e1", "e2", "none"]),
        }
        router = WebGoalRouter(ask=fake_ask(answers), api_key="k")
        decision = router.route("g", make_observation())  # no candidates
        self.assertFalse(decision.ok)
        self.assertEqual(decision.error_code, WebErrorCode.NEEDS_INPUT)
        self.assertIsNone(decision.proposal)

    def test_type_text_selects_candidate_id_not_text(self):
        answers = {
            "operation": op_answer("type_text"),
            "type_text_target": target_answer("e2", ["e1", "e2", "none"]),
            "text_candidate": {"choice": "c0", "confidence": 0.9,
                               "probabilities": probs("c0", ["c0", "none"])},
        }
        router = WebGoalRouter(ask=fake_ask(answers), api_key="k")
        decision = router.route("g", make_observation(), candidates={"c0": "你好世界"})
        self.assertTrue(decision.ok)
        self.assertEqual(decision.proposal.operation, WebOperation.TYPE_TEXT)
        self.assertEqual(decision.proposal.text_candidate_id, "c0")
        # The model returned an id, never the text itself.
        self.assertNotIn("你好世界", repr(decision.proposal))

    def test_type_text_candidate_not_in_set_is_needs_input(self):
        answers = {
            "operation": op_answer("type_text"),
            "type_text_target": target_answer("e2", ["e1", "e2", "none"]),
            "text_candidate": {"choice": "c9", "confidence": 0.9,
                               "probabilities": probs("c9", ["c9", "none"])},
        }
        router = WebGoalRouter(ask=fake_ask(answers), api_key="k")
        decision = router.route("g", make_observation(), candidates={"c0": "hi"})
        self.assertFalse(decision.ok)
        self.assertEqual(decision.error_code, WebErrorCode.NEEDS_INPUT)

    def test_navigate_without_candidates_is_needs_input(self):
        answers = {"operation": op_answer("navigate")}
        router = WebGoalRouter(ask=fake_ask(answers), api_key="k")
        decision = router.route("g", make_observation())
        self.assertFalse(decision.ok)
        self.assertEqual(decision.error_code, WebErrorCode.NEEDS_INPUT)

    def test_navigate_with_candidate_produces_bound_proposal(self):
        answers = {
            "operation": op_answer("navigate"),
            "text_candidate": {"choice": "c0", "confidence": 0.9,
                               "probabilities": probs("c0", ["c0", "none"])},
        }
        router = WebGoalRouter(ask=fake_ask(answers), api_key="k")
        decision = router.route(
            "g", make_observation(), candidates={"c0": "https://public.example"}
        )
        self.assertTrue(decision.ok)
        self.assertEqual(decision.proposal.operation, WebOperation.NAVIGATE)
        self.assertEqual(decision.proposal.text_candidate_id, "c0")


class StaleBindingTests(unittest.TestCase):
    def test_proposal_binds_current_page_revision(self):
        answers = {
            "operation": op_answer("click"),
            "click_target": target_answer("e1", ["e1", "e2", "none"]),
        }
        router = WebGoalRouter(ask=fake_ask(answers), api_key="k")
        observation = make_observation()
        decision = router.route("g", observation)
        self.assertEqual(decision.proposal.page_revision, observation.page_revision)
        self.assertTrue(decision.proposal.binds(observation))

    def test_target_vanished_from_fresh_observation_is_stale(self):
        # Model picked e2 but the fresh observation only has e1 (element replaced).
        answers = {
            "operation": op_answer("click"),
            "click_target": target_answer("e2", ["e1", "e2", "none"]),
        }
        router = WebGoalRouter(ask=fake_ask(answers), api_key="k")
        observation = make_observation(targets=[WebTarget("e1", role="button", label="Only")])
        # candidate id set is built from the observation, so e2 is out of range.
        decision = router.route("g", observation)
        self.assertFalse(decision.ok)
        self.assertEqual(decision.error_code, WebErrorCode.INVALID_PROPOSAL)


if __name__ == "__main__":
    unittest.main()
