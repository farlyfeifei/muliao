"""Durable orchestration boundary for Ghost swarm runs and handoffs.

``SwarmLedger`` deliberately has no knowledge of HTTP, runtime paths, or user
configuration.  A caller either injects an existing :class:`SwarmStore` or an
explicit SQLite path; omitting both creates an isolated in-memory ledger.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
import copy
from dataclasses import replace
import inspect
import json
from pathlib import Path
from typing import Any, Callable

from capsule import ArtifactLoader, ack_capsule, build_handoff_capsule
from swarm_models import CapsuleAck, SwarmEvent, TaskContract, WorkCapsule, utc_now
from swarm_planner import evaluate_user_gate, normalize_plan
from swarm_store import EventConflictError, SwarmStore


Publisher = Callable[[SwarmEvent], Any]

_TERMINAL_EVENT_STATUSES = {
    "swarm.done": "completed",
    "swarm.cancelled": "cancelled",
    "swarm.canceled": "cancelled",
    "swarm.error": "failed",
    "swarm.skipped": "skipped",
}
_NONTERMINAL_EVENT_STATUSES = {
    "swarm.waiting_user": "requires_confirmation",
    "swarm.paused": "paused",
}


class SwarmLedger:
    """Persist swarm control-plane state before it is exposed to consumers."""

    def __init__(
        self,
        store: SwarmStore | None = None,
        *,
        path: str | Path | None = None,
        publisher: Publisher | None = None,
    ) -> None:
        if store is not None and path is not None:
            raise ValueError("pass either store or path, not both")
        self.store = store if store is not None else SwarmStore(
            path if path is not None else ":memory:"
        )
        self._owns_store = store is None
        if publisher is not None and inspect.iscoroutinefunction(publisher):
            raise TypeError("publisher must be synchronous")
        self.publisher = publisher

    def close(self) -> None:
        """Close only a store created by this ledger."""

        if self._owns_store:
            self.store.close()

    def __enter__(self) -> "SwarmLedger":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @staticmethod
    def _contract(
        contract: TaskContract | Mapping[str, Any],
        *,
        goal: str,
        existing_created_at: str | None = None,
    ) -> TaskContract:
        if isinstance(contract, TaskContract):
            if contract.goal != goal:
                raise ValueError("goal differs from TaskContract.goal")
            return contract if contract.created_at else replace(
                contract,
                created_at=existing_created_at or utc_now(),
            )
        if not isinstance(contract, Mapping):
            raise TypeError("contract must be TaskContract or a mapping")
        data = copy.deepcopy(dict(contract))
        if data.get("goal") in (None, ""):
            data["goal"] = goal
        elif str(data["goal"]) != goal:
            raise ValueError("goal differs from contract mapping")
        if not data.get("created_at"):
            data["created_at"] = existing_created_at or utc_now()
        return TaskContract.from_dict(data)

    def begin_run(
        self,
        run_id: str,
        session_id: str,
        goal: str,
        plan: Mapping[str, Any],
        contract: TaskContract | Mapping[str, Any],
    ) -> TaskContract:
        """Persist an immutable contract and its run, idempotently."""

        run_id = str(run_id or "").strip()
        session_id = str(session_id or "").strip()
        goal = str(goal or "").strip()
        if not run_id or not session_id or not goal:
            raise ValueError("run_id, session_id, and goal are required")
        if not isinstance(plan, Mapping):
            raise TypeError("plan must be a mapping")

        existing = None
        if isinstance(contract, TaskContract):
            existing = self.store.get_contract(contract.task_id, contract.contract_rev)
        elif isinstance(contract, Mapping):
            task_id = str(contract.get("task_id") or "")
            try:
                contract_rev = int(contract.get("contract_rev") or 0)
            except (TypeError, ValueError, OverflowError):
                contract_rev = 0
            if task_id and contract_rev >= 1:
                existing = self.store.get_contract(task_id, contract_rev)
        normalized = self._contract(
            contract,
            goal=goal,
            existing_created_at=None if existing is None else existing.created_at,
        )
        decision = normalize_plan(plan)
        gate = evaluate_user_gate(
            decision,
            confirmed=bool(
                isinstance(plan.get("confirmation"), Mapping)
                and plan["confirmation"].get("satisfied") is True
            ),
        )
        recipe_id = decision.recipe_id
        self.store.register_run(
            normalized,
            run_id,
            recipe_id,
            status=("requires_confirmation" if gate.required else "running"),
            session_id=session_id,
            plan=copy.deepcopy(dict(plan)),
        )
        return normalized

    def confirm_run(
        self,
        run_id: str,
        confirmation: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self.store.confirm_run(str(run_id), confirmation)

    def next_seq(self, run_id: str) -> int:
        return self.store.next_run_seq(str(run_id))

    @staticmethod
    def _event(event: SwarmEvent | Mapping[str, Any]) -> SwarmEvent:
        if isinstance(event, SwarmEvent):
            return event
        if not isinstance(event, Mapping):
            raise TypeError("event must be SwarmEvent or a mapping")
        return SwarmEvent.from_dict(copy.deepcopy(dict(event)))

    def persist_event(
        self,
        event: SwarmEvent | Mapping[str, Any],
        *,
        publish: Publisher | None = None,
    ) -> SwarmEvent:
        """Append an event, update run state, then publish the persisted value.

        A publisher is never called if persistence or status synchronization
        fails.  Replaying the same event is idempotent and is not published a
        second time.
        """

        callback = publish if publish is not None else self.publisher
        if callback is not None and inspect.iscoroutinefunction(callback):
            raise TypeError("publisher must be synchronous")
        candidate = self._event(event)
        status = _TERMINAL_EVENT_STATUSES.get(candidate.type)
        if status is None:
            status = _NONTERMINAL_EVENT_STATUSES.get(candidate.type)
        persisted, inserted = self.store.record_event(
            candidate,
            run_status=status,
        )

        callback = publish if publish is not None else self.publisher
        if inserted and callback is not None:
            result = callback(persisted)
            if inspect.isawaitable(result):
                raise TypeError("publisher must be synchronous")
        return persisted

    def events(self, run_id: str, after_seq: int = 0) -> list[SwarmEvent]:
        """Replay ordered run-local events after ``after_seq``."""

        return self.store.list_events(str(run_id), after_run_seq=int(after_seq))

    def status(self, run_id: str) -> dict[str, Any] | None:
        """Return the durable run snapshot, or ``None`` when it is unknown."""

        return self.store.get_run(str(run_id))

    def recover_interrupted_runs(self) -> list[str]:
        """Mark in-flight runs interrupted after process restart."""

        return self.store.recover_interrupted_runs()

    @staticmethod
    def _same_handoff(left: WorkCapsule, right: WorkCapsule) -> bool:
        """Compare deterministic handoff content while ignoring creation time."""

        left_data = left.to_dict()
        right_data = right.to_dict()
        left_data.pop("created_at", None)
        right_data.pop("created_at", None)
        return left_data == right_data

    def exchange_handoff(
        self,
        *,
        run_id: str,
        stage_id: str,
        contract: TaskContract | Mapping[str, Any],
        from_bee: str,
        to_bee: str,
        result: Any,
        current_permission_snapshot: Any,
        receiver_permissions: Collection[str] = (),
        artifact_loader: ArtifactLoader | None = None,
        attempt: int = 1,
    ) -> tuple[WorkCapsule, CapsuleAck]:
        """Build, receiver-verify, and atomically persist a real handoff ACK."""

        capsule = build_handoff_capsule(
            run_id=run_id,
            stage_id=stage_id,
            contract=contract,
            from_bee=from_bee,
            to_bee=to_bee,
            result=result,
            attempt=attempt,
        )
        existing = self.store.find_capsule_by_message(capsule.message_id)
        if existing is not None:
            persisted = WorkCapsule.from_dict(json.loads(existing["body_json"]))
            if self._same_handoff(persisted, capsule):
                capsule = persisted
            # Otherwise preserve the newly-built capsule so ack_capsule can
            # return the protocol-level conflicted ACK for message-id reuse.
        ack = ack_capsule(
            capsule,
            self.store,
            current_permission_snapshot=current_permission_snapshot,
            receiver_permissions=receiver_permissions,
            receiver_bee=to_bee,
            artifact_loader=artifact_loader,
            run_id=run_id,
            stage_id=stage_id,
            attempt=attempt,
        )
        return capsule, ack


__all__ = ["SwarmLedger"]
