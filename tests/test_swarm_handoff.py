from __future__ import annotations

import asyncio
import copy
import unittest
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import swarm
from swarm_models import CapsuleAck
from swarm_persistence import SwarmLedger
from swarm_store import SwarmStore


async def collect(iterator):
    return [event async for event in iterator]


async def accepting_checker(_state, questions):
    answers: dict[str, Any] = {}
    for key in questions:
        if key.startswith("correction_action_"):
            answers[key] = "accept"
        elif key.startswith("conflict_present_"):
            answers[key] = False
        else:
            answers[key] = True
    return {"ok": True, "answers": answers}


class SwarmHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def runner(self, bee_id, context):
        return {"summary": f"{bee_id} complete", "inputs_seen": len(context["inputs"])}

    @staticmethod
    def exchange_with(status: str, calls: list):
        def exchange(*, capsule, **_kwargs):
            calls.append(capsule)
            return capsule, CapsuleAck(
                message_id=capsule.message_id,
                capsule_id=capsule.capsule_id,
                status=status,
                checked_sha256="a" * 64,
            )

        return exchange

    async def test_accepted_handoff_reaches_next_bee(self):
        calls: list = []
        seen: dict[str, list[dict[str, Any]]] = {}

        async def runner(bee_id, context):
            seen[bee_id] = context["inputs"]
            return {"summary": f"{bee_id} complete"}

        events = await collect(
            swarm.orchestrate(
                "build a report",
                runner,
                accepting_checker,
                recipe="build",
                run_id="run-accepted",
                handoff_exchange=self.exchange_with("accepted", calls),
            )
        )

        self.assertEqual(events[-1]["type"], "swarm.done")
        self.assertTrue(calls)
        self.assertEqual(len(seen["investigator"]), 1)
        self.assertEqual(seen["investigator"][0]["to_bee"], "investigator")
        self.assertTrue(
            all(
                event["payload"]["ack"] == "accepted"
                and event["payload"]["ack_status"] == "accepted"
                and event["payload"]["acknowledgement"]["status"] == "accepted"
                for event in events
                if event["type"] == "handoff.ack"
            )
        )

    async def test_need_context_waits_and_blocks_downstream(self):
        calls: list = []
        started: list[str] = []

        async def runner(bee_id, context):
            started.append(bee_id)
            return {"summary": f"{bee_id} complete"}

        events = await collect(
            swarm.orchestrate(
                "build a report",
                runner,
                accepting_checker,
                recipe="build",
                run_id="run-context",
                handoff_exchange=self.exchange_with("need_context", calls),
            )
        )

        self.assertEqual(events[-1]["type"], "swarm.waiting_user")
        self.assertEqual(events[-1]["payload"]["reason"], "handoff_need_context")
        self.assertEqual(started, ["compiler"])
        self.assertNotIn("swarm.done", [event["type"] for event in events])

    async def test_forbidden_is_explicit_error_and_blocks_downstream(self):
        calls: list = []
        started: list[str] = []

        async def runner(bee_id, context):
            started.append(bee_id)
            return {"summary": f"{bee_id} complete"}

        events = await collect(
            swarm.orchestrate(
                "build a report",
                runner,
                accepting_checker,
                recipe="build",
                run_id="run-forbidden",
                handoff_exchange=self.exchange_with("forbidden", calls),
            )
        )

        self.assertEqual(events[-1]["type"], "swarm.error")
        self.assertEqual(events[-1]["payload"]["code"], "handoff_forbidden")
        self.assertEqual(started, ["compiler"])

    async def test_positional_only_capsule_exchange_is_supported(self):
        seen: list = []

        def exchange(capsule, /):
            seen.append(capsule)
            return capsule, CapsuleAck(
                message_id=capsule.message_id,
                capsule_id=capsule.capsule_id,
                status="accepted",
                checked_sha256="a" * 64,
            )

        events = await collect(
            swarm.orchestrate(
                "build a report",
                self.runner,
                accepting_checker,
                recipe="build",
                run_id="run-positional-only",
                handoff_exchange=exchange,
            )
        )

        self.assertTrue(seen)
        self.assertEqual(events[-1]["type"], "swarm.done")

    async def test_unsupported_required_exchange_parameter_is_explicit(self):
        def exchange(*, capsule, unsupported):
            return capsule, CapsuleAck(
                message_id=capsule.message_id,
                capsule_id=capsule.capsule_id,
                status="accepted",
            )

        events = await collect(
            swarm.orchestrate(
                "build a report",
                self.runner,
                accepting_checker,
                recipe="build",
                run_id="run-unsupported-exchange-parameter",
                handoff_exchange=exchange,
            )
        )

        self.assertEqual(events[-1]["type"], "swarm.error")
        self.assertEqual(events[-1]["payload"]["code"], "orchestrator_error")
        self.assertIn("unsupported required parameters", events[-1]["payload"]["message"])

    async def test_foreign_capsule_is_rejected_even_with_accepted_ack(self):
        def exchange(*, capsule, **_kwargs):
            foreign = copy.deepcopy(capsule.to_dict())
            foreign["task_id"] = "tsk_foreign"
            return foreign, CapsuleAck(
                message_id=capsule.message_id,
                capsule_id=capsule.capsule_id,
                status="accepted",
                checked_sha256="a" * 64,
            )

        events = await collect(
            swarm.orchestrate(
                "build a report",
                self.runner,
                accepting_checker,
                recipe="build",
                run_id="run-foreign-capsule",
                handoff_exchange=exchange,
            )
        )

        self.assertEqual(events[-1]["type"], "swarm.error")
        self.assertEqual(events[-1]["payload"]["code"], "orchestrator_error")
        self.assertIn("foreign capsule", events[-1]["payload"]["message"])

    async def test_changed_payload_capsule_is_rejected(self):
        def exchange(*, capsule, **_kwargs):
            changed = capsule.to_dict()
            changed["payload"] = {"summary": "foreign result"}
            return changed, CapsuleAck(
                message_id=capsule.message_id,
                capsule_id=capsule.capsule_id,
                status="accepted",
                checked_sha256="a" * 64,
            )

        events = await collect(
            swarm.orchestrate(
                "build a report",
                self.runner,
                accepting_checker,
                recipe="build",
                run_id="run-changed-payload",
                handoff_exchange=exchange,
            )
        )

        self.assertEqual(events[-1]["type"], "swarm.error")
        self.assertIn("body", events[-1]["payload"]["message"])

    async def test_mismatched_ack_ids_are_rejected(self):
        def exchange(*, capsule, **_kwargs):
            return capsule, CapsuleAck(
                message_id="msg_foreign",
                capsule_id=capsule.capsule_id,
                status="accepted",
                checked_sha256="a" * 64,
            )

        events = await collect(
            swarm.orchestrate(
                "build a report",
                self.runner,
                accepting_checker,
                recipe="build",
                run_id="run-mismatched-ack",
                handoff_exchange=exchange,
            )
        )

        self.assertEqual(events[-1]["type"], "swarm.error")
        self.assertIn("message_id does not match", events[-1]["payload"]["message"])

    async def test_task_identity_includes_scope_and_acceptance_tests(self):
        base = swarm._plan_for_recipe("build", source="test")
        scoped = copy.deepcopy(base)
        scoped["scope"] = {"included": ["src"], "excluded": []}
        tested = copy.deepcopy(base)
        tested["acceptance_tests"] = ["tests must pass"]

        base_contract = swarm.build_contract("build a report", base, None)
        scoped_contract = swarm.build_contract("build a report", scoped, None)
        tested_contract = swarm.build_contract("build a report", tested, None)

        self.assertNotEqual(base_contract["task_id"], scoped_contract["task_id"])
        self.assertNotEqual(base_contract["task_id"], tested_contract["task_id"])
        self.assertNotEqual(scoped_contract["task_id"], tested_contract["task_id"])

    async def test_task_identity_includes_risk_and_final_budgets(self):
        base = swarm._plan_for_recipe("research", source="test")
        higher_risk = copy.deepcopy(base)
        higher_risk["risk_level"] = 5.0
        lower_parallel = copy.deepcopy(base)
        lower_parallel["max_parallel"] = 1

        base_contract = swarm.build_contract("research a report", base, None)
        risk_contract = swarm.build_contract("research a report", higher_risk, None)
        budget_contract = swarm.build_contract("research a report", lower_parallel, None)

        self.assertNotEqual(base_contract["task_id"], risk_contract["task_id"])
        self.assertNotEqual(base_contract["task_id"], budget_contract["task_id"])
        self.assertNotEqual(base_contract["risk_gate"], risk_contract["risk_gate"])
        self.assertNotEqual(
            base_contract["budgets"]["max_parallel"],
            budget_contract["budgets"]["max_parallel"],
        )

    async def test_parallel_recipients_receive_only_their_capsules(self):
        seen: dict[str, list[dict[str, Any]]] = {}

        async def runner(bee_id, context):
            seen[bee_id] = context["inputs"]
            return {"summary": f"{bee_id} complete"}

        events = await collect(
            swarm.orchestrate(
                "research a report",
                runner,
                accepting_checker,
                recipe="research",
                run_id="run-recipient-isolation",
                handoff_exchange=self.exchange_with("accepted", []),
            )
        )

        self.assertEqual(events[-1]["type"], "swarm.done")
        for bee_id in ("investigator", "extractor"):
            self.assertTrue(seen[bee_id])
            self.assertTrue(
                all(capsule["to_bee"] == bee_id for capsule in seen[bee_id])
            )
            self.assertFalse(
                any(capsule["to_bee"] != bee_id for capsule in seen[bee_id])
            )

    async def test_jev_gate_recoverable_actions_emit_recoverable_events(self):
        for action, expected in (
            ("need_context", "swarm.waiting_user"),
            ("escalate", "swarm.waiting_user"),
            ("pause", "swarm.paused"),
        ):
            used = False

            async def checker(_state, questions, *, selected=action):
                nonlocal used
                answers: dict[str, Any] = {}
                for key in questions:
                    if key.startswith("correction_action_") and not used:
                        answers[key] = selected
                        used = True
                    elif key.startswith("correction_action_"):
                        answers[key] = "accept"
                    elif key.startswith("conflict_present_"):
                        answers[key] = False
                    else:
                        answers[key] = True
                return {"ok": True, "answers": answers}

            events = await collect(
                swarm.orchestrate(
                    "build a report",
                    self.runner,
                    checker,
                    recipe="build",
                    run_id=f"run-jev-gate-{action}",
                )
            )

            self.assertEqual(events[-1]["type"], expected)
            self.assertNotEqual(events[-1]["type"], "swarm.error")

    async def test_cancel_interrupts_external_handoff(self):
        cancel_event = asyncio.Event()
        entered = asyncio.Event()

        async def exchange(*, capsule, **_kwargs):
            entered.set()
            await asyncio.sleep(10)
            return capsule, CapsuleAck(
                message_id=capsule.message_id,
                capsule_id=capsule.capsule_id,
                status="accepted",
            )

        async def trigger():
            await entered.wait()
            cancel_event.set()

        trigger_task = asyncio.create_task(trigger())
        events = await asyncio.wait_for(
            collect(
                swarm.orchestrate(
                    "build a report",
                    self.runner,
                    accepting_checker,
                    recipe="build",
                    run_id="run-cancel-handoff",
                    handoff_exchange=exchange,
                    cancel_event=cancel_event,
                )
            ),
            timeout=2,
        )
        await trigger_task

        self.assertEqual(events[-1]["type"], "swarm.cancelled")
        self.assertNotIn("swarm.done", [event["type"] for event in events])

    async def test_confirmed_run_resumes_without_reemitting_plan_or_contract(self):
        store = SwarmStore(":memory:")
        ledger = SwarmLedger(store)
        calls: list[str] = []

        async def runner(bee_id, context):
            calls.append(bee_id)
            return {"summary": f"{bee_id} complete"}

        try:
            first = await collect(
                swarm.orchestrate(
                    "delete the project",
                    runner,
                    accepting_checker,
                    recipe="sensitive",
                    run_id="run-durable-confirmation",
                    confirmed=False,
                    ledger=ledger,
                )
            )
            resumed = await collect(
                swarm.orchestrate(
                    "delete the project",
                    runner,
                    accepting_checker,
                    recipe="sensitive",
                    run_id="run-durable-confirmation",
                    confirmed=True,
                    ledger=ledger,
                )
            )

            self.assertEqual(first[-1]["type"], "swarm.waiting_user")
            self.assertEqual(resumed[0]["type"], "swarm.confirmed")
            self.assertNotIn("swarm.plan", [event["type"] for event in resumed])
            self.assertNotIn("contract.created", [event["type"] for event in resumed])
            self.assertEqual(resumed[-1]["type"], "swarm.done")
            self.assertEqual(
                resumed[0]["seq"],
                first[-1]["seq"] + 1,
            )
            self.assertTrue(calls)
        finally:
            store.close()

    async def test_done_exposes_string_integrator_final_text(self):
        async def runner(bee_id, context):
            if bee_id == "integrator":
                return "final integrated answer"
            return {"summary": f"{bee_id} complete"}

        events = await collect(
            swarm.orchestrate(
                "build a report",
                runner,
                accepting_checker,
                recipe="build",
                run_id="run-string-final-text",
            )
        )

        self.assertEqual(events[-1]["type"], "swarm.done")
        self.assertEqual(events[-1]["payload"]["final_text"], "final integrated answer")

    async def test_duplicate_exchange_is_deterministic(self):
        store = SwarmStore(":memory:")
        ledger = SwarmLedger(store)
        try:
            plan = swarm._plan_for_recipe("build", source="test")
            contract = swarm.TaskContract.from_dict(
                swarm.build_contract("build a report", plan, {"allowed_tools": []})
            )
            ledger.begin_run(
                "run-duplicate",
                "session-duplicate",
                contract.goal,
                plan,
                contract,
            )
            exchange = {
                "run_id": "run-duplicate",
                "stage_id": "stage_1",
                "contract": contract,
                "from_bee": "compiler",
                "to_bee": "investigator",
                "result": {"summary": "compiler complete"},
                "current_permission_snapshot": contract.permission_snapshot,
                "receiver_permissions": contract.allowed_tools,
                "attempt": 1,
            }

            first_capsule, first_ack = ledger.exchange_handoff(**exchange)
            second_capsule, second_ack = ledger.exchange_handoff(**exchange)
            with store.connection() as connection:
                capsule_count = connection.execute("SELECT COUNT(*) FROM capsules").fetchone()[0]

            self.assertEqual(first_capsule.message_id, second_capsule.message_id)
            self.assertEqual(first_capsule.capsule_id, second_capsule.capsule_id)
            self.assertFalse(first_ack.duplicate)
            self.assertTrue(second_ack.duplicate)
            self.assertEqual(capsule_count, 1)
        finally:
            store.close()

    async def test_events_are_persisted_before_they_are_yielded(self):
        store = SwarmStore(":memory:")
        ledger = SwarmLedger(store)
        try:
            observed: list[dict[str, Any]] = []
            plan = swarm._plan_for_recipe("build", source="test")
            plan["session_id"] = "session-origin"
            async for event in swarm.orchestrate(
                "build a report",
                self.runner,
                accepting_checker,
                plan=plan,
                run_id="run-persisted-events",
                ledger=ledger,
            ):
                persisted = ledger.events("run-persisted-events")
                self.assertEqual(len(persisted), event["seq"])
                self.assertEqual(persisted[-1].event_id, event["event_id"])
                observed.append(event)

            self.assertEqual(observed[-1]["type"], "swarm.done")
            status = ledger.status("run-persisted-events")
            self.assertIsNotNone(status)
            assert status is not None
            self.assertEqual(status["session_id"], "session-origin")
            self.assertEqual(
                [event.to_dict() for event in ledger.events("run-persisted-events")],
                observed,
            )
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
