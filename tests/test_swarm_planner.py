from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import swarm
import swarm_api
from swarm_planner import (
    PLANNER_SCHEMA,
    SWARM_PLAN_QUESTIONS,
    deterministic_fallback,
    evaluate_user_gate,
    materialize_plan,
    normalize_plan,
    parse_jev_response,
)


class SwarmPlannerTests(unittest.TestCase):
    def test_both_public_modules_share_one_question_schema(self):
        self.assertIs(swarm.SWARM_PLAN_QUESTIONS, SWARM_PLAN_QUESTIONS)
        self.assertIs(swarm_api.SWARM_PLAN_QUESTIONS, SWARM_PLAN_QUESTIONS)
        self.assertEqual(
            tuple(SWARM_PLAN_QUESTIONS),
            (
                "swarm_worthy",
                "recipe_id",
                "needs_clarification",
                "risk_score",
                "evidence_heavy",
                "parallelizable",
            ),
        )

    def test_legacy_core_and_api_answers_normalize_identically(self):
        core = parse_jev_response({
            "ok": True,
            "answers": {
                "swarm_worthy": {"noul": 0.92},
                "task_type": {"choice": "research", "confidence": 0.91},
                "needs_clarify": {"noul": 0.05},
                "risk_level": {"score": 2},
                "evidence_heavy": {"noul": 0.95},
                "parallelizable": {"noul": 0.9},
            },
        })
        api = parse_jev_response({
            "ok": True,
            "answers": {
                "swarm_worthy": {"choice": "yes"},
                "task_type": {"choice": "research", "confidence": 0.91},
                "needs_clarify": {"choice": "no"},
                "risk_level": {"choice": "low"},
                "evidence_heavy": {"choice": "yes"},
                "parallelizable": {"choice": "yes"},
            },
        })
        self.assertEqual(core.recipe_id, api.recipe_id)
        self.assertEqual(core.swarm_worthy, api.swarm_worthy)
        self.assertEqual(core.needs_clarification, api.needs_clarification)
        self.assertEqual(core.evidence_heavy, api.evidence_heavy)
        self.assertEqual(core.parallelizable, api.parallelizable)
        self.assertEqual(evaluate_user_gate(core).kind, "none")
        self.assertEqual(evaluate_user_gate(api).kind, "none")

    def test_risk_six_is_sensitive_and_requires_confirmation_everywhere(self):
        decision = normalize_plan({
            "recipe": "build",
            "swarm_worthy": True,
            "risk_level": 6,
        })
        plan = materialize_plan(decision)
        self.assertEqual(decision.recipe_id, "sensitive")
        self.assertEqual(plan["recipe"], "sensitive")
        self.assertEqual(plan["risk_level"], 6.0)
        self.assertTrue(plan["requires_confirmation"])
        self.assertEqual(plan["user_gate"]["kind"], "confirmation")

    def test_confirmation_never_bypasses_clarification_aliases(self):
        for key in ("needs_clarify", "needs_clarification"):
            with self.subTest(key=key):
                decision = normalize_plan({
                    "recipe": "research",
                    "swarm_worthy": True,
                    key: True,
                    "risk_level": 1,
                })
                self.assertEqual(evaluate_user_gate(decision, confirmed=True).kind, "clarification")
                plan = materialize_plan(decision, confirmed=True)
                self.assertTrue(plan["requires_confirmation"])
                self.assertFalse(plan["execution_allowed"])

    def test_explicit_confirmation_is_not_lost_by_low_risk_normalization(self):
        decision = normalize_plan({
            "recipe": "build",
            "swarm_worthy": True,
            "risk_level": 1,
            "requires_confirmation": True,
            "confirmation_reasons": ["operator_review"],
        })
        self.assertTrue(decision.confirmation_required)
        self.assertIn("operator_review", decision.confirmation_reasons)
        self.assertEqual(evaluate_user_gate(decision).kind, "confirmation")
        self.assertEqual(evaluate_user_gate(decision, confirmed=True).kind, "none")

    def test_fallback_and_materialized_plan_are_deterministic(self):
        first = materialize_plan(deterministic_fallback("分析多个来源并给出证据"))
        second = materialize_plan(deterministic_fallback("分析多个来源并给出证据"))
        self.assertEqual(first, second)
        self.assertEqual(first["planner_schema"], PLANNER_SCHEMA)
        self.assertEqual(first["recipe"], "research")
        self.assertTrue(first["degraded"])
        self.assertTrue(first["swarm_worthy"])


if __name__ == "__main__":
    unittest.main()
