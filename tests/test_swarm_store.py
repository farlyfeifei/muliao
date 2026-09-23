from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest


from swarm_models import CapsuleAck, SwarmEvent, TaskContract, WorkCapsule
from swarm_store import EventConflictError, SwarmStore


class SwarmStoreV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = SwarmStore(self.root / "swarm.db")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def contract(self, task_id: str) -> TaskContract:
        return TaskContract(
            task_id=task_id,
            contract_rev=1,
            goal=f"goal for {task_id}",
            recipe="research",
            permission_snapshot={"revision": 1},
        )

    def create_run(self, run_id: str, task_id: str) -> None:
        self.store.put_contract(self.contract(task_id))
        self.store.create_run(
            run_id,
            task_id,
            "research",
            1,
            session_id=f"session-{run_id}",
            plan={"recipe": "research"},
        )

    def test_schema_v2_and_memory_store_survive_multiple_connections(self) -> None:
        memory = SwarmStore(":memory:")
        try:
            self.assertEqual(memory.schema_version(), 2)
            self.assertIn("contracts", memory.table_names())
            contract = self.contract("task-memory")
            self.assertTrue(memory.put_contract(contract))
            self.assertIsNotNone(memory.get_contract(contract.task_id, 1))
        finally:
            memory.close()

    def test_each_run_starts_at_sequence_one_while_db_sequence_is_global(self) -> None:
        self.create_run("run-a", "task-a")
        self.create_run("run-b", "task-b")
        a = self.store.append_event(
            event_id="event-a",
            run_id="run-a",
            task_id="task-a",
            event_type="swarm.plan",
        )
        b = self.store.append_event(
            event_id="event-b",
            run_id="run-b",
            task_id="task-b",
            event_type="swarm.plan",
        )
        self.assertEqual(a.seq, 1)
        self.assertEqual(b.seq, 1)
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT db_seq, run_id, run_seq FROM events ORDER BY db_seq"
            ).fetchall()
        self.assertEqual([row["db_seq"] for row in rows], [1, 2])
        self.assertEqual([row["run_seq"] for row in rows], [1, 1])

    def test_event_replay_is_idempotent_and_conflicting_sequence_is_rejected(self) -> None:
        self.create_run("run-events", "task-events")
        event = SwarmEvent(
            event_id="event-one",
            seq=1,
            run_id="run-events",
            task_id="task-events",
            type="swarm.plan",
            payload={"ok": True},
            ts=1.0,
        )
        first = self.store.append_event(event)
        second = self.store.append_event(event)
        self.assertEqual(first, second)
        self.assertEqual(self.store.list_events("run-events"), [event])
        with self.assertRaises(EventConflictError):
            self.store.append_event(
                SwarmEvent(
                    event_id="event-other",
                    seq=1,
                    run_id="run-events",
                    task_id="task-events",
                    type="bee.start",
                    payload={},
                    ts=2.0,
                )
            )

    def test_event_task_id_must_match_run(self) -> None:
        self.create_run("run-task", "task-real")
        with self.assertRaises(EventConflictError):
            self.store.append_event(
                SwarmEvent(
                    event_id="event-wrong-task",
                    seq=1,
                    run_id="run-task",
                    task_id="task-wrong",
                    type="swarm.plan",
                    payload={},
                    ts=1.0,
                )
            )

    def test_capsule_and_ack_are_written_atomically_and_duplicate_reuses_ack_time(self) -> None:
        contract = self.contract("task-capsule")
        self.store.put_contract(contract)
        self.store.create_run("run-capsule", contract.task_id, "research", 1)
        capsule = WorkCapsule(
            message_id="message-one",
            capsule_id="capsule-one",
            task_id=contract.task_id,
            contract_rev=1,
            from_bee="investigator",
            to_bee="verifier",
            payload={"summary": "done"},
            permission_snapshot=contract.permission_snapshot,
        )
        ack = CapsuleAck(
            message_id=capsule.message_id,
            capsule_id=capsule.capsule_id,
            status="accepted",
            checked_sha256="a" * 64,
            checked_at="2026-09-23T00:00:00Z",
        )
        inserted, persisted, row = self.store.record_capsule_ack(
            capsule,
            ack,
            run_id="run-capsule",
            stage_id="stage-1",
        )
        self.assertTrue(inserted)
        self.assertEqual(persisted, ack)
        self.assertEqual(row["ack_status"], "accepted")
        self.assertIsNotNone(row["ack_json"])
        self.assertEqual(row["status"], "current")

        inserted, duplicate, row = self.store.record_capsule_ack(
            capsule,
            CapsuleAck(
                message_id=capsule.message_id,
                capsule_id=capsule.capsule_id,
                status="accepted",
                checked_sha256="b" * 64,
            ),
            run_id="run-capsule",
            stage_id="stage-1",
        )
        self.assertFalse(inserted)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.checked_at, ack.checked_at)
        self.assertEqual(duplicate.checked_sha256, ack.checked_sha256)
        self.assertIsNotNone(row["ack_json"])

    def test_rejected_capsule_is_not_a_current_dependency(self) -> None:
        contract = self.contract("task-reject")
        self.store.put_contract(contract)
        capsule = WorkCapsule(
            message_id="message-reject",
            capsule_id="capsule-reject",
            task_id=contract.task_id,
            contract_rev=1,
            from_bee="builder",
            to_bee="verifier",
            permission_snapshot=contract.permission_snapshot,
        )
        ack = CapsuleAck(
            message_id=capsule.message_id,
            capsule_id=capsule.capsule_id,
            status="forbidden",
            checked_sha256="c" * 64,
        )
        self.store.record_capsule_ack(capsule, ack)
        self.assertEqual(
            self.store.dependency_states([capsule.capsule_id])[capsule.capsule_id],
            "forbidden",
        )

    def test_terminal_event_is_atomic_and_rejects_late_events(self) -> None:
        self.create_run("run-terminal", "task-terminal")
        done = SwarmEvent(
            event_id="event-terminal-done",
            seq=1,
            run_id="run-terminal",
            task_id="task-terminal",
            type="swarm.done",
            payload={"status": "completed"},
            ts=1.0,
        )
        persisted, inserted = self.store.record_event(done, run_status="completed")
        self.assertTrue(inserted)
        self.assertEqual(persisted, done)
        state = self.store.get_run("run-terminal")
        assert state is not None
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["terminal_event_id"], done.event_id)

        replayed, inserted = self.store.record_event(done, run_status="completed")
        self.assertFalse(inserted)
        self.assertEqual(replayed, done)
        with self.assertRaisesRegex(EventConflictError, "already terminal"):
            self.store.record_event(
                SwarmEvent(
                    event_id="event-terminal-late",
                    seq=2,
                    run_id="run-terminal",
                    task_id="task-terminal",
                    type="swarm.error",
                    payload={"error": "late"},
                    ts=2.0,
                ),
                run_status="failed",
            )
        state = self.store.get_run("run-terminal")
        assert state is not None
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["terminal_event_id"], done.event_id)

    def test_automatic_sequences_are_allocated_inside_one_transaction(self) -> None:
        self.create_run("run-concurrent", "task-concurrent")

        def append(index: int) -> int:
            return self.store.append_event(
                event_id=f"event-concurrent-{index}",
                run_id="run-concurrent",
                task_id="task-concurrent",
                event_type="bee.delta",
                payload={"index": index},
                ts=float(index),
            ).seq

        with ThreadPoolExecutor(max_workers=8) as executor:
            sequences = list(executor.map(append, range(24)))
        self.assertEqual(sorted(sequences), list(range(1, 25)))
        self.assertEqual(
            [event.seq for event in self.store.list_events("run-concurrent")],
            list(range(1, 25)),
        )

    def test_dependency_states_enforce_task_ownership_and_fact_expiry(self) -> None:
        expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        from swarm_models import ArtifactRecord, FactRecord

        self.store.put_fact(
            FactRecord(
                fact_id="fact-expired",
                task_id="task-owner",
                state="verified",
                value="old",
                expires_at=expired,
            )
        )
        self.store.put_artifact(
            ArtifactRecord(
                artifact_id="artifact-owned",
                task_id="task-owner",
                uri="memory://owned",
                mime="text/plain",
                sha256="a" * 64,
            )
        )
        states = self.store.dependency_states(
            ["fact-expired", "artifact-owned"],
            task_id="task-other",
        )
        self.assertEqual(states["fact-expired"], "forbidden")
        self.assertEqual(states["artifact-owned"], "forbidden")
        self.assertEqual(
            self.store.dependency_states(["fact-expired"], task_id="task-owner")["fact-expired"],
            "stale",
        )

    def test_invalidation_propagates_through_derived_facts(self) -> None:
        from swarm_models import ArtifactRecord, FactRecord

        self.store.put_fact(
            FactRecord(
                fact_id="fact-root",
                task_id="task-graph",
                state="verified",
                value=1,
            )
        )
        self.store.put_fact(
            FactRecord(
                fact_id="fact-derived",
                task_id="task-graph",
                state="inferred",
                value=2,
                depends_on=("fact-root",),
            )
        )
        self.store.put_artifact(
            ArtifactRecord(
                artifact_id="artifact-derived",
                task_id="task-graph",
                uri="memory://derived",
                mime="application/json",
                sha256="b" * 64,
                depends_on=("fact-derived",),
            )
        )
        affected = self.store.invalidate_fact("fact-root")
        self.assertEqual(affected["facts"], ["fact-derived", "fact-root"])
        self.assertEqual(self.store.get_fact("fact-derived")["state"], "invalidated")
        self.assertEqual(self.store.get_artifact("artifact-derived")["status"], "stale")

    def test_capsule_replay_cannot_change_handoff_metadata(self) -> None:
        contract = self.contract("task-metadata")
        self.store.put_contract(contract)
        self.store.create_run("run-metadata", contract.task_id, "research", 1)
        capsule = WorkCapsule(
            message_id="message-metadata",
            capsule_id="capsule-metadata",
            task_id=contract.task_id,
            contract_rev=1,
            from_bee="compiler",
            to_bee="investigator",
            permission_snapshot=contract.permission_snapshot,
        )
        ack = CapsuleAck(
            message_id=capsule.message_id,
            capsule_id=capsule.capsule_id,
            status="accepted",
            checked_sha256="d" * 64,
        )
        self.store.record_capsule_ack(
            capsule,
            ack,
            run_id="run-metadata",
            stage_id="stage-1",
            attempt=1,
        )
        from swarm_store import DuplicateMessageError

        with self.assertRaises(DuplicateMessageError):
            self.store.record_capsule_ack(
                capsule,
                ack,
                run_id="run-metadata",
                stage_id="stage-other",
                attempt=1,
            )
        with self.assertRaises(DuplicateMessageError):
            self.store.record_capsule_ack(
                capsule,
                ack,
                run_id="run-metadata",
                stage_id="stage-1",
                attempt=2,
            )

    def test_v1_events_are_migrated_to_run_local_sequences(self) -> None:
        path = self.root / "legacy.db"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            PRAGMA foreign_keys = OFF;
            CREATE TABLE contracts (
                task_id TEXT NOT NULL,
                contract_rev INTEGER NOT NULL,
                body_json TEXT NOT NULL,
                body_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(task_id, contract_rev)
            );
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                recipe_id TEXT NOT NULL,
                status TEXT NOT NULL,
                contract_rev INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                ts REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE capsules (
                capsule_id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL,
                contract_rev INTEGER NOT NULL,
                from_bee TEXT NOT NULL,
                to_bee TEXT NOT NULL,
                body_json TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                ack_status TEXT,
                ack_json TEXT,
                status TEXT NOT NULL DEFAULT 'current',
                depends_on_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            INSERT INTO contracts VALUES ('task-a', 1, '{}', 'h', 't');
            INSERT INTO contracts VALUES ('task-b', 1, '{}', 'h', 't');
            INSERT INTO runs VALUES ('run-a', 'task-a', 'research', 'done', 1, 't', 't');
            INSERT INTO runs VALUES ('run-b', 'task-b', 'research', 'done', 1, 't', 't');
            INSERT INTO events(event_id, run_id, task_id, event_type, payload_json, ts, created_at)
                VALUES ('event-a1', 'run-a', 'task-a', 'swarm.plan', '{}', 1, 't');
            INSERT INTO events(event_id, run_id, task_id, event_type, payload_json, ts, created_at)
                VALUES ('event-b1', 'run-b', 'task-b', 'swarm.plan', '{}', 1, 't');
            INSERT INTO events(event_id, run_id, task_id, event_type, payload_json, ts, created_at)
                VALUES ('event-a2', 'run-a', 'task-a', 'swarm.done', '{}', 2, 't');
            """
        )
        connection.commit()
        connection.close()

        migrated = SwarmStore(path)
        try:
            self.assertEqual(migrated.schema_version(), 2)
            self.assertEqual([event.seq for event in migrated.list_events("run-a")], [1, 2])
            self.assertEqual([event.seq for event in migrated.list_events("run-b")], [1])
            with migrated.connection() as check:
                foreign_keys = check.execute("PRAGMA foreign_key_list(capsules)").fetchall()
                capsule_sql = check.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'capsules'"
                ).fetchone()[0]
            self.assertTrue(
                any(row["table"] == "runs" and row["from"] == "run_id" for row in foreign_keys)
            )
            self.assertIn("CHECK (attempt >= 1)", capsule_sql)
            with self.assertRaises(sqlite3.IntegrityError):
                with migrated.transaction() as check:
                    check.execute(
                        """
                        INSERT INTO capsules(
                            capsule_id, message_id, task_id, contract_rev,
                            from_bee, to_bee, body_json, sha256, attempt, created_at
                        ) VALUES ('bad', 'bad', 'task-a', 1, 'a', 'b', '{}', 'h', 0, 't')
                        """
                    )
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main()
