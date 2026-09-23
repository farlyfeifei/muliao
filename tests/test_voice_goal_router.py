from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.goal_router import (
    OPERATION_CONFIDENCE_FLOOR,
    JevGoalChooser,
    build_questions,
    parse_element_ids,
)


STATE = (
    "source=uia\n"
    "fallback=none\n"
    "candidates=2\n"
    "e001 | role=Button | text=文件 | bounds=(1,2,3,4) | center=(2,3)\n"
    "e002 | role=EditControl | text=评论框 | bounds=(5,6,7,8) | center=(6,7)"
)

KIND_IDS = ["click", "type", "enter", "esc", "done", "stuck", "none"]


def probs(choice, ids, top=0.9):
    others = [item for item in ids if item != choice]
    rest = round((1.0 - top) / len(others), 6) if others else 0.0
    values = {item: rest for item in others}
    values[choice] = round(top - rest * len(others), 6) if others else top
    drift = round(1.0 - sum(values.values()), 6)
    values[choice] = round(values[choice] + drift, 6)
    return values


def kind_answer(choice, confidence=0.9):
    return {"choice": choice, "confidence": confidence,
            "probabilities": probs(choice, KIND_IDS, confidence)}


def target_answer(choice, ids, confidence=0.9):
    return {"choice": choice, "confidence": confidence,
            "probabilities": probs(choice, ids, confidence)}


def chooser(script, calls=None):
    def ask(state, questions):
        if calls is not None:
            calls.append({"state": state, "questions": questions})
        return script

    return JevGoalChooser(ask=ask, api_key="test"), calls


class ParseTests(unittest.TestCase):
    def test_element_ids_are_parsed_in_order(self):
        self.assertEqual(parse_element_ids(STATE), ["e001", "e002"])

    def test_no_ids_in_empty_state(self):
        self.assertEqual(parse_element_ids(""), [])

    def test_duplicate_ids_are_deduplicated(self):
        state = "e001 | role=Button | text=A\ne001 | role=Button | text=A"
        self.assertEqual(parse_element_ids(state), ["e001"])


class QuestionTests(unittest.TestCase):
    def test_target_criteria_are_element_ids_plus_none(self):
        questions = build_questions(["e001", "e002"])
        self.assertEqual(set(questions["click_target"]["criteria"]), {"e001", "e002", "none"})
        self.assertIn("kind", questions)

    def test_state_carries_goal_and_elements_not_a_screenshot(self):
        calls = []
        ch, _ = chooser({"kind": kind_answer("done")}, calls)
        ch.choose("保存文件", STATE, 1)
        self.assertEqual(calls[0]["state"]["goal"], "保存文件")
        self.assertIn("e001", calls[0]["state"]["elements"])
        self.assertNotIn("screenshot", repr(calls[0]["state"]).casefold())


class TransportGatingTests(unittest.TestCase):
    def test_no_transport_is_stuck_without_network(self):
        decision = JevGoalChooser().choose("g", STATE, 1)
        self.assertTrue(decision["stuck"])
        self.assertEqual(decision["reason"], "no_jev_transport")

    def test_client_without_key_does_not_call_network(self):
        calls = []

        class Client:
            def _ask(self, state, questions):
                calls.append(1)
                return {}

        decision = JevGoalChooser(client=Client(), api_key="").choose("g", STATE, 1)
        self.assertTrue(decision["stuck"])
        self.assertEqual(calls, [])

    def test_no_elements_is_stuck(self):
        ch, _ = chooser({"kind": kind_answer("click")})
        decision = ch.choose("g", "source=uia\ncandidates=0", 1)
        self.assertTrue(decision["stuck"])
        self.assertEqual(decision["reason"], "no_elements")

    def test_transport_exception_is_stuck_not_crash(self):
        def boom(state, questions):
            raise RuntimeError("network down")

        decision = JevGoalChooser(ask=boom, api_key="k").choose("g", STATE, 1)
        self.assertTrue(decision["stuck"])
        self.assertIn("jev_error", decision["reason"])


