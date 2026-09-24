from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server  # noqa: E402
from swarm import SwarmOrchestrator  # noqa: E402
from swarm_api import SwarmService  # noqa: E402
from swarm_runtime import BeeRuntime  # noqa: E402


SAFE_ANSWERS = {
    "swarm_worthy": {"choice": "yes"},
    "task_type": {"choice": "research"},
    "needs_clarify": {"choice": "no"},
    "risk_level": {"choice": "low"},
    "evidence_heavy": {"choice": "yes"},
    "parallelizable": {"choice": "yes"},
}


async def degraded_jev(state, questions):
    return {"ok": False, "err": "offline-test"}


def parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


class SwarmServerApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app, base_url="http://localhost")
        self.original_checker = server._swarm_orchestrator.jev_checker
        self.original_service = server._swarm_service
        server._swarm_orchestrator.jev_checker = degraded_jev
        server._swarm_service = SwarmService(orchestrator=server._swarm_backend)
        with server._swarm_plans_lock:
            server._swarm_plans.clear()
            server._swarm_active_runs.clear()

    def tearDown(self):
        server._swarm_orchestrator.jev_checker = self.original_checker
        server._swarm_service = self.original_service
        self.client.close()

    def plan(self, *, high_risk: bool = False) -> dict:
        answers = {name: dict(value) for name, value in SAFE_ANSWERS.items()}
        if high_risk:
            answers["risk_level"] = {"choice": "high"}
            answers["task_type"] = {"choice": "action"}

        async def fake_jev(state, questions):
            self.assertEqual(set(questions), set(server.SWARM_PLAN_QUESTIONS))
            return {"ok": True, "answers": answers, "model": "mock-jev", "ms": 1}

        with (
            patch.object(server, "jev_ask", fake_jev),
            patch.object(server.permissions, "granted_scopes", return_value=["system"]),
            patch.object(server.machine_tools, "available_tool_specs", return_value=[]),
        ):
            response = self.client.post(
                "/api/swarm/plan",
                json={"goal": "并行调查并核验证据", "session_id": "session-test", "source": "test"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["plan"]

    def test_real_swarm_objects_are_wired_and_plan_status_is_available(self):
        self.assertIsInstance(server._swarm_runtime, BeeRuntime)
        self.assertIsInstance(server._swarm_orchestrator, SwarmOrchestrator)
        self.assertIsInstance(server._swarm_service, SwarmService)

        plan = self.plan(high_risk=True)
        self.assertEqual(plan["recipe"], "sensitive")
        self.assertTrue(plan["requires_confirmation"])
        self.assertEqual(plan["permissions_snapshot"]["granted_scopes"], ["system"])
        self.assertIn("permission_version", plan["permissions_snapshot"])

        response = self.client.get(f"/api/swarm/{plan['run_id']}")
        self.assertEqual(response.status_code, 200, response.text)
        status = response.json()
        self.assertTrue(status["found"])
        self.assertEqual(status["status"], "requires_confirmation")

    def test_confirmed_run_clears_gate_emits_audit_and_completes(self):
        plan = self.plan(high_risk=True)

        async def fake_runtime_run(bee_spec, goal, contract, input_capsules, emit, cancel_event, correction=None):
            await emit({
                "type": "bee.delta",
                "payload": {"bee_id": bee_spec.bee_id, "text": f"{bee_spec.bee_id}-ok"},
            })
            return {
                "text": f"{bee_spec.bee_id}-result",
                "reasoning": "",
                "tool_usage": [],
                "usage": {},
                "cache": {},
            }

        with (
            patch.object(server._swarm_runtime, "run", new=fake_runtime_run),
            patch.object(server.machine_tools, "available_tool_specs", return_value=[]),
            patch.object(server.permissions, "granted_scopes", return_value=[]),
        ):
            response = self.client.post(
                "/api/swarm/run",
                json={
                    "goal": plan["goal"],
                    "plan": plan,
                    "session_id": plan["session_id"],
                    "confirmed": True,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        events = parse_sse(response.text)
        kinds = [event["type"] for event in events]
        self.assertEqual(kinds[0], "swarm.confirmed")
        self.assertIn("swarm.plan", kinds)
        self.assertIn("swarm.done", kinds)
        self.assertNotIn("swarm.waiting_user", kinds)
        self.assertTrue(events[0]["payload"]["required_confirmation"])
        self.assertIn("high_risk", events[0]["payload"]["confirmation_reasons"])
        done = next(event for event in events if event["type"] == "swarm.done")
        self.assertEqual(done["payload"]["final_text"], "integrator-result")
        seqs = [event["seq"] for event in events]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))

        status = self.client.get(f"/api/swarm/{plan['run_id']}").json()
        self.assertEqual(status["status"], "completed")
        self.assertFalse(status["requires_confirmation"])

    def test_confirmation_does_not_bypass_required_clarification(self):
        plan = self.plan()
        plan.update({
            "needs_clarify": True,
            "needs_clarification": True,
            "requires_confirmation": True,
            "confirmation_reasons": ["needs_clarification"],
            "status": "requires_confirmation",
        })
        with server._swarm_plans_lock:
            server._swarm_plans[plan["run_id"]] = dict(plan)
        server._swarm_service._update_run(
            plan["run_id"], status="requires_confirmation", requires_confirmation=True
        )

        response = self.client.post(
            "/api/swarm/run",
            json={
                "goal": plan["goal"],
                "plan": plan,
                "session_id": plan["session_id"],
                "confirmed": True,
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        events = parse_sse(response.text)
        self.assertEqual([event["type"] for event in events], ["swarm.waiting_user"])
        self.assertEqual(
            self.client.get(f"/api/swarm/{plan['run_id']}").json()["status"],
            "requires_confirmation",
        )

    def test_cancel_endpoint_stops_active_run_and_status_becomes_cancelled(self):
        plan = self.plan()
        started = threading.Event()
        collected: list[dict] = []

        async def blocking_runtime_run(bee_spec, goal, contract, input_capsules, emit, cancel_event, correction=None):
            started.set()
            while not cancel_event.is_set():
                await asyncio.sleep(0.005)
            raise asyncio.CancelledError()

        current_permissions = server._swarm_permission_snapshot()
        run_plan, audit = server._prepare_swarm_run_plan(
            plan,
            confirmed=True,
            permissions_snapshot=current_permissions,
        )
        server._mark_swarm_confirmed(plan["run_id"], audit)

        def consume():
            async def scenario():
                async for event in server._swarm_service.stream_run(
                    run_plan,
                    run_plan["goal"],
                    run_plan["session_id"],
                    server._swarm_llm_config(),
                    server.jev_ask,
                    server.machine_tools.execute_tool,
                    asyncio.Event(),
                ):
                    collected.append(event)

            asyncio.run(scenario())

        with patch.object(server._swarm_runtime, "run", new=blocking_runtime_run):
            worker = threading.Thread(target=consume, daemon=True)
            worker.start()
            self.assertTrue(started.wait(2), "bee runtime did not start")
            response = self.client.post(f"/api/swarm/{plan['run_id']}/cancel", json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["cancel_requested"])
            worker.join(3)

        self.assertFalse(worker.is_alive(), "cancel did not stop the active swarm")
        self.assertEqual(collected[-1]["type"], "swarm.cancelled")
        status = self.client.get(f"/api/swarm/{plan['run_id']}").json()
        self.assertEqual(status["status"], "cancelled")

    def test_run_error_event_is_sanitized(self):
        plan = self.plan()

        async def failing_runtime_run(bee_spec, goal, contract, input_capsules, emit, cancel_event, correction=None):
            raise RuntimeError(r"C:\Users\SecretName\private\token.txt")

        with patch.object(server._swarm_runtime, "run", new=failing_runtime_run):
            response = self.client.post(
                "/api/swarm/run",
                json={"goal": plan["goal"], "plan": plan, "session_id": plan["session_id"], "confirmed": True},
            )

        self.assertEqual(response.status_code, 200, response.text)
        errors = [event for event in parse_sse(response.text) if event["type"] == "swarm.error"]
        self.assertTrue(errors)
        encoded = json.dumps(errors, ensure_ascii=False)
        self.assertNotIn(r"C:\Users\SecretName", encoded)
        self.assertNotIn("token.txt", encoded)


if __name__ == "__main__":
    unittest.main(verbosity=2)
