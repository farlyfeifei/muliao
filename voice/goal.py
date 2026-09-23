"""Injectable, dry-run-first GOAL loop for multi-step UI tasks."""
from __future__ import annotations

from dataclasses import dataclass, field
import inspect
import time
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from .perception import Snapshot, UIElement

MAX_STEPS = 8


@dataclass(frozen=True)
class GoalStep:
    """One chooser decision and its observed outcome."""

    index: int
    element_id: str
    action: str = "activate"
    status: str = "planned"
    changed: bool = False
    dry_run: bool = True
    element: UIElement | None = None
    detail: str = ""
    before_state: str = field(default="", repr=False, compare=False)
    after_state: str = field(default="", repr=False, compare=False)

    @property
    def id(self) -> str:
        return self.element_id


@dataclass(frozen=True)
class GoalResult:
    status: str
    goal: str
    steps: tuple[GoalStep, ...] = ()
    snapshot: Snapshot | None = None
    detail: str = ""

    @property
    def completed(self) -> bool:
        return self.status == "completed"


@runtime_checkable
class PerceptionAdapter(Protocol):
    def capture(self) -> Snapshot: ...


@runtime_checkable
class GoalChooser(Protocol):
    def choose(self, goal: str, state: str, step: int) -> Any: ...


@runtime_checkable
class GoalActionExecutor(Protocol):
    def execute(self, element: UIElement, action: str = "activate") -> Any: ...


class DryRunActionExecutor:
    """Action adapter that records intent and cannot touch the desktop."""

    def execute(self, element: UIElement, action: str = "activate") -> Mapping[str, Any]:
        return {
            "ok": True,
            "changed": False,
            "detail": f"dry-run: would {action} {element.id}",
        }


def validate_choice(choice: Any, snapshot: Snapshot) -> UIElement:
    """Return the chosen candidate or reject an ungrounded element id."""

    element_id = _choice_id(choice)
    if not element_id:
        raise ValueError("choice must include an element id")
    candidate = snapshot.candidate(element_id)
    if candidate is None:
        raise ValueError(f"unknown element id: {element_id}")
    return candidate


def _choice_id(choice: Any) -> str:
    if isinstance(choice, GoalStep):
        return choice.element_id.strip()
    if isinstance(choice, str):
        return choice.strip()
    if isinstance(choice, Mapping):
        for key in ("id", "element_id", "elementId", "target"):
            value = choice.get(key)
            if value is not None:
                return str(value).strip()
        return ""
    for name in ("id", "element_id", "elementId", "target"):
        value = getattr(choice, name, None)
        if value is not None:
            return str(value).strip()
    return ""


def _choice_action(choice: Any) -> str:
    if isinstance(choice, GoalStep):
        return choice.action or "activate"
    if isinstance(choice, Mapping):
        return str(choice.get("action") or choice.get("kind") or "activate").strip() or "activate"
    return str(getattr(choice, "action", "activate") or "activate").strip() or "activate"


def _choice_complete(choice: Any) -> bool:
    if isinstance(choice, Mapping):
        return bool(choice.get("complete") or choice.get("completed") or choice.get("done"))
    return bool(
        getattr(choice, "complete", False)
        or getattr(choice, "completed", False)
        or getattr(choice, "done", False)
    )


def _safe_goal(value: Any) -> str:
    # Keep the goal bounded too: it is sent with state to the chooser.
    return " ".join(str(value or "").split())[:24_000]


def _call_with_signature(callable_obj: Callable[..., Any], variants: tuple[tuple[Any, ...], ...]) -> Any:
    """Select a compatible injected-call signature without swallowing body errors."""

    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return callable_obj(*variants[0])
    for args in variants:
        try:
            signature.bind(*args)
        except TypeError:
            continue
        return callable_obj(*args)
    raise TypeError("injected callable has an unsupported signature")


def _invoke_chooser(chooser: Any, goal: str, snapshot: Snapshot, step: int) -> Any:
    method = getattr(chooser, "choose", None)
    target = method if callable(method) else chooser
    if not callable(target):
        raise TypeError("chooser must be callable or expose choose()")
    # The preferred interface receives text state, never a screenshot or image.
    return _call_with_signature(
        target,
        (
            (goal, snapshot.state, step),
            (goal, snapshot.state),
            (snapshot.state,),
        ),
    )


def _execute_action(executor: Any, element: UIElement, action: str) -> Any:
    method = getattr(executor, "execute", None)
    target = method if callable(method) else executor
    if not callable(target):
        raise TypeError("action executor must be callable or expose execute()")
    return _call_with_signature(target, ((element, action), (element,)))


def _action_ok(result: Any) -> bool:
    if result is None:
        return True
    if isinstance(result, bool):
        return result
    if isinstance(result, Mapping):
        return bool(result.get("ok", True))
    return bool(getattr(result, "ok", True))


def _action_changed(result: Any) -> bool | None:
    if isinstance(result, Mapping) and "changed" in result:
        return bool(result["changed"])
    if hasattr(result, "changed"):
        return bool(getattr(result, "changed"))
    return None


def _action_detail(result: Any) -> str:
    if result is None or isinstance(result, bool):
        return ""
    if isinstance(result, Mapping):
        return str(result.get("detail") or result.get("message") or "")
    return str(getattr(result, "detail", "") or getattr(result, "message", ""))


