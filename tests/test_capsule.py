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

from capsule import ack_capsule, build_capsule, capsule_sha256, contract_sha256
from swarm_models import ArtifactRecord, FactRecord, TaskContract
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
        capsule = self.make_capsule()
        first = capsule.to_json()
        second = capsule.to_json()
        self.assertEqual(first, second)
        self.assertEqual(capsule_sha256(capsule), capsule_sha256(capsule.to_dict()))
        self.assertIn('"protocol":"GCTX/0.1"', first)

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

    def test_expired_capsule_returns_stale(self) -> None:
        expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        capsule = self.make_capsule(
            expires_at=expired,
            message_id="msg_expired",
            capsule_id="cap_expired",
        )

        ack = self.ack(capsule)

        self.assertEqual(ack.status, "stale")

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


if __name__ == "__main__":
    unittest.main()