class ClickRoutingTests(unittest.TestCase):
    def test_valid_click_returns_grounded_id_and_action(self):
        ch, _ = chooser({
            "kind": kind_answer("click"),
            "click_target": target_answer("e001", ["e001", "e002", "none"]),
        })
        decision = ch.choose("点文件", STATE, 1)
        self.assertEqual(decision["id"], "e001")
        self.assertEqual(decision["action"], "click")
        self.assertNotIn("stuck", decision)

    def test_out_of_range_target_is_stuck(self):
        ch, _ = chooser({
            "kind": kind_answer("click"),
            "click_target": target_answer("e999", ["e001", "e002", "none"]),
        })
        decision = ch.choose("g", STATE, 1)
        self.assertTrue(decision["stuck"])
        self.assertEqual(decision["reason"], "target_choice_invalid")

    def test_probability_sum_off_is_stuck(self):
        ch, _ = chooser({
            "kind": kind_answer("click"),
            "click_target": {"choice": "e001", "confidence": 0.9,
                             "probabilities": {"e001": 0.9, "e002": 0.9, "none": 0.9}},
        })
        decision = ch.choose("g", STATE, 1)
        self.assertTrue(decision["stuck"])
        self.assertEqual(decision["reason"], "target_choice_invalid")

    def test_choice_not_argmax_is_stuck(self):
        ch, _ = chooser({
            "kind": kind_answer("click"),
            "click_target": {"choice": "e002", "confidence": 0.9,
                             "probabilities": {"e001": 0.8, "e002": 0.1, "none": 0.1}},
        })
        decision = ch.choose("g", STATE, 1)
        self.assertTrue(decision["stuck"])

    def test_target_none_is_stuck(self):
        ch, _ = chooser({
            "kind": kind_answer("click"),
            "click_target": target_answer("none", ["e001", "e002", "none"]),
        })
        decision = ch.choose("g", STATE, 1)
        self.assertTrue(decision["stuck"])
        self.assertEqual(decision["reason"], "no_suitable_target")

    def test_low_target_probability_is_stuck(self):
        ch, _ = chooser({
            "kind": kind_answer("click"),
            "click_target": target_answer("e001", ["e001", "e002", "none"], confidence=0.34),
        })
        decision = ch.choose("g", STATE, 1)
        self.assertTrue(decision["stuck"])
        self.assertEqual(decision["reason"], "low_target_confidence")

    def test_low_operation_confidence_is_stuck(self):
        ch, _ = chooser({"kind": kind_answer("click", confidence=OPERATION_CONFIDENCE_FLOOR - 0.05)})
        decision = ch.choose("g", STATE, 1)
        self.assertTrue(decision["stuck"])
        self.assertEqual(decision["reason"], "low_operation_confidence")


class TerminalTests(unittest.TestCase):
    def test_done_is_complete(self):
        ch, _ = chooser({"kind": kind_answer("done")})
        decision = ch.choose("g", STATE, 1)
        self.assertTrue(decision["complete"])
        self.assertEqual(decision["action"], "done")

    def test_stuck_kind_is_stuck(self):
        ch, _ = chooser({"kind": kind_answer("stuck")})
        decision = ch.choose("g", STATE, 1)
        self.assertTrue(decision["stuck"])

    def test_unknown_kind_is_stuck(self):
        ch, _ = chooser({"kind": {"choice": "execute_shell", "confidence": 0.99,
                                  "probabilities": probs("execute_shell", ["execute_shell"] + KIND_IDS)}})
        decision = ch.choose("g", STATE, 1)
        self.assertTrue(decision["stuck"])
        self.assertEqual(decision["reason"], "unknown_operation")


class SelectNotGenerateTests(unittest.TestCase):
    def test_type_step_never_carries_model_text(self):
        # The chooser returns only the element id + action for a type step; it
        # must never author the value (select-not-generate).
        ch, _ = chooser({
            "kind": kind_answer("type"),
            "click_target": target_answer("e002", ["e001", "e002", "none"]),
        })
        decision = ch.choose("填写评论", STATE, 1)
        self.assertEqual(decision["action"], "type")
        self.assertEqual(decision["id"], "e002")
        self.assertNotIn("text", decision)
        self.assertNotIn("value", decision)


if __name__ == "__main__":
    unittest.main()