def _snapshots_changed(before: Snapshot, after: Snapshot) -> bool:
    return before.signature != after.signature


class GoalLoop:
    """A bounded perceive/choose/validate/act/settle loop.

    Dry-run is the default.  In that mode the action adapter is never invoked,
    which makes the default and test paths strictly side-effect free.
    """

    def __init__(
        self,
        perception: PerceptionAdapter | Callable[[], Snapshot],
        chooser: GoalChooser | Callable[..., Any],
        action: GoalActionExecutor | Callable[..., Any] | None = None,
        *,
        executor: GoalActionExecutor | Callable[..., Any] | None = None,
        dry_run: bool = True,
        max_steps: int = MAX_STEPS,
        settle_seconds: float = 0.0,
        settle: Callable[[float], None] = time.sleep,
        sleeper: Callable[[float], None] | None = None,
        stall_limit: int = 1,
    ) -> None:
        if max_steps < 1 or max_steps > MAX_STEPS:
            raise ValueError(f"max_steps must be between 1 and {MAX_STEPS}")
        if settle_seconds < 0:
            raise ValueError("settle_seconds cannot be negative")
        if stall_limit < 1:
            raise ValueError("stall_limit must be at least 1")
        if action is not None and executor is not None:
            raise ValueError("pass action or executor, not both")
        self.perception = perception
        self.chooser = chooser
        self.action = action or executor or DryRunActionExecutor()
        self.dry_run = bool(dry_run)
        self.max_steps = int(max_steps)
        self.settle_seconds = float(settle_seconds)
        self.settle = sleeper or settle
        self.stall_limit = int(stall_limit)

    def validate_choice(self, choice: Any, snapshot: Snapshot) -> UIElement:
        return validate_choice(choice, snapshot)

    def run(self, goal: str) -> GoalResult:
        goal_text = _safe_goal(goal)
        if not goal_text:
            raise ValueError("goal cannot be empty")

        snapshot = self._capture()
        if not snapshot.elements:
            return GoalResult("no_candidates", goal_text, snapshot=snapshot, detail="no safe candidates")

        steps: list[GoalStep] = []
        stalls = 0
        for index in range(1, self.max_steps + 1):
            choice = _invoke_chooser(self.chooser, goal_text, snapshot, index)
            if _choice_complete(choice):
                return GoalResult("completed", goal_text, tuple(steps), snapshot)

            try:
                element = self.validate_choice(choice, snapshot)
            except ValueError as exc:
                return GoalResult("invalid_choice", goal_text, tuple(steps), snapshot, str(exc))
            action_name = _choice_action(choice)

            if self.dry_run:
                steps.append(
                    GoalStep(
                        index=index,
                        element_id=element.id,
                        action=action_name,
                        status="dry_run",
                        dry_run=True,
                        element=element,
                        detail=f"dry-run: would {action_name} {element.id}",
                        before_state=snapshot.state,
                        after_state=snapshot.state,
                    )
                )
                return GoalResult("dry_run", goal_text, tuple(steps), snapshot)

            action_result = _execute_action(self.action, element, action_name)
            if not _action_ok(action_result):
                step = GoalStep(
                    index=index,
                    element_id=element.id,
                    action=action_name,
                    status="action_failed",
                    dry_run=False,
                    element=element,
                    detail=_action_detail(action_result),
                    before_state=snapshot.state,
                    after_state=snapshot.state,
                )
                steps.append(step)
                return GoalResult("action_failed", goal_text, tuple(steps), snapshot, step.detail)

            if self.settle_seconds:
                self.settle(self.settle_seconds)
            after = self._capture()
            declared_change = _action_changed(action_result)
            changed = _snapshots_changed(snapshot, after) if declared_change is None else declared_change
            step = GoalStep(
                index=index,
                element_id=element.id,
                action=action_name,
                status="changed" if changed else "stalled",
                changed=changed,
                dry_run=False,
                element=element,
                detail=_action_detail(action_result),
                before_state=snapshot.state,
                after_state=after.state,
            )
            steps.append(step)
            snapshot = after

            if changed:
                stalls = 0
            else:
                stalls += 1
                if stalls >= self.stall_limit:
                    return GoalResult("stalled", goal_text, tuple(steps), snapshot, "UI did not change")

        return GoalResult(
            "step_limit",
            goal_text,
            tuple(steps),
            snapshot,
            f"maximum of {self.max_steps} steps reached",
        )

    def _capture(self) -> Snapshot:
        method = getattr(self.perception, "capture", None)
        target = method if callable(method) else self.perception
        if not callable(target):
            raise TypeError("perception must be callable or expose capture()")
        raw = target()
        if isinstance(raw, Snapshot):
            return raw
        if isinstance(raw, Mapping):
            raw = raw.get("elements", ())
        return Snapshot(tuple(raw or ()), source="injected")


__all__ = [
    "DryRunActionExecutor",
    "GoalActionExecutor",
    "GoalChooser",
    "GoalLoop",
    "GoalResult",
    "GoalStep",
    "MAX_STEPS",
    "PerceptionAdapter",
    "validate_choice",
]
