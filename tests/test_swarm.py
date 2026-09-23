from __future__ import annotations

import asyncio
import unittest
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import swarm


async def collect_events(iterator):
    return [event async for event in iterator]


def event_types(events):
    return [event["type"] for event in events]


class SwarmPureFunctionTests(unittest.TestCase):
    def test_fixed_roles_and_recipes(self):
        self.assertEqual(
            swarm.ROLE_POOL,
            ("compiler", "investigator", "extractor", "builder", "verifier", "integrator"),
        )
        self.assertEqual(set(swarm.RECIPES), {"single", "research", "diagnose", "build", "sensitive"})
        for recipe in swarm.RECIPES.values():
            for stage in recipe["stages"]:
                self.assertTrue(set(stage).issubset(set(swarm.ROLE_POOL)))

    def test_fallback_and_contract_are_deterministic(self):
        goal = "分析多个来源并给出证据"
        first = swarm.deterministic_fallback_plan(goal)
        second = swarm.deterministic_fallback_plan(goal)
        self.assertEqual(first, second)
        self.assertEqual(first["recipe"], "research")
        self.assertTrue(first["degraded"])

        permission = {"epoch": 12, "scopes": {"files": True, "browser": False}}
        contract_a = swarm.build_contract(goal, first, permission)
        contract_b = swarm.build_contract(goal, first, permission)
        self.assertEqual(contract_a, contract_b)
        self.assertEqual(contract_a["allowed_tools"], ["files"])
        self.assertEqual(contract_a["budgets"]["max_retries_per_bee"], 1)

    def test_event_helper_envelope(self):
        event = swarm.make_event(
            "bee.start",
            {"bee_id": "compiler"},
            run_id="run_test",
            task_id="tsk_test",
            seq=1,
            ts=123.5,
        )
        self.assertEqual(event["type"], "bee.start")
        self.assertEqual(event["seq"], 1)
        self.assertEqual(event["ts"], 123.5)
        self.assertTrue(event["event_id"].startswith("evt_"))


class SwarmOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_research_parallel_stage_and_event_order(self):
        active = 0
        peak = 0
        starts: dict[str, float] = {}
        releases: dict[str, float] = {}
        lock = asyncio.Lock()

        async def runner(bee_id, context):
            nonlocal active, peak
            async with lock:
                active += 1
                peak = max(peak, active)
                starts[bee_id] = asyncio.get_running_loop().time()
            if bee_id in {"investigator", "extractor"}:
                await asyncio.sleep(0.04)
            else:
                await asyncio.sleep(0.005)
            async with lock:
                releases[bee_id] = asyncio.get_running_loop().time()
                active -= 1
            return {"summary": f"{bee_id} complete", "stage": context["stage"]}

        async def checker(state, questions):
            answers = {}
            for key in questions:
                if key.startswith("correction_action_"):
                    answers[key] = "accept"
                elif key.startswith("conflict_present_"):
                    answers[key] = False
                else:
                    answers[key] = True
            return {"ok": True, "answers": answers}

        events = await collect_events(
            swarm.orchestrate(
                "分析多个来源并给出证据",
                runner,
                checker,
                recipe="research",
                max_parallel=2,
                run_id="run_research",
            )
        )

        self.assertEqual(events[-1]["type"], "swarm.done")
        self.assertEqual([event["seq"] for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(len({event["event_id"] for event in events}), len(events))
        self.assertGreaterEqual(peak, 2)
        self.assertLess(starts["investigator"], releases["extractor"])
        self.assertLess(starts["extractor"], releases["investigator"])
        self.assertGreater(starts["verifier"], max(releases["investigator"], releases["extractor"]))

        starts_seen = [
            event["payload"]["bee_id"] for event in events if event["type"] == "bee.start"
        ]
        self.assertEqual(
            Counter(starts_seen),
            Counter({"compiler": 1, "investigator": 1, "extractor": 1, "verifier": 1, "integrator": 1}),
        )
        self.assertIn("handoff.created", event_types(events))
        self.assertIn("handoff.ack", event_types(events))

    async def test_one_correction_retry_per_bee(self):
        attempts = Counter()
        retried = False

        async def runner(bee_id, context):
            attempts[bee_id] += 1
            return {"summary": f"{bee_id} attempt {context['attempt']}"}

        async def checker(state, questions):
            nonlocal retried
            answers = {}
            for key in questions:
                if key == "correction_action_builder" and not retried:
                    answers[key] = "retry"
                    retried = True
                elif key.startswith("correction_action_"):
                    answers[key] = "accept"
                elif key.startswith("conflict_present_"):
                    answers[key] = False
                else:
                    answers[key] = True
            return {"ok": True, "answers": answers}

        events = await collect_events(
            swarm.orchestrate(
                "生成一份报告",
                runner,
                checker,
                recipe="build",
                run_id="run_retry",
            )
        )

        self.assertEqual(events[-1]["type"], "swarm.done")
        self.assertEqual(attempts["builder"], 2)
        self.assertEqual(attempts["compiler"], 1)
        corrections = [
            event for event in events
            if event["type"] == "bee.correct" and event["payload"]["bee_id"] == "builder"
        ]
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["payload"]["retry"], 1)
        builder_starts = [
            event["payload"]["attempt"] for event in events
            if event["type"] == "bee.start" and event["payload"]["bee_id"] == "builder"
        ]
        self.assertEqual(builder_starts, [1, 2])

    async def test_cancel_stops_parallel_bees_and_is_terminal(self):
        cancel_event = asyncio.Event()
        started = asyncio.Event()
        cancelled = set()

        async def runner(bee_id, context):
            if bee_id == "compiler":
                return {"summary": "compiled"}
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.add(bee_id)
                raise
            return {"summary": "unexpected"}

        async def checker(state, questions):
            answers = {}
            for key in questions:
                if key.startswith("correction_action_"):
                    answers[key] = "accept"
                elif key.startswith("conflict_present_"):
                    answers[key] = False
                else:
                    answers[key] = True
            return {"ok": True, "answers": answers}

        async def trigger_cancel():
            await started.wait()
            cancel_event.set()

        cancel_task = asyncio.create_task(trigger_cancel())
        events = await asyncio.wait_for(
            collect_events(
                swarm.orchestrate(
                    "分析多个来源并给出证据",
                    runner,
                    checker,
                    recipe="research",
                    cancel_event=cancel_event,
                    max_parallel=2,
                    run_id="run_cancel",
                )
            ),
            timeout=2,
        )
        await cancel_task

        self.assertEqual(events[-1]["type"], "swarm.cancelled")
        self.assertNotIn("swarm.done", event_types(events))
        self.assertNotIn("swarm.error", event_types(events))
        self.assertEqual(cancelled, {"investigator", "extractor"})

    async def test_sensitive_waits_for_confirmation(self):
        runner_calls = []

        async def runner(bee_id, context):
            runner_calls.append(bee_id)
            return {"summary": "should not run"}

        async def checker(state, questions):
            raise AssertionError("explicit recipe must not require a planner call before confirmation")

        events = await collect_events(
            swarm.orchestrate(
                "删除整个项目",
                runner,
                checker,
                recipe="sensitive",
                confirmed=False,
                run_id="run_sensitive",
            )
        )

        self.assertEqual(event_types(events), ["swarm.plan", "contract.created", "swarm.waiting_user"])
        self.assertEqual(events[-1]["payload"]["reason"], "high_risk_confirmation_required")
        self.assertEqual(runner_calls, [])

    async def test_jev_failure_uses_fallback_and_disables_auto_correction(self):
        checker_calls = 0
        runner_calls = []

        async def runner(bee_id, context):
            runner_calls.append(bee_id)
            return {"summary": f"{bee_id} complete"}

        async def checker(state, questions):
            nonlocal checker_calls
            checker_calls += 1
            raise ConnectionError("Jev offline")

        events = await collect_events(
            swarm.orchestrate(
                "分析多个来源并给出证据",
                runner,
                checker,
                run_id="run_fallback",
            )
        )

        self.assertEqual(events[-1]["type"], "swarm.done")
        self.assertEqual(checker_calls, 1)
        plan_event = events[0]
        self.assertEqual(plan_event["type"], "swarm.plan")
        self.assertEqual(plan_event["payload"]["recipe"], "research")
        self.assertTrue(plan_event["payload"]["degraded"])
        self.assertIn("checker_error:ConnectionError", plan_event["payload"]["degraded_reason"])
        checks = [event for event in events if event["type"] == "bee.check"]
        self.assertTrue(checks)
        self.assertTrue(all(event["payload"]["status"] == "degraded" for event in checks))
        self.assertFalse(any(event["type"] == "bee.correct" for event in events))
        self.assertEqual(
            Counter(runner_calls),
            Counter({"compiler": 1, "investigator": 1, "extractor": 1, "verifier": 1, "integrator": 1}),
        )


if __name__ == "__main__":
    unittest.main()
