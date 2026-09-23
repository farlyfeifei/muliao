import asyncio
import datetime as dt
import json
import pathlib
import sys
import threading
import unittest
from dataclasses import dataclass


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from swarm_api import SWARM_PLAN_QUESTIONS, SwarmService  # noqa: E402


SAFE_ANSWERS = {
    "swarm_worthy": {"noul": 0.95},
    "recipe_id": {"choice": "research"},
    "needs_clarification": {"noul": 0.05},
    "risk_score": {"score": 1.5},
    "evidence_heavy": {"noul": 0.95},
    "parallelizable": {"noul": 0.95},
}


class SwarmServicePlanTests(unittest.TestCase):
    def test_jev_success_uses_one_batch_with_all_six_questions(self):
        calls = []

        async def jev_ask(state, questions):
            calls.append((json.loads(state), questions))
            return {
                "ok": True,
                "answers": SAFE_ANSWERS,
                "model": "mock-jev",
                "ms": 7,
            }

        service = SwarmService(orchestrator=object())
        result = service.plan(
            "Research three implementations in parallel and compare evidence.",
            "session-1",
            {"network": True},
            jev_ask,
        )

        self.assertEqual(len(calls), 1)
        state, questions = calls[0]
        self.assertEqual(set(questions), set(SWARM_PLAN_QUESTIONS))
        self.assertEqual(
            set(questions),
            {
                "swarm_worthy",
                "recipe_id",
                "needs_clarification",
                "risk_score",
                "evidence_heavy",
                "parallelizable",
            },
        )
        self.assertEqual(state["session_id"], "session-1")
        self.assertEqual(result["source"], "jev")
        self.assertTrue(result["swarm_worthy"])
        self.assertFalse(result["requires_confirmation"])
        self.assertTrue(result["execution_allowed"])
        self.assertEqual(result["status"], "planned")

    def test_jev_failure_uses_deterministic_fallback(self):
        calls = 0

        def failing_jev(state, questions):
            nonlocal calls
            calls += 1
            raise TimeoutError("mock timeout")

        goal = "Research multiple sources in parallel, compare evidence, and verify conclusions."
        first = SwarmService().plan(goal, "session-a", {}, failing_jev)
        second = SwarmService().plan(goal, "session-b", {}, failing_jev)

        self.assertEqual(calls, 2)
        for field in (
            "swarm_worthy",
            "recipe_id",
            "needs_clarification",
            "risk_score",
            "evidence_heavy",
            "parallelizable",
        ):
            self.assertEqual(first[field], second[field])
        self.assertEqual(first["source"], "fallback")
        self.assertIn("TimeoutError", first["fallback_reason"])
        self.assertTrue(first["evidence_heavy"])
        self.assertTrue(first["parallelizable"])
        self.assertTrue(first["swarm_worthy"])

    def test_high_risk_plan_requires_confirmation(self):
        def high_risk_jev(state, questions):
            answers = {key: dict(value) for key, value in SAFE_ANSWERS.items()}
            answers["risk_score"] = {"score": 7}
            return {"ok": True, "answers": answers}

        service = SwarmService(orchestrator=_ExplodingOrchestrator())
        plan = service.plan("Deploy and publish the change.", "session-risk", {}, high_risk_jev)

        self.assertTrue(plan["requires_confirmation"])
        self.assertFalse(plan["execution_allowed"])
        self.assertEqual(plan["status"], "requires_confirmation")
        self.assertIn("high_risk", plan["confirmation_reasons"])

        events = asyncio.run(_collect(service.stream_run(
            plan,
            plan["goal"],
            plan["session_id"],
            {},
            high_risk_jev,
            None,
            asyncio.Event(),
        )))
        self.assertEqual([event["type"] for event in events], ["swarm.waiting_user"])
        self.assertFalse(service._orchestrator.called)
        self.assertEqual(service.status(plan["run_id"])["status"], "requires_confirmation")


@dataclass
class _Payload:
    path: pathlib.Path
    created_at: dt.datetime
    labels: set[str]
    raw: bytes


class _EventOrchestrator:
    async def stream_run(self, plan, goal, session_id, **kwargs):
        yield {
            "event_type": "bee.tool_result",
            "payload": _Payload(
                path=pathlib.Path("artifact.txt"),
                created_at=dt.datetime(2026, 9, 23, 12, 30, tzinfo=dt.timezone.utc),
                labels={"beta", "alpha"},
                raw=b"ok",
            ),
        }
        yield _Payload(
            path=pathlib.Path("done.txt"),
            created_at=dt.datetime(2026, 9, 23, 12, 31, tzinfo=dt.timezone.utc),
            labels={"done"},
            raw=b"complete",
        )


