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
    def execute(self, element: UIElement, action: str = "activate", text: str = "") -> Any: ...


class DryRunActionExecutor:
    """Action adapter that records intent and cannot touch the desktop."""

    def execute(self, element: UIElement, action: str = "activate", text: str = "") -> Mapping[str, Any]:
        detail = f"dry-run: would {action} {element.id}"
        if text:
            # Never echo the text itself; only record that a value would be typed.
            detail += f" with {len(text)} chars"
        return {
            "ok": True,
            "changed": False,
            "detail": detail,
        }


# Actions that write a value into the target; they require an editable element.
_TEXT_ACTIONS = frozenset({"type", "type_text", "input", "input_text", "fill", "set_value"})
# Actions that toggle/select; they require a selectable/interactive element.
_SELECT_ACTIONS = frozenset({"select", "choose", "toggle", "check", "uncheck"})
# Roles that can receive typed text.
_EDITABLE_ROLE_TOKENS = ("edit", "textbox", "text box", "input", "document", "search")
# Roles that expose a selection.
_SELECTABLE_ROLE_TOKENS = (
    "combo", "combobox", "list", "listitem", "menu", "menuitem",
    "radio", "checkbox", "tab", "tabitem", "tree", "treeitem", "option",
)


def _role_allows_action(role: str, action: str) -> bool:
    """Conservative role/action compatibility check.

    Unknown roles are permissive (we cannot enumerate every UI framework); only
    clearly incompatible pairs — typing into a non-editable control, or selecting
    a non-selectable one — are rejected. This is a guard against the model
    choosing a plausible id for the wrong action, not a full a11y model.
    """

    normalized_role = role.strip().lower().replace("_", " ")
    normalized_action = action.strip().lower().replace("_", "")
    if normalized_action in {a.replace("_", "") for a in _TEXT_ACTIONS}:
        if not normalized_role:
            return True
        return any(token in normalized_role for token in _EDITABLE_ROLE_TOKENS)
    if normalized_action in {a.replace("_", "") for a in _SELECT_ACTIONS}:
        if not normalized_role:
            return True
        if any(token in normalized_role for token in _SELECTABLE_ROLE_TOKENS):
            return True
        # A button/link can also "select" in loose UIs; only reject clearly inert roles.
        return normalized_role not in {"text", "static", "image", "group", "pane", "window"}
    return True


def validate_choice(
    choice: Any,
    snapshot: Snapshot,
    *,
    action: str = "",
) -> UIElement:
    """Return the chosen candidate, or reject an ungrounded/incompatible choice.

    ``action`` is optional; when supplied the element's role is checked for
    compatibility (typing needs an editable control, selecting needs a
    selectable one). Backward compatible: callers that pass no action get the
    original id-only validation.
    """

    element_id = _choice_id(choice)
    if not element_id:
        raise ValueError("choice must include an element id")
    candidate = snapshot.candidate(element_id)
    if candidate is None:
        raise ValueError(f"unknown element id: {element_id}")
    if action and not _role_allows_action(candidate.role, action):
        raise ValueError(
            f"action {action!r} is incompatible with element {element_id} role {candidate.role!r}"
        )
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


def _choice_text(choice: Any) -> str:
    """Extract an optional verbatim text value the chooser resolved for typing.

    The text always originates from a code-selected span (select-not-generate);
    the model may only point at it, never author it. An empty string means no
    value is attached, so the action is not a text-bearing one.
    """

    if isinstance(choice, Mapping):
        for key in ("text", "value", "input_text", "text_value"):
            value = choice.get(key)
            if value is not None:
                return str(value)
        return ""
    for name in ("text", "value", "input_text", "text_value"):
        value = getattr(choice, name, None)
        if value is not None:
            return str(value)
    return ""


def _choice_stuck(choice: Any) -> bool:
    """True when the chooser reports no element can advance the goal.

    A distinct ``stuck`` signal (doc 12 §6.2) keeps "the model correctly found
    no way forward" separate from "the model named an id that is not in the
    candidate set" (invalid_choice), so the loop can report the honest reason.
    """

    if isinstance(choice, Mapping):
        if choice.get("stuck") is True:
            return True
        action = str(choice.get("action") or choice.get("kind") or "").strip().lower()
        return action in {"stuck", "blocked"}
    if getattr(choice, "stuck", False) is True:
        return True
    action = str(getattr(choice, "action", "") or "").strip().lower()
    return action in {"stuck", "blocked"}


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


def _execute_action(executor: Any, element: UIElement, action: str, text: str = "") -> Any:
    method = getattr(executor, "execute", None)
    target = method if callable(method) else executor
    if not callable(target):
        raise TypeError("action executor must be callable or expose execute()")
    # Prefer the 3-arg form (element, action, text) for text-bearing actions;
    # fall back to older 2-arg / 1-arg executors. _call_with_signature binds by
    # signature, so an internal TypeError is never mistaken for arity mismatch.
    if text:
        return _call_with_signature(target, ((element, action, text), (element, action), (element,)))
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

    def validate_choice(self, choice: Any, snapshot: Snapshot, *, action: str = "") -> UIElement:
        return validate_choice(choice, snapshot, action=action)

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
            if _choice_stuck(choice):
                return GoalResult("stuck", goal_text, tuple(steps), snapshot, "chooser reported no way forward")

            action_name = _choice_action(choice)
            try:
                element = self.validate_choice(choice, snapshot, action=action_name)
            except ValueError as exc:
                return GoalResult("invalid_choice", goal_text, tuple(steps), snapshot, str(exc))
            choice_text = _choice_text(choice)

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

            action_result = _execute_action(self.action, element, action_name, choice_text)
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
