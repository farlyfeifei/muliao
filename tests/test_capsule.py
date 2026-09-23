from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest


MODULE_DIR = Path(__file__).resolve().parents[1]
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from capsule import (
    ack_capsule,
    build_capsule,
    build_handoff_capsule,
    capsule_sha256,
    contract_sha256,
)
from swarm_models import ArtifactRecord, CapsuleAck, FactRecord, TaskContract, WorkCapsule
from swarm_store import SwarmStore


class CapsuleProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = SwarmStore(self.root / "swarm.db")
        self.permission_snapshot = {"revision": 12, "grants": ["artifact.read"]}
        self.contract = TaskContract(
            task_id="tsk_demo",
            contract_rev=1,
            goal="核验有来源的交接包",
            required_constraints=(
                {"id": "C001", "text": "每个观察事实必须有来源", "source": "user"},
            ),
            allowed_tools=("artifact.read",),
            permission_snapshot=self.permission_snapshot,
        )
        self.store.put_contract(self.contract, contract_sha256(self.contract))
        self.source_path = self.root / "source.txt"
        self.source_path.write_bytes(b"source evidence\n")
        self.source_hash = hashlib.sha256(self.source_path.read_bytes()).hexdigest()
        self.artifact = {
            "artifact_id": "art_source",
            "uri": str(self.source_path),
            "sha256": self.source_hash,
            "mime": "text/plain",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_capsule(self, **overrides):
        values = {
            "task_id": self.contract.task_id,
            "contract_rev": self.contract.contract_rev,
            "from_bee": "investigator",
            "to_bee": "verifier",
            "required_constraints": ("C001",),
            "facts": (
                {
                    "fact_id": "fact_observed",
                    "state": "observed",
                    "value": "source evidence",
                    "source_ref": "artifact://art_source#L1",
                    "source_sha256": self.source_hash,
                },
            ),
            "artifacts": (self.artifact,),
            "permission_snapshot": self.permission_snapshot,
            "required_permissions": ("artifact.read",),
            "message_id": "msg_demo",
            "capsule_id": "cap_demo",
        }
        values.update(overrides)
        return build_capsule(**values)

    def ack(self, capsule):
        return ack_capsule(
            capsule,
            self.store,
            current_permission_snapshot=self.permission_snapshot,
            receiver_permissions={"artifact.read"},
        )

    def test_models_are_deterministically_json_serializable(self) -> None:
        capsule = self.make_capsule(payload={"summary": "完整结果", "ok": True})
        first = capsule.to_json()
        second = capsule.to_json()
        self.assertEqual(first, second)
        self.assertEqual(capsule_sha256(capsule), capsule_sha256(capsule.to_dict()))
        self.assertIn('"protocol":"GCTX/0.1"', first)
        self.assertEqual(
            WorkCapsule.from_dict(capsule.to_dict()).payload,
            {"summary": "完整结果", "ok": True},
        )

    def test_handoff_capsule_round_trip_and_deterministic_ids(self) -> None:
        result = {
            "status": "done",
            "summary": "handoff complete",
            "facts": [
                {
                    "fact_id": "fact_observed",
                    "state": "observed",
                    "value": "source evidence",
                    "source_ref": "artifact://art_source#L1",
                    "source_sha256": self.source_hash,
                }
            ],
            "artifacts": [self.artifact],
            "open_questions": ["what next?"],
            "side_effects": [{"kind": "write", "path": "out.json"}],
            "loss_manifest": ["raw logs omitted"],
            "depends_on": [],
            "nested": {"all": ["safe", 1, True, None]},
        }
        kwargs = {
            "run_id": "run_demo",
            "stage_id": "stage_1",
            "contract": self.contract,
            "from_bee": "investigator",
            "to_bee": "verifier",
            "result": result,
            "attempt": 2,
        }

        first = build_handoff_capsule(**kwargs)
        second = build_handoff_capsule(**dict(kwargs, contract=self.contract.to_dict()))
        round_trip = WorkCapsule.from_dict(first.to_dict())

        self.assertEqual(first.message_id, second.message_id)
        self.assertEqual(first.capsule_id, second.capsule_id)
        self.assertEqual(first.created_at, second.created_at)
        self.assertEqual(first.created_at, self.contract.created_at)
        self.assertEqual(round_trip, first)
        self.assertEqual(first.payload, result)
        self.assertEqual(first.required_constraints, ("C001",))
        self.assertEqual(first.permission_snapshot, self.permission_snapshot)
        self.assertEqual(first.required_permissions, ("artifact.read",))
        self.assertEqual(first.open_questions, ("what next?",))
        self.assertEqual(first.loss_manifest, ("raw logs omitted",))

        changed_attempt = build_handoff_capsule(**dict(kwargs, attempt=3))
        self.assertNotEqual(first.message_id, changed_attempt.message_id)
        self.assertNotEqual(first.capsule_id, changed_attempt.capsule_id)

    def test_handoff_constraint_without_id_uses_stable_text_identifier(self) -> None:
        contract = TaskContract(
            task_id="tsk_constraint_text",
            contract_rev=1,
            goal="preserve text constraint",
            required_constraints=(
                {"text": "Do not omit the audited limitation", "source": "user"},
                {"string": "Preserve exact output shape", "source": "system"},
            ),
            created_at="2026-09-23T12:00:00Z",
        )

        capsule = build_handoff_capsule(
            run_id="run_constraint_text",
            stage_id="stage_1",
            contract=contract,
            from_bee="compiler",
            to_bee="investigator",
            result={"summary": "done"},
        )

        self.assertEqual(
            capsule.required_constraints,
            (
                "Do not omit the audited limitation",
                "Preserve exact output shape",
            ),
        )
        self.assertEqual(capsule.created_at, contract.created_at)

    def test_handoff_created_at_is_stable_without_contract_timestamp(self) -> None:
        contract = self.contract.to_dict()
        contract["created_at"] = None
        kwargs = {
            "run_id": "run_stable_time",
            "stage_id": "stage_1",
            "contract": contract,
            "from_bee": "investigator",
            "to_bee": "verifier",
            "result": {"summary": "done"},
        }

        first = build_handoff_capsule(**kwargs)
        second = build_handoff_capsule(**kwargs)

        self.assertEqual(first.created_at, second.created_at)
        self.assertEqual(first.to_json(), second.to_json())

    def test_schema_contains_only_required_tables_and_message_id_is_unique(self) -> None:
        required = {
            "contracts",
            "runs",
            "bee_runs",
            "events",
            "facts",
            "artifacts",
            "capsules",
            "usage_records",
        }
        self.assertTrue(required.issubset(self.store.table_names()))

        capsule = self.make_capsule()
        first = self.ack(capsule)
        second = self.ack(capsule)

        self.assertEqual(first.status, "accepted")
        self.assertEqual(second.status, "accepted")
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        with self.store.connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM capsules WHERE message_id = ?",
                (capsule.message_id,),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_duplicate_reuses_persisted_ack_timestamp_and_hash(self) -> None:
        self.store.create_run(
            "run_demo",
            self.contract.task_id,
            "research",
            self.contract.contract_rev,
        )
        capsule = self.make_capsule(
            message_id="msg_atomic_duplicate",
            capsule_id="cap_atomic_duplicate",
        )
        first = ack_capsule(
            capsule,
            self.store,
            current_permission_snapshot=self.permission_snapshot,
            receiver_permissions={"artifact.read"},
            run_id="run_demo",
            stage_id="stage_1",
            attempt=2,
        )
        second = ack_capsule(
            capsule,
            self.store,
            current_permission_snapshot=self.permission_snapshot,
            receiver_permissions={"artifact.read"},
            run_id="run_demo",
            stage_id="stage_1",
            attempt=2,
        )

        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(second.checked_at, first.checked_at)
        self.assertEqual(second.checked_sha256, first.checked_sha256)
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT run_id, stage_id, attempt, ack_json FROM capsules "
                "WHERE message_id = ?",
                (capsule.message_id,),
            ).fetchone()
        self.assertEqual(row["run_id"], "run_demo")
        self.assertEqual(row["stage_id"], "stage_1")
        self.assertEqual(row["attempt"], 2)
        self.assertIsNotNone(row["ack_json"])

    def test_duplicate_wrong_receiver_is_forbidden_without_reloading_artifact(self) -> None:
        capsule = self.make_capsule(
            message_id="msg_duplicate_wrong_receiver",
            capsule_id="cap_duplicate_wrong_receiver",
        )
        loads: list[str] = []

        first = ack_capsule(
            capsule,
            self.store,
            current_permission_snapshot=self.permission_snapshot,
            receiver_permissions={"artifact.read"},
            receiver_bee="verifier",
            artifact_loader=lambda artifact: (
                loads.append(str(artifact["artifact_id"])),
                self.source_path.read_bytes(),
            )[1],
        )
        second = ack_capsule(
            capsule,
            self.store,
            current_permission_snapshot=self.permission_snapshot,
            receiver_permissions={"artifact.read"},
            receiver_bee="different-verifier",
            artifact_loader=lambda _artifact: (_ for _ in ()).throw(
                AssertionError("duplicate ACK must not reload artifact bytes")
            ),
        )

        self.assertEqual(first.status, "accepted")
        self.assertEqual(loads, ["art_source"])
        self.assertEqual(second.status, "forbidden")
        self.assertTrue(second.duplicate)

    def test_identical_accepted_duplicate_revalidates_artifact_and_reuses_ack_identity(self) -> None:
        capsule = self.make_capsule(
            message_id="msg_duplicate_revalidate",
            capsule_id="cap_duplicate_revalidate",
        )
        first = self.ack(capsule)
        loads: list[str] = []
        second = ack_capsule(
            capsule,
            self.store,
            current_permission_snapshot=self.permission_snapshot,
            receiver_permissions={"artifact.read"},
            receiver_bee="verifier",
            artifact_loader=lambda artifact: (
                loads.append(str(artifact["artifact_id"])),
                self.source_path.read_bytes(),
            )[1],
        )

        self.assertEqual(first.status, "accepted")
        self.assertEqual(loads, ["art_source"])
        self.assertEqual(second.status, "accepted")
        self.assertTrue(second.duplicate)
        self.assertEqual(second.checked_at, first.checked_at)
        self.assertEqual(second.checked_sha256, first.checked_sha256)

    def test_duplicate_after_dependency_invalidation_returns_stale(self) -> None:
        self.store.put_fact(
            FactRecord(
                fact_id="fact-duplicate-dependency",
                task_id=self.contract.task_id,
                state="verified",
                value="current",
            )
        )
        capsule = self.make_capsule(
            depends_on=("fact-duplicate-dependency",),
            message_id="msg-duplicate-stale",
            capsule_id="cap-duplicate-stale",
        )
        first = self.ack(capsule)
        self.store.invalidate_fact("fact-duplicate-dependency")
        second = self.ack(capsule)
        self.assertEqual(first.status, "accepted")
        self.assertEqual(second.status, "stale")
        self.assertTrue(second.duplicate)
        self.assertEqual(
            self.store.get_capsule(capsule.capsule_id)["status"],
            "stale",
        )

    def test_inline_fact_dependency_is_verified(self) -> None:
        self.store.put_fact(
            FactRecord(
                fact_id="fact-inline-old",
                task_id=self.contract.task_id,
                state="invalidated",
                value="old",
            )
        )
        capsule = self.make_capsule(
            facts=(
                {
                    "fact_id": "fact-inline-new",
                    "state": "inferred",
                    "value": "derived",
                    "depends_on": ["fact-inline-old"],
                },
            ),
            artifacts=(),
            message_id="msg-inline-dependency",
            capsule_id="cap-inline-dependency",
        )
        ack = self.ack(capsule)
        self.assertEqual(ack.status, "stale")
        self.assertIn("stale:fact-inline-old", ack.missing)

    def test_artifact_loader_runtime_error_returns_need_context(self) -> None:
        capsule = self.make_capsule(
            message_id="msg-loader-runtime-error",
            capsule_id="cap-loader-runtime-error",
        )
        ack = ack_capsule(
            capsule,
            self.store,
            current_permission_snapshot=self.permission_snapshot,
            receiver_permissions={"artifact.read"},
            receiver_bee="verifier",
            artifact_loader=lambda _artifact: (_ for _ in ()).throw(
                RuntimeError("network down")
            ),
        )
        self.assertEqual(ack.status, "need_context")
        self.assertTrue(any("network down" in reason for reason in ack.reasons))

    def test_duplicate_metadata_mismatch_is_conflicted(self) -> None:
        self.store.create_run(
            "run_duplicate_metadata",
            self.contract.task_id,
            "research",
            self.contract.contract_rev,
        )
        capsule = self.make_capsule(
            message_id="msg_duplicate_metadata",
            capsule_id="cap_duplicate_metadata",
        )
        first = ack_capsule(
            capsule,
            self.store,
            current_permission_snapshot=self.permission_snapshot,
            receiver_permissions={"artifact.read"},
            run_id="run_duplicate_metadata",
            stage_id="stage_1",
            attempt=1,
        )
        second = ack_capsule(
            capsule,
            self.store,
            current_permission_snapshot=self.permission_snapshot,
            receiver_permissions={"artifact.read"},
            run_id="run_duplicate_metadata",
            stage_id="stage_2",
            attempt=1,
        )

        self.assertEqual(first.status, "accepted")
        self.assertEqual(second.status, "conflicted")
        self.assertTrue(second.duplicate)
        self.assertTrue(any("stage_id" in reason for reason in second.reasons))

    def test_existing_capsule_without_ack_is_revalidated_and_completed(self) -> None:
        capsule = self.make_capsule(
            message_id="msg_pending_ack",
            capsule_id="cap_pending_ack",
        )
        inserted, row = self.store.put_capsule(capsule)
        self.assertTrue(inserted)
        self.assertIsNone(row["ack_json"])

        self.source_path.write_bytes(b"tampered evidence\n")
        ack = self.ack(capsule)

        self.assertEqual(ack.status, "conflicted")
        self.assertFalse(ack.duplicate)
        persisted = self.store.find_capsule_by_message(capsule.message_id)
        self.assertEqual(persisted["ack_status"], "conflicted")
        self.assertIsNotNone(persisted["ack_json"])

    def test_reused_message_id_with_different_content_is_conflicted(self) -> None:
        original = self.make_capsule()
        self.assertEqual(self.ack(original).status, "accepted")

        altered = self.make_capsule(
            capsule_id="cap_changed",
            facts=(
                {
                    "fact_id": "fact_observed",
                    "state": "observed",
                    "value": "different value",
                    "source_ref": "artifact://art_source#L1",
                    "source_sha256": self.source_hash,
                },
            ),
        )
        ack = self.ack(altered)

        self.assertEqual(ack.status, "conflicted")
        self.assertTrue(ack.duplicate)

    def test_missing_source_returns_need_context(self) -> None:
        capsule = self.make_capsule(
            facts=(
                {
                    "fact_id": "fact_without_source",
                    "state": "observed",
                    "value": "unsupported claim",
                },
            ),
            message_id="msg_missing_source",
            capsule_id="cap_missing_source",
        )

        ack = self.ack(capsule)

        self.assertEqual(ack.status, "need_context")
        self.assertIn("source_ref:fact_without_source", ack.missing)

    def test_outdated_contract_returns_stale(self) -> None:
        revision_two = TaskContract(
            task_id=self.contract.task_id,
            contract_rev=2,
            goal=self.contract.goal,
            required_constraints=self.contract.required_constraints,
            allowed_tools=self.contract.allowed_tools,
            permission_snapshot=self.permission_snapshot,
        )
        self.store.put_contract(revision_two, contract_sha256(revision_two))
        capsule = self.make_capsule(
            message_id="msg_old_contract",
            capsule_id="cap_old_contract",
        )

        ack = self.ack(capsule)

        self.assertEqual(ack.status, "stale")
        self.assertTrue(any("contract_rev" in reason for reason in ack.reasons))

    def test_conflicted_fact_returns_conflicted(self) -> None:
        self.store.put_fact(
            FactRecord(
                fact_id="fact_conflict",
                task_id=self.contract.task_id,
                state="conflicted",
                value={"candidates": ["A", "B"]},
                source_ref="artifact://art_source#L1",
                source_sha256=self.source_hash,
            )
        )
        capsule = self.make_capsule(
            facts=(
                {
                    "fact_id": "fact_conflict",
                    "state": "conflicted",
                    "value": {"candidates": ["A", "B"]},
                    "source_ref": "artifact://art_source#L1",
                    "source_sha256": self.source_hash,
                },
            ),
            message_id="msg_conflict",
            capsule_id="cap_conflict",
        )

        ack = self.ack(capsule)

        self.assertEqual(ack.status, "conflicted")

    def test_artifact_hash_mismatch_returns_conflicted(self) -> None:
        bad_artifact = dict(self.artifact, sha256="0" * 64)
        capsule = self.make_capsule(
            artifacts=(bad_artifact,),
            message_id="msg_bad_hash",
            capsule_id="cap_bad_hash",
        )

        ack = self.ack(capsule)

        self.assertEqual(ack.status, "conflicted")
        self.assertTrue(any("sha256 mismatch" in reason for reason in ack.reasons))

    def test_permission_snapshot_mismatch_returns_forbidden(self) -> None:
        capsule = self.make_capsule(
            permission_snapshot={"revision": 11, "grants": ["artifact.read"]},
            message_id="msg_old_permission",
            capsule_id="cap_old_permission",
        )

        ack = self.ack(capsule)

        self.assertEqual(ack.status, "forbidden")

    def test_receiver_identity_mismatch_returns_forbidden(self) -> None:
        capsule = self.make_capsule(
            message_id="msg_wrong_receiver",
            capsule_id="cap_wrong_receiver",
        )

        ack = ack_capsule(
            capsule,
            self.store,
            current_permission_snapshot=self.permission_snapshot,
            receiver_permissions={"artifact.read"},
            receiver_bee="different-verifier",
        )

        self.assertEqual(ack.status, "forbidden")
        self.assertTrue(any("does not match" in reason for reason in ack.reasons))

    def test_expired_capsule_returns_stale(self) -> None:
        expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        capsule = self.make_capsule(
            expires_at=expired,
            message_id="msg_expired",
            capsule_id="cap_expired",
        )

        ack = self.ack(capsule)

        self.assertEqual(ack.status, "stale")

    def test_inline_fact_expiry_is_validated_and_enforced(self) -> None:
        check_time = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
        base_fact = {
            "fact_id": "fact_expiry",
            "state": "observed",
            "value": "source evidence",
            "source_ref": "artifact://art_source#L1",
            "source_sha256": self.source_hash,
        }
        expired = self.make_capsule(
            facts=(dict(base_fact, expires_at="2026-09-23T11:59:59Z"),),
            message_id="msg_fact_expired",
            capsule_id="cap_fact_expired",
        )
        invalid = self.make_capsule(
            facts=(dict(base_fact, expires_at="2026-09-23T13:00:00"),),
            message_id="msg_fact_invalid_expiry",
            capsule_id="cap_fact_invalid_expiry",
        )
        future = self.make_capsule(
            facts=(dict(base_fact, expires_at="2026-09-23T13:00:00+00:00"),),
            message_id="msg_fact_future",
            capsule_id="cap_fact_future",
        )

        expired_ack = ack_capsule(
            expired,
            self.store,
            current_permission_snapshot=self.permission_snapshot,
            receiver_permissions={"artifact.read"},
            now=check_time,
        )
        invalid_ack = ack_capsule(
            invalid,
            self.store,
            current_permission_snapshot=self.permission_snapshot,
            receiver_permissions={"artifact.read"},
            now=check_time,
        )
        future_ack = ack_capsule(
            future,
            self.store,
            current_permission_snapshot=self.permission_snapshot,
            receiver_permissions={"artifact.read"},
            now=check_time,
        )

        self.assertEqual(expired_ack.status, "stale")
        self.assertEqual(invalid_ack.status, "incompatible")
        self.assertEqual(future_ack.status, "accepted")

    def test_unknown_protocol_returns_incompatible(self) -> None:
        data = self.make_capsule(
            message_id="msg_protocol",
            capsule_id="cap_protocol",
        ).to_dict()
        data["protocol"] = "GCTX/9.9"
        from swarm_models import WorkCapsule

        capsule = WorkCapsule.from_dict(data)
        ack = self.ack(capsule)

        self.assertEqual(ack.status, "incompatible")

    def test_invalidated_fact_marks_only_dependent_artifact_stale(self) -> None:
        self.store.put_fact(
            FactRecord(
                fact_id="fact_old",
                task_id=self.contract.task_id,
                state="verified",
                value=0,
                source_ref="artifact://art_source#L1",
                source_sha256=self.source_hash,
            )
        )
        self.store.put_artifact(
            ArtifactRecord(
                artifact_id="art_dependent",
                task_id=self.contract.task_id,
                uri=str(self.root / "dependent.json"),
                mime="application/json",
                sha256="1" * 64,
                depends_on=("fact_old",),
            )
        )
        self.store.put_artifact(
            ArtifactRecord(
                artifact_id="art_unrelated",
                task_id=self.contract.task_id,
                uri=str(self.root / "unrelated.json"),
                mime="application/json",
                sha256="2" * 64,
                depends_on=(),
            )
        )

        affected = self.store.invalidate_fact("fact_old")

        self.assertEqual(self.store.get_fact("fact_old")["state"], "invalidated")
        self.assertEqual(self.store.get_artifact("art_dependent")["status"], "stale")
        self.assertEqual(self.store.get_artifact("art_unrelated")["status"], "current")
        self.assertEqual(affected["artifacts"], ["art_dependent"])

    def test_stale_dependency_rejects_downstream_capsule(self) -> None:
        self.store.put_fact(
            FactRecord(
                fact_id="fact_dependency",
                task_id=self.contract.task_id,
                state="invalidated",
                value="old",
            )
        )
        capsule = self.make_capsule(
            depends_on=("fact_dependency",),
            message_id="msg_stale_dependency",
            capsule_id="cap_stale_dependency",
        )

        ack = self.ack(capsule)

        self.assertEqual(ack.status, "stale")

    def test_accepted_inline_evidence_is_promoted_for_downstream_dependencies(self) -> None:
        first = self.make_capsule(
            message_id="msg_promote_evidence",
            capsule_id="cap_promote_evidence",
        )

        first_ack = self.ack(first)
        downstream = self.make_capsule(
            facts=(),
            artifacts=(),
            depends_on=("fact_observed", "art_source"),
            message_id="msg_promoted_dependency",
            capsule_id="cap_promoted_dependency",
        )
        second_ack = self.ack(downstream)

        self.assertEqual(first_ack.status, "accepted")
        self.assertIsNotNone(self.store.get_fact("fact_observed"))
        self.assertIsNotNone(self.store.get_artifact("art_source"))
        self.assertEqual(second_ack.status, "accepted")

    def test_rejected_capsule_dependency_is_not_accepted(self) -> None:
        dependency = self.make_capsule(
            message_id="msg_rejected_dependency",
            capsule_id="cap_rejected_dependency",
        )
        rejected_ack = CapsuleAck(
            message_id=dependency.message_id,
            capsule_id=dependency.capsule_id,
            status="forbidden",
            checked_sha256=capsule_sha256(dependency),
        )
        self.store.record_capsule_ack(dependency, rejected_ack)
        downstream = self.make_capsule(
            depends_on=(dependency.capsule_id,),
            message_id="msg_downstream_rejected",
            capsule_id="cap_downstream_rejected",
        )

        ack = self.ack(downstream)

        self.assertEqual(ack.status, "need_context")
        self.assertIn(f"dependency:{dependency.capsule_id}", ack.missing)

    def test_stored_fact_from_another_task_is_conflicted(self) -> None:
        self.store.put_fact(
            FactRecord(
                fact_id="fact_foreign_task",
                task_id="tsk_foreign",
                state="observed",
                value="source evidence",
                source_ref="artifact://art_source#L1",
                source_sha256=self.source_hash,
            )
        )
        capsule = self.make_capsule(
            facts=(
                {
                    "fact_id": "fact_foreign_task",
                    "state": "observed",
                    "value": "source evidence",
                    "source_ref": "artifact://art_source#L1",
                    "source_sha256": self.source_hash,
                },
            ),
            message_id="msg_foreign_fact",
            capsule_id="cap_foreign_fact",
        )

        ack = self.ack(capsule)

        self.assertEqual(ack.status, "conflicted")
        self.assertTrue(any("belongs to task" in reason for reason in ack.reasons))

    def test_inline_artifact_id_cannot_alias_another_task(self) -> None:
        self.store.put_artifact(
            ArtifactRecord(
                artifact_id="art_source",
                task_id="tsk_foreign",
                uri=str(self.source_path),
                mime="text/plain",
                sha256=self.source_hash,
            )
        )
        capsule = self.make_capsule(
            message_id="msg_inline_foreign_artifact",
            capsule_id="cap_inline_foreign_artifact",
        )

        ack = self.ack(capsule)

        self.assertEqual(ack.status, "conflicted")
        self.assertTrue(any("belongs to task" in reason for reason in ack.reasons))

    def test_stored_artifact_from_another_task_is_conflicted(self) -> None:
        self.store.put_artifact(
            ArtifactRecord(
                artifact_id="art_foreign_task",
                task_id="tsk_foreign",
                uri=str(self.source_path),
                mime="text/plain",
                sha256=self.source_hash,
            )
        )
        capsule = self.make_capsule(
            facts=(
                {
                    "fact_id": "fact_foreign_artifact",
                    "state": "observed",
                    "value": "source evidence",
                    "source_ref": "artifact://art_foreign_task#L1",
                    "source_sha256": self.source_hash,
                },
            ),
            artifacts=(),
            message_id="msg_foreign_artifact",
            capsule_id="cap_foreign_artifact",
        )

        ack = self.ack(capsule)

        self.assertEqual(ack.status, "conflicted")
        self.assertTrue(any("belongs to task" in reason for reason in ack.reasons))

    def test_expired_stored_fact_is_stale(self) -> None:
        self.store.put_fact(
            FactRecord(
                fact_id="fact_stored_expired",
                task_id=self.contract.task_id,
                state="observed",
                value="source evidence",
                source_ref="artifact://art_source#L1",
                source_sha256=self.source_hash,
                expires_at="2026-09-23T11:59:59Z",
            )
        )
        capsule = self.make_capsule(
            facts=(
                {
                    "fact_id": "fact_stored_expired",
                    "state": "observed",
                    "value": "source evidence",
                    "source_ref": "artifact://art_source#L1",
                    "source_sha256": self.source_hash,
                },
            ),
            message_id="msg_stored_expired",
            capsule_id="cap_stored_expired",
        )

        ack = ack_capsule(
            capsule,
            self.store,
            current_permission_snapshot=self.permission_snapshot,
            receiver_permissions={"artifact.read"},
            now=datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(ack.status, "stale")
        self.assertTrue(any("has expired" in reason for reason in ack.reasons))

    def test_unknown_inline_artifact_status_is_rejected(self) -> None:
        capsule = self.make_capsule(
            artifacts=(dict(self.artifact, status="mystery"),),
            message_id="msg_unknown_inline_artifact_status",
            capsule_id="cap_unknown_inline_artifact_status",
        )

        ack = self.ack(capsule)

        self.assertEqual(ack.status, "incompatible")
        self.assertTrue(any("unsupported status" in reason for reason in ack.reasons))

    def test_unknown_stored_artifact_status_is_rejected(self) -> None:
        self.store.put_artifact(
            ArtifactRecord(
                artifact_id="art_unknown_status",
                task_id=self.contract.task_id,
                uri=str(self.source_path),
                mime="text/plain",
                sha256=self.source_hash,
                status="mystery",
            )
        )
        capsule = self.make_capsule(
            facts=(
                {
                    "fact_id": "fact_unknown_artifact",
                    "state": "observed",
                    "value": "source evidence",
                    "source_ref": "artifact://art_unknown_status#L1",
                    "source_sha256": self.source_hash,
                },
            ),
            artifacts=(),
            message_id="msg_unknown_artifact_status",
            capsule_id="cap_unknown_artifact_status",
        )

        ack = self.ack(capsule)

        self.assertEqual(ack.status, "incompatible")
        self.assertTrue(any("unsupported status" in reason for reason in ack.reasons))


if __name__ == "__main__":
    unittest.main()
