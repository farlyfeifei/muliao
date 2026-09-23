"""Offline end-to-end tests for the current Ghost swarm implementation.

Run from the ``muliao`` directory with:
    python -m unittest discover -s tests -p "test_swarm_e2e.py" -v

The suite covers only interfaces implemented today: orchestrator planning
fallback, fixed research stages, bounded correction, synthetic handoff receipts,
completion, cancellation, JSON serialization, and contiguous event sequencing.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest


TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
for path in (str(PROJECT_ROOT), str(TESTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from swarm_fixture import (  # noqa: E402
    MODELS,
    MODELS_READY,
    RUNTIME_READY,
    RUNTIME_SKIP_REASON,
    TERMINAL_EVENT_TYPES,
    SwarmHarness,
    as_plain_dict,
    event_bee_id,
    event_payload,
    event_type,
)


@unittest.skipUnless(MODELS_READY, "swarm_models is not implemented yet")
class ProtocolJsonSerializationTests(unittest.TestCase):
    def test_event_json_round_trip_preserves_envelope(self) -> None:
        event = MODELS.SwarmEvent(
            event_id="evt_0001",
            seq=12,
            run_id="run_json",
            task_id="tsk_json",
            type="bee.start",
            payload={"bee_id": "investigator", "attempt": 1},
            ts=1_795_000_000.125,
        )

        encoded = event.to_json()
        decoded = json.loads(encoded)
        rebuilt = MODELS.SwarmEvent.from_dict(decoded)

        self.assertEqual(decoded, event.to_dict())
        self.assertEqual(rebuilt, event)
        self.assertEqual(json.loads(rebuilt.to_json()), decoded)
        self.assertEqual(list(decoded), sorted(decoded))
        json.dumps(as_plain_dict(event), ensure_ascii=False, allow_nan=False)

    def test_work_capsule_json_is_deterministic_and_round_trips(self) -> None:
        common = {
            "message_id": "msg_json",
            "capsule_id": "cap_json",
            "task_id": "tsk_json",
            "contract_rev": 1,
            "from_bee": "investigator",
            "to_bee": "verifier",
            "required_constraints": ("C001", "C004"),
            "facts": (
                {
                    "fact_id": "fact_json",
                    "state": "observed",
                    "value": {"zero_is_valid": 0},
                    "source_ref": "artifact://art_json#root",
                    "source_sha256": "a" * 64,
                },
            ),
            "artifacts": (
                {
                    "artifact_id": "art_json",
                    "uri": "file:///fixture/art.json",
                    "sha256": "a" * 64,
                    "mime": "application/json",
                },
            ),
            "created_at": "2026-09-23T00:00:00Z",
        }
        left = MODELS.WorkCapsule(
            **common,
            sender_claims={"required_payload_complete": True, "count": 1},
        )
        right = MODELS.WorkCapsule(
            **common,
            sender_claims={"count": 1, "required_payload_complete": True},
        )

        self.assertEqual(left.to_json(), right.to_json())
        decoded = json.loads(left.to_json())
        rebuilt = MODELS.WorkCapsule.from_dict(decoded)
        self.assertEqual(rebuilt, left)
        self.assertEqual(decoded["protocol"], "GCTX/0.1")
        self.assertEqual(decoded["facts"][0]["value"]["zero_is_valid"], 0)
        json.dumps(as_plain_dict(left), ensure_ascii=False, allow_nan=False)


@unittest.skipUnless(RUNTIME_READY, RUNTIME_SKIP_REASON)
class SwarmEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="muliao-swarm-e2e-")
        self.root = Path(self._temporary.name)

    async def asyncTearDown(self) -> None:
        self._temporary.cleanup()

    def harness(self, **kwargs) -> SwarmHarness:
        return SwarmHarness(self.root / f"case-{time.time_ns()}", **kwargs)

    def assert_event_contract(
        self,
        events: list[dict],
        run_id: str,
        *,
        terminal: str,
    ) -> None:
        self.assertTrue(events, "run produced no events")
        seqs = [event.get("seq") for event in events]
        self.assertEqual(
            seqs,
            list(range(1, len(events) + 1)),
            f"event seq is not contiguous from one: {seqs!r}",
        )

        event_ids = [event.get("event_id") for event in events]
        self.assertTrue(all(isinstance(value, str) and value for value in event_ids))
        self.assertEqual(len(event_ids), len(set(event_ids)), "event_id repeated")
        self.assertTrue(all(event.get("run_id") == run_id for event in events))
        self.assertTrue(
            all(isinstance(event.get("task_id"), str) and event["task_id"] for event in events)
        )
        self.assertTrue(all(isinstance(event.get("ts"), (int, float)) for event in events))
        self.assertTrue(all(isinstance(event.get("payload"), dict) for event in events))

        terminals = [
            event_type(event)
            for event in events
            if event_type(event) in TERMINAL_EVENT_TYPES
        ]
        self.assertEqual(terminals, [terminal])
        self.assertEqual(event_type(events[-1]), terminal, "terminal event was not last")

        for event in events:
            encoded = json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            self.assertEqual(json.loads(encoded), event)

    @staticmethod
    def first_seq(events: list[dict], kind: str, bee_id: str | None = None) -> int:
        for event in events:
            if event_type(event) != kind:
                continue
            if bee_id is not None and event_bee_id(event) != bee_id:
                continue
            return int(event["seq"])
        raise AssertionError(f"missing event {kind!r} for {bee_id!r}")

    def assert_handoff_receipts(self, events: list[dict]) -> None:
        created = [event for event in events if event_type(event) == "handoff.created"]
        acknowledgements = [event for event in events if event_type(event) == "handoff.ack"]
        self.assertTrue(created, "orchestrator emitted no handoff")
        self.assertEqual(len(created), len(acknowledgements))

        def identity(event: dict) -> tuple[str, str, str, str]:
            payload = event_payload(event)
            return (
                str(payload.get("from") or ""),
                str(payload.get("to") or ""),
                str(payload.get("capsule_id") or ""),
                str(payload.get("message_id") or ""),
            )

        created_by_identity = {identity(event): event for event in created}
        ack_by_identity = {identity(event): event for event in acknowledgements}
        self.assertEqual(set(created_by_identity), set(ack_by_identity))
        self.assertEqual(len(created_by_identity), len(created))

        for handoff_id, created_event in created_by_identity.items():
            self.assertTrue(all(handoff_id), f"incomplete handoff identity: {handoff_id!r}")
            ack_event = ack_by_identity[handoff_id]
            self.assertEqual(event_payload(ack_event).get("ack"), "accepted")
            self.assertLess(created_event["seq"], ack_event["seq"])
            receiver_start = self.first_seq(events, "bee.start", handoff_id[1])
            self.assertLess(
                ack_event["seq"],
                receiver_start,
                "receiver started before the orchestrator handoff receipt",
            )

    async def test_fallback_selects_research_parallelizes_and_completes(self) -> None:
        harness = self.harness(recipe="research", fail_planning=True)
        goal = "分析多个离线来源并给出证据"
        events = await harness.collect(
            goal,
            plan=None,
            run_id="run_fallback_research",
        )

        self.assert_event_contract(
            events,
            "run_fallback_research",
            terminal="swarm.done",
        )
        self.assertEqual(event_type(events[0]), "swarm.plan")
        plan_payload = event_payload(events[0])
        self.assertEqual(plan_payload.get("recipe"), "research")
        self.assertTrue(plan_payload.get("degraded"))
        self.assertEqual(
            plan_payload.get("plan", {}).get("planner"),
            "deterministic_fallback",
        )
        self.assertEqual(harness.jev.plan_calls, 1)
        self.assertEqual(harness.jev.check_calls, 0)
        self.assertGreaterEqual(harness.bees.peak_parallel, 2)
        self.assertEqual(
            {call.bee_id for call in harness.bees.calls_for("investigator") + harness.bees.calls_for("extractor")},
            {"investigator", "extractor"},
        )
        self.assertNotIn("bee.correct", {event_type(event) for event in events})
        self.assert_handoff_receipts(events)

        done = event_payload(events[-1])
        self.assertEqual(done.get("status"), "completed")
        self.assertEqual(done.get("recipe"), "research")
        self.assertTrue(done.get("degraded"))
        self.assertEqual(done.get("retries"), {})

    async def test_research_corrects_once_handoffs_and_completes(self) -> None:
        harness = self.harness(
            recipe="research",
            correction_target="investigator",
        )
        goal = "核验离线研究材料"
        plan = harness.plan(goal)
        events = await harness.collect(
            goal,
            plan=plan,
            run_id="run_research_correction",
        )

        self.assert_event_contract(
            events,
            "run_research_correction",
            terminal="swarm.done",
        )
        self.assertGreaterEqual(harness.bees.peak_parallel, 2)
        self.assertEqual(
            [call.attempt for call in harness.bees.calls_for("investigator")],
            [1, 2],
        )
        self.assertEqual(
            [call.attempt for call in harness.bees.calls_for("extractor")],
            [1],
        )

        corrections = [
            event
            for event in events
            if event_type(event) == "bee.correct"
            and event_bee_id(event) == "investigator"
        ]
        self.assertEqual(len(corrections), 1)
        first_check = self.first_seq(events, "bee.check", "investigator")
        correction_seq = int(corrections[0]["seq"])
        investigator_starts = [
            int(event["seq"])
            for event in events
            if event_type(event) == "bee.start"
            and event_bee_id(event) == "investigator"
        ]
        investigator_checks = [
            int(event["seq"])
            for event in events
            if event_type(event) == "bee.check"
            and event_bee_id(event) == "investigator"
        ]
        verifier_start = self.first_seq(events, "bee.start", "verifier")
        self.assertEqual(len(investigator_starts), 2)
        self.assertEqual(len(investigator_checks), 2)
        self.assertLess(first_check, correction_seq)
        self.assertLess(correction_seq, investigator_starts[1])
        self.assertLess(investigator_starts[1], investigator_checks[1])
        self.assertLess(investigator_checks[1], verifier_start)

        self.assert_handoff_receipts(events)
        done = event_payload(events[-1])
        self.assertEqual(done.get("status"), "completed")
        self.assertEqual(done.get("recipe"), "research")
        self.assertEqual(done.get("retries"), {"investigator": 1})

    async def test_cancel_is_terminal_and_drops_late_output(self) -> None:
        harness = self.harness(
            recipe="research",
            hold_bees=frozenset({"investigator", "extractor"}),
        )
        goal = "运行一个可取消的离线研究"
        plan = harness.plan(goal)
        cancel_event = asyncio.Event()
        task = harness.start(
            goal,
            plan=plan,
            run_id="run_cancel",
            cancel_event=cancel_event,
        )
        await asyncio.wait_for(harness.bees.parallel_started.wait(), timeout=3.0)
        cancel_event.set()
        events = await asyncio.wait_for(task, timeout=5.0)

        self.assert_event_contract(events, "run_cancel", terminal="swarm.cancelled")
        self.assertGreater(harness.bees.late_delta_attempts, 0)
        self.assertFalse(
            any(
                event_type(event) == "bee.delta"
                and event_payload(event).get("text") == "late-after-cancel"
                for event in events
            )
        )
        self.assertNotIn("swarm.done", {event_type(event) for event in events})
        self.assertNotIn("swarm.error", {event_type(event) for event in events})


if __name__ == "__main__":
    unittest.main(verbosity=2)
