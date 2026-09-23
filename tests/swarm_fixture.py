"""Offline fixtures aligned with the current Ghost swarm interfaces.

The fixture imports the current swarm, API, capsule, and bee-runtime modules so
import regressions remain visible.  The end-to-end harness intentionally drives
``SwarmOrchestrator`` through its documented ``bee_runner(bee_id, context)``
seam; ``BeeRuntime.run`` has a different public signature and is covered by its
own unit tests rather than hidden behind a test-only adapter here.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
import importlib
import json
from pathlib import Path
import time
from typing import Any


def _probe_runtime() -> tuple[dict[str, Any] | None, str]:
    modules: dict[str, Any] = {}
    required = (
        "swarm_models",
        "capsule",
        "swarm",
        "swarm_api",
        "swarm_runtime",
    )
    for name in required:
        try:
            modules[name] = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name in set(required):
                return None, f"Ghost swarm runtime is incomplete: missing {exc.name}"
            raise

    required_attributes = {
        "capsule": "build_capsule",
        "swarm": "SwarmOrchestrator",
        "swarm_api": "SwarmService",
        "swarm_runtime": "BeeRuntime",
    }
    for module_name, attribute in required_attributes.items():
        if not hasattr(modules[module_name], attribute):
            return None, (
                f"Ghost swarm runtime is incomplete: "
                f"{module_name}.{attribute} is absent"
            )
    return modules, ""


RUNTIME, RUNTIME_SKIP_REASON = _probe_runtime()
RUNTIME_READY = RUNTIME is not None

try:
    MODELS = importlib.import_module("swarm_models")
except ModuleNotFoundError:
    MODELS = None
MODELS_READY = MODELS is not None


TERMINAL_EVENT_TYPES = frozenset(
    {"swarm.done", "swarm.cancelled", "swarm.error"}
)


def as_plain_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        parsed = to_dict()
        if isinstance(parsed, Mapping):
            return dict(parsed)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"cannot convert {type(value)!r} to a JSON object")


def event_type(event: Mapping[str, Any]) -> str:
    return str(event.get("type") or event.get("event_type") or "")


def event_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event.get("payload", {})
    return dict(payload) if isinstance(payload, Mapping) else {}


def event_bee_id(event: Mapping[str, Any]) -> str:
    payload = event_payload(event)
    return str(payload.get("bee_id") or payload.get("role") or "")


class MockJev:
    """Deterministic planner/checker compatible with the current Jev seam."""

    def __init__(
        self,
        recipe: str,
        *,
        correction_target: str | None = None,
        fail_planning: bool = False,
    ) -> None:
        self.recipe = recipe
        self.correction_target = correction_target
        self.fail_planning = fail_planning
        self.plan_calls = 0
        self.check_calls = 0
        self._correction_sent = False

    def plan_for(self, goal: str) -> dict[str, Any]:
        """Return a complete provided plan accepted by SwarmOrchestrator.run."""

        self.plan_calls += 1
        return {
            "goal": goal,
            "recipe": self.recipe,
            "recipe_id": self.recipe,
            "task_type": self.recipe,
            "swarm_worthy": self.recipe != "single",
            "should_swarm": self.recipe != "single",
            "requires_confirmation": False,
            "needs_clarification": False,
            "risk_level": 2.0,
            "risk_gate": "auto",
            "max_parallel": 3,
            "planner": "fixture",
            "degraded": False,
            "required_constraints": [
                {"id": "C001", "text": "keep source evidence", "source": "test"}
            ],
        }

    async def ask(
        self,
        state: str,
        questions: Mapping[str, Any],
        timeout: float = 25.0,
    ) -> dict[str, Any]:
        del state, timeout
        question_ids = tuple(questions)
        if "swarm_worthy" in questions and ({"recipe_id", "task_type"} & set(questions)):
            self.plan_calls += 1
            if self.fail_planning:
                return {
                    "ok": False,
                    "answers": {},
                    "model": "jev-offline-fixture",
                    "usage": {},
                    "ms": 1,
                    "err": "fixture planner unavailable",
                    "need_topup": False,
                }
            is_swarm = self.recipe != "single"
            if "recipe_id" in questions:
                answers = {
                    "swarm_worthy": {"type": "noul", "noul": 1.0 if is_swarm else 0.0},
                    "recipe_id": {
                        "type": "choice",
                        "choice": self.recipe,
                        "confidence": 0.96,
                    },
                    "needs_clarification": {"type": "noul", "noul": 0.0},
                    "risk_score": {"type": "score", "score": 2.0},
                    "evidence_heavy": {"type": "noul", "noul": 1.0 if is_swarm else 0.0},
                    "parallelizable": {"type": "noul", "noul": 1.0 if is_swarm else 0.0},
                }
            else:
                answers = {
                    "swarm_worthy": {"type": "noul", "noul": 1.0 if is_swarm else 0.0},
                    "task_type": {
                        "type": "choice",
                        "choice": self.recipe,
                        "confidence": 0.96,
                    },
                    "needs_clarify": {"type": "noul", "noul": 0.0},
                    "risk_level": {"type": "score", "score": 2.0},
                    "evidence_heavy": {"type": "noul", "noul": 1.0 if is_swarm else 0.0},
                    "parallelizable": {"type": "noul", "noul": 1.0 if is_swarm else 0.0},
                }
            return {
                "ok": True,
                "answers": answers,
                "model": "jev-offline-fixture",
                "usage": {},
                "ms": 1,
                "err": None,
                "need_topup": False,
            }

        self.check_calls += 1
        bees = [
            question_id.removeprefix("correction_action_")
            for question_id in question_ids
            if question_id.startswith("correction_action_")
        ]
        answers: dict[str, Any] = {}
        for bee in bees:
            retry = bee == self.correction_target and not self._correction_sent
            action = "retry" if retry else "accept"
            answers[f"on_contract_{bee}"] = {
                "type": "noul",
                "noul": 0.0 if retry else 1.0,
            }
            answers[f"evidence_sufficient_{bee}"] = {
                "type": "noul",
                "noul": 0.0 if retry else 1.0,
            }
            answers[f"safe_to_continue_{bee}"] = {"type": "noul", "noul": 1.0}
            answers[f"conflict_present_{bee}"] = {"type": "noul", "noul": 0.0}
            answers[f"correction_action_{bee}"] = {
                "type": "choice",
                "choice": action,
                "confidence": 0.99,
            }
            if retry:
                self._correction_sent = True
        return {
            "ok": True,
            "answers": answers,
            "model": "jev-offline-fixture",
            "usage": {},
            "ms": 1,
            "err": None,
            "need_topup": False,
        }


@dataclass(slots=True)
class BeeCall:
    bee_id: str
    attempt: int
    started_at: float
    finished_at: float | None = None
    cancelled: bool = False


class MockBeeRunner:
    """Offline bee runner with observable concurrency and late-cancel output."""

    def __init__(
        self,
        *,
        parallel_bees: frozenset[str] = frozenset(),
        hold_bees: frozenset[str] = frozenset(),
    ) -> None:
        self.parallel_bees = parallel_bees
        self.hold_bees = hold_bees
        self.calls: list[BeeCall] = []
        self._attempts: Counter[str] = Counter()
        self._in_flight: set[str] = set()
        self.peak_parallel = 0
        self.parallel_started = asyncio.Event()
        self._parallel_release = asyncio.Event()
        self.cancel_release = asyncio.Event()
        self.late_delta_attempts = 0

    def calls_for(self, bee_id: str) -> list[BeeCall]:
        return [call for call in self.calls if call.bee_id == bee_id]

    def _output(
        self,
        bee_id: str,
        attempt: int,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        contract = context.get("contract", {})
        stream_events = [
            {
                "type": "bee.reasoning",
                "payload": {
                    "bee_id": bee_id,
                    "attempt": attempt,
                    "text": "fixture reasoning",
                },
            },
            {
                "type": "bee.delta",
                "payload": {
                    "bee_id": bee_id,
                    "attempt": attempt,
                    "text": f"{bee_id}:delta",
                },
            },
            {
                "type": "bee.tool_call",
                "payload": {
                    "bee_id": bee_id,
                    "tool": "fixture.read",
                    "attempt": attempt,
                },
            },
            {
                "type": "bee.tool_result",
                "payload": {
                    "bee_id": bee_id,
                    "tool": "fixture.read",
                    "ok": True,
                },
            },
        ]
        return {
            "status": "ok",
            "summary": f"{bee_id} completed attempt {attempt}",
            "text": f"{bee_id} completed attempt {attempt}",
            "task_id": str(contract.get("task_id") or "tsk_fixture"),
            "contract_rev": int(contract.get("contract_rev") or 1),
            "events": stream_events,
        }

    async def __call__(self, bee_id: str, context: Mapping[str, Any]) -> dict[str, Any]:
        self._attempts[bee_id] += 1
        attempt = int(context.get("attempt") or self._attempts[bee_id])
        call = BeeCall(bee_id, attempt, time.monotonic())
        self.calls.append(call)
        self._in_flight.add(bee_id)
        self.peak_parallel = max(self.peak_parallel, len(self._in_flight))
        try:
            if bee_id in self.parallel_bees and attempt == 1:
                if self.parallel_bees.issubset(self._in_flight):
                    self.parallel_started.set()
                    self._parallel_release.set()
                await asyncio.wait_for(self._parallel_release.wait(), timeout=2.0)

            if bee_id in self.hold_bees:
                try:
                    await self.cancel_release.wait()
                except asyncio.CancelledError:
                    call.cancelled = True
                    self.late_delta_attempts += 1
                    return {
                        "status": "cancelled-late-output",
                        "events": [
                            {
                                "type": "bee.delta",
                                "payload": {
                                    "bee_id": bee_id,
                                    "text": "late-after-cancel",
                                },
                            }
                        ],
                    }

            await asyncio.sleep(0)
            return self._output(bee_id, attempt, context)
        finally:
            call.finished_at = time.monotonic()
            self._in_flight.discard(bee_id)


class SwarmHarness:
    """Drive the current orchestrator interface and collect its event stream."""

    def __init__(
        self,
        root: Path,
        *,
        recipe: str,
        correction_target: str | None = None,
        fail_planning: bool = False,
        hold_bees: frozenset[str] = frozenset(),
    ) -> None:
        if not RUNTIME_READY or RUNTIME is None:
            raise RuntimeError(RUNTIME_SKIP_REASON)
        root.mkdir(parents=True, exist_ok=True)
        self.swarm = RUNTIME["swarm"]
        self.jev = MockJev(
            recipe,
            correction_target=correction_target,
            fail_planning=fail_planning,
        )
        stages = self.swarm.RECIPES[recipe]["stages"]
        parallel = (
            frozenset(stages[1])
            if len(stages) > 1 and len(stages[1]) > 1
            else frozenset()
        )
        self.bees = MockBeeRunner(
            parallel_bees=parallel,
            hold_bees=hold_bees,
        )
        self.orchestrator = self.swarm.SwarmOrchestrator(
            self.bees,
            self.jev.ask,
            max_parallel=3,
            max_jev_calls=5,
        )

    def plan(self, goal: str) -> dict[str, Any]:
        return self.jev.plan_for(goal)

    async def collect(
        self,
        goal: str,
        *,
        plan: Mapping[str, Any] | None = None,
        run_id: str,
        cancel_event: asyncio.Event | None = None,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        async for event in self.orchestrator.run(
            goal,
            plan=plan,
            permission_snapshot={"allowed_tools": ["fixture.read"]},
            cancel_event=cancel_event,
            confirmed=True,
            run_id=run_id,
        ):
            events.append(as_plain_dict(event))
        return events

    def start(
        self,
        goal: str,
        *,
        plan: Mapping[str, Any] | None,
        run_id: str,
        cancel_event: asyncio.Event,
    ) -> asyncio.Task[list[dict[str, Any]]]:
        return asyncio.create_task(
            self.collect(
                goal,
                plan=plan,
                run_id=run_id,
                cancel_event=cancel_event,
            )
        )