class _ExplodingOrchestrator:
    def __init__(self):
        self.called = False

    async def stream_run(self, **kwargs):
        self.called = True
        raise AssertionError("orchestrator must not run before confirmation")
        yield  # pragma: no cover


class _BlockingOrchestrator:
    def __init__(self):
        self.entered = asyncio.Event()

    async def stream_run(self, cancel_event, **kwargs):
        self.entered.set()
        yield {"type": "bee.start", "payload": {"ok": True}}
        while not cancel_event.is_set():
            await asyncio.sleep(0.005)


class _ForeignIdentityOrchestrator:
    async def stream_run(self, **kwargs):
        yield {
            "event_id": "foreign-event",
            "seq": 77,
            "run_id": "foreign-run",
            "type": "swarm.done",
            "payload": {"final_text": "done"},
        }


class SwarmServiceStreamTests(unittest.TestCase):
    def test_stream_events_are_json_serializable_and_normalized(self):
        service = SwarmService(orchestrator=_EventOrchestrator())
        plan = {
            "run_id": "run-normalize",
            "swarm_worthy": True,
            "needs_clarify": False,
            "risk_level": "low",
        }

        events = asyncio.run(_collect(service.stream_run(
            plan,
            "normalize mock events",
            "session-stream",
            {"model": "mock"},
            None,
            None,
            asyncio.Event(),
        )))

        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["type"], "bee.tool_result")
        self.assertEqual(events[0]["run_id"], "run-normalize")
        self.assertEqual(events[0]["seq"], 1)
        self.assertEqual(events[0]["payload"]["path"], "artifact.txt")
        self.assertEqual(events[0]["payload"]["labels"], ["alpha", "beta"])
        self.assertEqual(events[0]["payload"]["raw"], "ok")
        self.assertEqual(events[1]["type"], "swarm.event")
        self.assertEqual(events[2]["type"], "swarm.error")
        self.assertEqual(events[2]["payload"]["code"], "missing_terminal")
        json.dumps(events, ensure_ascii=False)
        self.assertEqual(service.status("run-normalize")["status"], "failed")

    def test_backend_event_identity_is_rebound_to_current_run(self):
        service = SwarmService(orchestrator=_ForeignIdentityOrchestrator())
        plan = {
            "run_id": "run-current",
            "swarm_worthy": True,
            "needs_clarify": False,
            "risk_level": "low",
        }
        events = asyncio.run(_collect(service.stream_run(
            plan,
            "identity-safe",
            "session-current",
            {},
            None,
            None,
            asyncio.Event(),
        )))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["run_id"], "run-current")
        self.assertEqual(events[0]["seq"], 1)
        self.assertEqual(events[0]["type"], "swarm.done")
        self.assertEqual(service.status("run-current")["status"], "completed")
        self.assertEqual(service.status("foreign-run")["status"], "unknown")

    def test_cancel_stops_an_active_stream_and_updates_status(self):
        async def scenario():
            orchestrator = _BlockingOrchestrator()
            service = SwarmService(orchestrator=orchestrator)
            plan = {
                "run_id": "run-cancel",
                "swarm_worthy": True,
                "needs_clarify": False,
                "risk_level": "low",
            }
            events = []

            async def consume():
                async for event in service.stream_run(
                    plan,
                    "long running task",
                    "session-cancel",
                    {},
                    None,
                    None,
                    asyncio.Event(),
                ):
                    events.append(event)

            task = asyncio.create_task(consume())
            await asyncio.wait_for(orchestrator.entered.wait(), timeout=1)
            while not events:
                await asyncio.sleep(0)
            first_cancel = service.cancel("run-cancel")
            second_cancel = service.cancel("run-cancel")
            await asyncio.wait_for(task, timeout=1)
            return service, events, first_cancel, second_cancel

        service, events, first_cancel, second_cancel = asyncio.run(scenario())

        self.assertTrue(first_cancel)
        self.assertFalse(second_cancel)
        self.assertEqual(events[-1]["type"], "swarm.cancelled")
        self.assertEqual(service.status("run-cancel")["status"], "cancelled")
        self.assertFalse(service.cancel("missing-run"))


async def _collect(iterator):
    return [event async for event in iterator]


if __name__ == "__main__":
    unittest.main()
