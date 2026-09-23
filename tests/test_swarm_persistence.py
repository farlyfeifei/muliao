from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from swarm_models import SwarmEvent, TaskContract
from swarm_persistence import SwarmLedger
from swarm_store import EventConflictError, SwarmStore


class SwarmLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "ledger.db"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def plan() -> dict[str, object]:
        return {
            "recipe": "research",
            "requires_confirmation": False,
            "stages": [{"id": "stage-1", "bees": ["investigator"]}],
        }

    @staticmethod
    def contract(task_id: str = "task-ledger") -> dict[str, object]:
        return {
            "task_id": task_id,
            "contract_rev": 1,
            "goal": "durably run the swarm",
            "recipe": "research",
            "required_constraints": [
                {"id": "C001", "text": "preserve evidence", "source": "test"}
            ],
            "allowed_tools": ["artifact.read"],
            "permission_snapshot": {
                "revision": 1,
                "grants": ["artifact.read"],
            },
            "created_at": None,
        }

    def begin(self, ledger: SwarmLedger, run_id: str = "run-ledger") -> TaskContract:
        contract = ledger.begin_run(
            run_id,
            "session-ledger",
            "durably run the swarm",
            self.plan(),
            self.contract(),
        )
        replayed = ledger.begin_run(
            run_id,
            "session-ledger",
            "durably run the swarm",
            self.plan(),
            self.contract(),
        )
        self.assertEqual(replayed, contract)
        return contract

    @staticmethod
    def event(
        *,
        run_id: str = "run-ledger",
        event_id: str,
        seq: int,
        event_type: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "event_id": event_id,
            "seq": seq,
            "run_id": run_id,
            "task_id": "task-ledger",
            "type": event_type,
            "payload": payload or {},
            "ts": float(seq),
        }

    def test_restart_reads_run_contract_and_events(self) -> None:
        first = SwarmLedger(path=self.db_path)
        contract = self.begin(first)
        self.assertTrue(contract.created_at)
        self.assertIsInstance(contract.required_constraints, tuple)
        first.persist_event(
            self.event(event_id="event-plan", seq=1, event_type="swarm.plan")
        )
        first.close()

        restarted = SwarmLedger(path=self.db_path)
        try:
            status = restarted.status("run-ledger")
            self.assertIsNotNone(status)
            assert status is not None
            self.assertEqual(status["session_id"], "session-ledger")
            self.assertEqual(status["plan"], self.plan())
            self.assertEqual(status["status"], "running")
            self.assertEqual(
                restarted.store.get_contract("task-ledger", 1),
                contract,
            )
            self.assertEqual(
                [event.event_id for event in restarted.events("run-ledger")],
                ["event-plan"],
            )
        finally:
            restarted.close()

    def test_events_are_contiguous_filterable_idempotent_and_persisted_before_publish(self) -> None:
        ledger = SwarmLedger(path=self.db_path)
        try:
            self.begin(ledger)
            published: list[SwarmEvent] = []

            def publish(event: SwarmEvent) -> None:
                self.assertEqual(ledger.events(event.run_id)[-1], event)
                published.append(event)

            first = ledger.persist_event(
                self.event(event_id="event-1", seq=1, event_type="swarm.plan"),
                publish=publish,
            )
            second = ledger.persist_event(
                SwarmEvent(
                    event_id="event-2",
                    seq=2,
                    run_id="run-ledger",
                    task_id="task-ledger",
                    type="bee.start",
                    payload={"bee_id": "investigator"},
                    ts=2.0,
                ),
                publish=publish,
            )
            replayed = ledger.persist_event(first, publish=publish)

            self.assertEqual(replayed, first)
            self.assertEqual([event.seq for event in ledger.events("run-ledger")], [1, 2])
            self.assertEqual(ledger.events("run-ledger", after_seq=1), [second])
            self.assertEqual([event.event_id for event in published], ["event-1", "event-2"])
            with self.assertRaises(EventConflictError):
                ledger.persist_event(
                    self.event(event_id="event-4", seq=4, event_type="bee.start")
                )
        finally:
            ledger.close()

    def test_terminal_events_synchronize_run_status(self) -> None:
        cases = (
            ("swarm.done", "completed"),
            ("swarm.cancelled", "cancelled"),
            ("swarm.error", "failed"),
        )
        for index, (event_type, expected_status) in enumerate(cases, start=1):
            with self.subTest(event_type=event_type):
                run_id = f"run-terminal-{index}"
                ledger = SwarmLedger(path=self.db_path)
                ledger.begin_run(
                    run_id,
                    f"session-{index}",
                    "durably run the swarm",
                    self.plan(),
                    self.contract(task_id=f"task-terminal-{index}"),
                )
                event = {
                    "event_id": f"event-terminal-{index}",
                    "seq": 1,
                    "run_id": run_id,
                    "task_id": f"task-terminal-{index}",
                    "type": event_type,
                    "payload": {"status": expected_status},
                    "ts": 1.0,
                }
                ledger.persist_event(event)
                state = ledger.status(run_id)
                assert state is not None
                self.assertEqual(state["status"], expected_status)
                self.assertEqual(state["terminal_event_id"], event["event_id"])
                ledger.close()

    def test_recover_interrupted_runs_after_restart(self) -> None:
        first = SwarmLedger(path=self.db_path)
        self.begin(first)
        first.close()

        restarted = SwarmLedger(path=self.db_path)
        try:
            self.assertEqual(restarted.recover_interrupted_runs(), ["run-ledger"])
            state = restarted.status("run-ledger")
            assert state is not None
            self.assertEqual(state["status"], "interrupted")
            self.assertEqual(restarted.recover_interrupted_runs(), [])
        finally:
            restarted.close()

    def test_injected_store_is_used_and_not_closed_by_ledger(self) -> None:
        store = SwarmStore(":memory:")
        ledger = SwarmLedger(store)
        self.begin(ledger)
        ledger.close()
        self.assertIsNotNone(store.get_run("run-ledger"))
        store.close()

    def test_task_contract_with_blank_created_at_is_completed(self) -> None:
        ledger = SwarmLedger(path=self.db_path)
        try:
            contract = TaskContract(
                task_id="task-blank-time",
                contract_rev=1,
                goal="durably run the swarm",
                recipe="research",
                created_at="",
            )
            persisted = ledger.begin_run(
                "run-blank-time",
                "session-blank-time",
                contract.goal,
                self.plan(),
                contract,
            )
            self.assertTrue(persisted.created_at)
            self.assertNotEqual(persisted, contract)
        finally:
            ledger.close()

    def test_invalid_plan_does_not_leave_orphan_contract(self) -> None:
        ledger = SwarmLedger(path=self.db_path)
        contract_data = self.contract(task_id="task-invalid-plan")
        try:
            with self.assertRaisesRegex(ValueError, "unknown recipe"):
                ledger.begin_run(
                    "run-invalid-plan",
                    "session-invalid-plan",
                    "durably run the swarm",
                    {"recipe": "not-a-recipe"},
                    contract_data,
                )
            self.assertIsNone(ledger.store.get_contract("task-invalid-plan", 1))
            self.assertIsNone(ledger.status("run-invalid-plan"))
        finally:
            ledger.close()

    def test_confirm_run_preserves_plan_and_continues_sequence(self) -> None:
        ledger = SwarmLedger(path=self.db_path)
        try:
            plan = dict(self.plan(), requires_confirmation=True)
            contract = ledger.begin_run(
                "run-confirm",
                "session-confirm",
                "durably run the swarm",
                plan,
                self.contract(task_id="task-confirm"),
            )
            ledger.persist_event({
                "event_id": "event-confirm-wait",
                "seq": 1,
                "run_id": "run-confirm",
                "task_id": contract.task_id,
                "type": "swarm.waiting_user",
                "payload": {"reason": "high_risk_confirmation_required"},
                "ts": 1.0,
            })
            before = ledger.status("run-confirm")
            assert before is not None
            original_plan = before["plan"]
            confirmed = ledger.confirm_run(
                "run-confirm",
                {"confirmed": True, "confirmed_at": "2026-09-23T00:00:00Z"},
            )
            self.assertEqual(confirmed["status"], "running")
            self.assertEqual(confirmed["plan"], original_plan)
            self.assertTrue(confirmed["confirmation"]["confirmed"])
            self.assertEqual(ledger.next_seq("run-confirm"), 2)
        finally:
            ledger.close()

    def test_exchange_handoff_builds_verifies_and_persists_real_capsule(self) -> None:
        ledger = SwarmLedger(path=self.db_path)
        try:
            contract = self.begin(ledger)
            artifact_bytes = b"verified handoff evidence\n"
            artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
            result = {
                "summary": "investigation completed",
                "facts": [
                    {
                        "fact_id": "fact-handoff",
                        "state": "observed",
                        "value": "verified handoff evidence",
                        "source_ref": "artifact://artifact-handoff#L1",
                        "source_sha256": artifact_hash,
                    }
                ],
                "artifacts": [
                    {
                        "artifact_id": "artifact-handoff",
                        "uri": "memory://artifact-handoff",
                        "mime": "text/plain",
                        "sha256": artifact_hash,
                    }
                ],
            }
            loader_calls: list[str] = []

            def artifact_loader(artifact: dict[str, object]) -> bytes:
                loader_calls.append(str(artifact["artifact_id"]))
                return artifact_bytes

            capsule, ack = ledger.exchange_handoff(
                run_id="run-ledger",
                stage_id="stage-1",
                contract=contract,
                from_bee="investigator",
                to_bee="verifier",
                result=result,
                current_permission_snapshot=contract.permission_snapshot,
                receiver_permissions={"artifact.read"},
                artifact_loader=artifact_loader,
                attempt=2,
            )

            self.assertEqual(capsule.payload, result)
            self.assertEqual(capsule.from_bee, "investigator")
            self.assertEqual(capsule.to_bee, "verifier")
            self.assertEqual(ack.status, "accepted")
            self.assertEqual(loader_calls, ["artifact-handoff"])
            row = ledger.store.get_capsule(capsule.capsule_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["run_id"], "run-ledger")
            self.assertEqual(row["stage_id"], "stage-1")
            self.assertEqual(row["attempt"], 2)
            self.assertEqual(row["ack_status"], "accepted")

            duplicate_capsule, duplicate_ack = ledger.exchange_handoff(
                run_id="run-ledger",
                stage_id="stage-1",
                contract=contract,
                from_bee="investigator",
                to_bee="verifier",
                result=result,
                current_permission_snapshot=contract.permission_snapshot,
                receiver_permissions={"artifact.read"},
                artifact_loader=artifact_loader,
                attempt=2,
            )
            self.assertEqual(duplicate_capsule, capsule)
            self.assertTrue(duplicate_ack.duplicate)
        finally:
            ledger.close()


if __name__ == "__main__":
    unittest.main()
