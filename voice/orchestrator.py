"""Bridge FAST routing/execution with the bounded GOAL loop and voice context.

The orchestrator is deliberately independent from :mod:`voice.engine` and the
server.  It accepts already-built components, stages context changes until an
operation has passed permission and cancellation checks, and keeps GOAL dry-run
unless the caller explicitly passes ``act=True``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import copy
import inspect
import re
from typing import Any, Callable, Mapping, Sequence

from .cancellation import VoiceCancelled
from .context import (
    AppContext,
    ContextSnapshot,
    VoiceContext,
    build_jev_state,
    sanitize_elements,
    serialize_jev_state,
)
from .contracts import ActionResult, RouteDecision
from .goal import DryRunActionExecutor, GoalLoop, GoalResult, GoalStep
from .perception import ScreenPerception, Snapshot, UIElement

FAST = "FAST"
GOAL = "GOAL"
NONE = "NONE"

_UNSET = object()
_SENSITIVE_RE = re.compile(
    r"(?:password|passwd|secret|token|cookie|credential|api[ _-]?key|one[ _-]?time[ _-]?code|"
    r"passcode|\botp\b|\bpin\b|密码|口令|密钥|令牌|验证码|信用卡|银行卡)",
    re.IGNORECASE,
)
_CONTENT_BEARING_KINDS = frozenset({"open_url", "search", "type", "type_text", "input_text"})
_PERMISSION_CODES = frozenset({"permission_denied", "permission_revoked", "unauthorized", "forbidden"})
_CANCEL_CODES = frozenset({"cancelled", "canceled", "stale"})


@dataclass(frozen=True)
class OrchestratorResult:
    """Unified result without changing the existing FAST or GOAL contracts."""

    status: str
    mode: str = NONE
    command: str = ""
    decision: RouteDecision | None = None
    action: ActionResult | None = None
    goal: GoalResult | Any | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        if self.mode == FAST:
            return bool(self.action and self.action.ok and self.status == "completed")
        if self.mode == GOAL:
            return self.status in {"completed", "dry_run"}
        return False

    @property
    def completed(self) -> bool:
        return self.status == "completed"


@dataclass(frozen=True)
class _ContextRecord:
    said: str
    action: str
    target: str = ""
    ok: bool = False
    outcome: str = "failed"
    detail: str = ""


@dataclass
class _ContextUpdate:
    records: list[_ContextRecord] = field(default_factory=list)
    foreground: Any = _UNSET
    elements: Any = _UNSET

    @property
    def empty(self) -> bool:
        return not self.records and self.foreground is _UNSET and self.elements is _UNSET


@dataclass(frozen=True)
class _StagedResult:
    result: OrchestratorResult
    update: _ContextUpdate = field(default_factory=_ContextUpdate)


class _AllowAllPermission:
    def allowed(self) -> bool:
        return True

    def run_if_allowed(self, callback: Callable[[], Any]) -> tuple[bool, Any]:
        return True, callback()


class _CapturingPerception:
    """Cancellation-aware perception proxy that retains only safe snapshots."""

    def __init__(self, source: Any, cancellation: Any = None) -> None:
        self.source = source
        self.cancellation = cancellation
        self.latest: Snapshot | None = None
        self.foreground: Any = _UNSET

    def capture(self) -> Snapshot:
        _raise_if_cancelled(self.cancellation)
        raw = _invoke_capture(self.source)
        _raise_if_cancelled(self.cancellation)
        snapshot, foreground = _normalise_snapshot(raw)
        self.latest = snapshot
        if foreground is not _UNSET:
            self.foreground = foreground
        return snapshot


class _ChooserProxy:
    """Give an existing chooser a bounded reference-aware state string."""

    def __init__(
        self,
        chooser: Any,
        context: VoiceContext,
        perception: _CapturingPerception,
        cancellation: Any,
        foreground: Any,
    ) -> None:
        self.chooser = chooser
        self.context = context
        self.perception = perception
        self.cancellation = cancellation
        self.foreground = foreground

    def choose(self, goal: str, screen_state: str, step: int) -> Any:
        _raise_if_cancelled(self.cancellation)
        reference_state = _build_reference_state(
            self.context,
            goal,
            foreground=self.foreground,
            snapshot=self.perception.latest,
        )
        state_text = serialize_jev_state(reference_state)
        target = _call_target(self.chooser, "choose")
        result = _call_compatible(
            target,
            (
                ((goal, state_text, step), {"context_state": reference_state, "screen_state": screen_state}),
                ((goal, state_text, step), {"context_state": reference_state}),
                ((goal, state_text, step), {"screen_state": screen_state}),
                ((goal, state_text, step), {}),
                ((goal, state_text), {}),
                ((state_text,), {}),
            ),
        )
        _raise_if_cancelled(self.cancellation)
        return result


class _ActionProxy:
    def __init__(self, action: Any, cancellation: Any) -> None:
        self.action = action
        self.cancellation = cancellation

    def execute(self, element: UIElement, action: str = "activate") -> Any:
        _raise_if_cancelled(self.cancellation)
        target = _call_target(self.action, "execute")
        result = _call_compatible(target, (((element, action), {}), ((element,), {})))
        _raise_if_cancelled(self.cancellation)
        return result


class VoiceOrchestrator:
    """Route a command to FAST or GOAL while maintaining bounded context.

    ``fast_engine`` may be an aggregate exposing ``router`` and ``executor``, or
    an object exposing ``route`` and/or ``execute`` directly.  Explicit
    ``router``/``executor``/``chooser`` injections take precedence.

    No chat session or cache object is accepted or touched.  Context mutations
    are staged until permission and cancellation are checked a second time.
    """

    def __init__(
        self,
        fast_engine: Any = None,
        goal_loop: Any = None,
        context: VoiceContext | None = None,
        perception: Any = None,
        *,
        router: Any = None,
        executor: Any = None,
        chooser: Any = None,
        goal_action: Any = None,
        permission: Any = None,
        foreground: Any = None,
        foreground_provider: Any = None,
    ) -> None:
        self.fast_engine = fast_engine
        self.goal_loop = goal_loop
        self.context = context or VoiceContext()
        self.perception = (
            perception
            if perception is not None
            else getattr(goal_loop, "perception", None) or ScreenPerception()
        )
        self.router = router if router is not None else self._component("router", "route")
        self.executor = executor if executor is not None else self._component("executor", "execute")
        self.chooser = chooser
        self.goal_action = goal_action
        self.permission = permission or _AllowAllPermission()
        self.foreground_provider = (
            foreground_provider if foreground_provider is not None else foreground
        )

    def process(
        self,
        command: str,
        *,
        decision: RouteDecision | Mapping[str, Any] | Any | None = None,
        goal: str | None = None,
        act: bool = False,
        cancellation: Any = None,
        foreground: Any = _UNSET,
    ) -> OrchestratorResult:
        """Route and run one command.

        An explicit ``goal`` bypasses the FAST router.  Otherwise ``kind=goal``
        or ``needs_screen`` selects GOAL even when JevFastRouter rejected that
        decision, because its allowlist intentionally excludes GOAL.
        """

        if goal is not None:
            return self.process_goal(
                goal,
                act=act,
                cancellation=cancellation,
                foreground=foreground,
            )

        command_text = _bounded_text(command, 24_000)
        if not command_text:
            return OrchestratorResult("rejected", command=command_text, detail="command cannot be empty")
        return self._authorized_operation(
            lambda: self._process_command(
                command_text,
                decision=decision,
                act=act,
                cancellation=cancellation,
                foreground=foreground,
            ),
            command=command_text,
            cancellation=cancellation,
        )

    def process_decision(
        self,
        command: str,
        decision: RouteDecision | Mapping[str, Any] | Any,
        *,
        act: bool = False,
        cancellation: Any = None,
        foreground: Any = _UNSET,
    ) -> OrchestratorResult:
        """Execute a pre-routed decision through the same safety path."""

        return self.process(
            command,
            decision=decision,
            act=act,
            cancellation=cancellation,
            foreground=foreground,
        )

    def process_goal(
        self,
        goal: str,
        *,
        act: bool = False,
        cancellation: Any = None,
        foreground: Any = _UNSET,
    ) -> OrchestratorResult:
        """Enter GOAL explicitly; dry-run remains the default."""

        goal_text = _bounded_text(goal, 24_000)
        if not goal_text:
            return OrchestratorResult("rejected", mode=GOAL, command=goal_text, detail="goal cannot be empty")
        return self._authorized_operation(
            lambda: self._process_goal_allowed(
                goal_text,
                act=act,
                cancellation=cancellation,
                foreground=foreground,
            ),
            command=goal_text,
            cancellation=cancellation,
            mode=GOAL,
        )

    def clear(self) -> None:
        """Clear all short-lived reference context."""

        self.context.clear()

    def purge(self) -> bool:
        """Purge expired reference context and report whether it changed."""

        return bool(self.context.purge())

    clear_context = clear
    purge_context = purge

    def build_state(self, utterance: str) -> dict[str, Any]:
        """Expose the same bounded state supplied to reference-aware routers."""

        return self.context.build_state(utterance)

    def _component(self, attribute: str, direct_method: str) -> Any:
        if self.fast_engine is None:
            return None
        nested = getattr(self.fast_engine, attribute, None)
        if nested is not None:
            return nested
        if callable(getattr(self.fast_engine, direct_method, None)):
            return self.fast_engine
        return None

    def _authorized_operation(
        self,
        callback: Callable[[], _StagedResult],
        *,
        command: str,
        cancellation: Any,
        mode: str = NONE,
    ) -> OrchestratorResult:
        try:
            _raise_if_cancelled(cancellation)
            allowed, staged = self._permission_call(callback)
        except VoiceCancelled:
            return OrchestratorResult("cancelled", mode=mode, command=command)
        except Exception as exc:
            return OrchestratorResult(
                "permission_error",
                mode=mode,
                command=command,
                detail=f"{type(exc).__name__}: {exc}",
            )
        if not allowed or staged is None:
            return OrchestratorResult("permission_denied", mode=mode, command=command)
        if not isinstance(staged, _StagedResult):
            return OrchestratorResult(
                "orchestrator_error",
                mode=mode,
                command=command,
                detail="authorized callback returned an invalid result",
            )

        if staged.result.status in {"cancelled", "permission_denied"}:
            return staged.result
        try:
            _raise_if_cancelled(cancellation)
        except VoiceCancelled:
            return replace(staged.result, status="cancelled", detail="")
        if staged.update.empty:
            return staged.result

        try:
            allowed, committed = self._permission_call(
                lambda: self._commit_if_current(staged.update, cancellation)
            )
        except VoiceCancelled:
            return replace(staged.result, status="cancelled", detail="")
        except Exception as exc:
            return replace(
                staged.result,
                status="permission_error",
                detail=f"{type(exc).__name__}: {exc}",
            )
        if not allowed:
            return replace(staged.result, status="permission_denied", detail="")
        if not committed:
            return replace(staged.result, status="cancelled", detail="")
        return staged.result

    def _permission_call(self, callback: Callable[[], Any]) -> tuple[bool, Any]:
        runner = getattr(self.permission, "run_if_allowed", None)
        if callable(runner):
            result = runner(callback)
            if isinstance(result, tuple) and len(result) == 2:
                return bool(result[0]), result[1]
            return bool(result), None
        allowed_method = getattr(self.permission, "allowed", None)
        if callable(allowed_method):
            if not allowed_method():
                return False, None
            return True, callback()
        if callable(self.permission):
            if not self.permission():
                return False, None
            return True, callback()
        return False, None

    def _process_command(
        self,
        command: str,
        *,
        decision: Any,
        act: bool,
        cancellation: Any,
        foreground: Any,
    ) -> _StagedResult:
        _raise_if_cancelled(cancellation)
        if foreground is not _UNSET:
            reference_foreground = _safe_app(foreground)
        else:
            reference_foreground = self._read_foreground_provider()
            _raise_if_cancelled(cancellation)
        if decision is None:
            if self.router is None:
                return _StagedResult(
                    OrchestratorResult("routing_failed", command=command, detail="FAST router is not configured")
                )
            state = _build_reference_state(
                self.context,
                command,
                foreground=reference_foreground,
            )
            _raise_if_cancelled(cancellation)
            try:
                raw_decision = _invoke_router(self.router, command, state)
            except VoiceCancelled:
                raise
            except Exception as exc:
                return _StagedResult(
                    OrchestratorResult(
                        "routing_failed",
                        command=command,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
        else:
            raw_decision = decision
        _raise_if_cancelled(cancellation)

        try:
            routed = _coerce_decision(raw_decision)
        except Exception as exc:
            return _StagedResult(
                OrchestratorResult(
                    "routing_failed",
                    command=command,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )

        if _decision_needs_screen(raw_decision, routed):
            staged = self._process_goal_allowed(
                command,
                act=act,
                cancellation=cancellation,
                foreground=foreground,
            )
            return _StagedResult(replace(staged.result, decision=routed), staged.update)

        if not routed.accepted:
            return _StagedResult(
                OrchestratorResult(
                    "rejected",
                    mode=FAST,
                    command=command,
                    decision=routed,
                    detail=routed.reason,
                )
            )
        return self._process_fast_allowed(
            command,
            routed,
            cancellation=cancellation,
            foreground=foreground,
        )

    def _process_fast_allowed(
        self,
        command: str,
        decision: RouteDecision,
        *,
        cancellation: Any,
        foreground: Any,
    ) -> _StagedResult:
        if self.executor is None:
            return _StagedResult(
                OrchestratorResult(
                    "action_failed",
                    mode=FAST,
                    command=command,
                    decision=decision,
                    detail="FAST executor is not configured",
                )
            )
        _raise_if_cancelled(cancellation)
        try:
            raw_action = _invoke_executor(self.executor, decision)
            action = _coerce_action_result(raw_action, decision)
        except VoiceCancelled:
            raise
        except Exception as exc:
            action = ActionResult(
                False,
                _decision_action_name(decision),
                f"{type(exc).__name__}: {exc}",
                {"code": "fast_action_failed"},
            )

        code = _result_code(action)
        if code in _CANCEL_CODES:
            return _StagedResult(
                OrchestratorResult(
                    "cancelled",
                    mode=FAST,
                    command=command,
                    decision=decision,
                    action=action,
                )
            )
        if code in _PERMISSION_CODES:
            return _StagedResult(
                OrchestratorResult(
                    "permission_denied",
                    mode=FAST,
                    command=command,
                    decision=decision,
                    action=action,
                )
            )
        if action.ok and action.metadata.get("committed", True) is False:
            action = ActionResult(
                False,
                action.action,
                action.detail or "action did not commit",
                {**dict(action.metadata), "committed": False},
            )

        _raise_if_cancelled(cancellation)
        snapshot: Snapshot | None = None
        perceived_foreground: Any = _UNSET
        try:
            raw_snapshot = _invoke_capture(self.perception)
            snapshot, perceived_foreground = _normalise_snapshot(raw_snapshot)
        except VoiceCancelled:
            raise
        except Exception:
            # Perception refresh is best-effort and must not rewrite action truth.
            snapshot = None
        _raise_if_cancelled(cancellation)

        staged_foreground = self._resolve_foreground(
            explicit=foreground,
            perceived=perceived_foreground,
            decision=decision,
            action=action,
        )
        update = _ContextUpdate(
            foreground=staged_foreground,
            elements=_context_elements(snapshot) if snapshot is not None else _UNSET,
        )
        update.records.append(
            _fast_record(command, decision, action)
        )
        status = "completed" if action.ok else "action_failed"
        return _StagedResult(
            OrchestratorResult(
                status,
                mode=FAST,
                command=command,
                decision=decision,
                action=action,
                detail="" if action.ok else action.detail,
            ),
            update,
        )

    def _process_goal_allowed(
        self,
        goal: str,
        *,
        act: bool,
        cancellation: Any,
        foreground: Any,
    ) -> _StagedResult:
        _raise_if_cancelled(cancellation)
        initial_foreground = (
            _safe_app(foreground)
            if foreground is not _UNSET
            else self._read_foreground_provider()
        )
        if initial_foreground is None and foreground is _UNSET:
            initial_foreground = _UNSET
        capture = _CapturingPerception(self.perception, cancellation)
        try:
            result = self._run_goal_loop(
                goal,
                act=act is True,
                cancellation=cancellation,
                capture=capture,
                foreground=initial_foreground,
            )
        except VoiceCancelled:
            raise
        except Exception as exc:
            return _StagedResult(
                OrchestratorResult(
                    "goal_failed",
                    mode=GOAL,
                    command=goal,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
        _raise_if_cancelled(cancellation)

        status = _goal_status(result)
        if status in _CANCEL_CODES:
            return _StagedResult(
                OrchestratorResult("cancelled", mode=GOAL, command=goal, goal=result)
            )
        if status in _PERMISSION_CODES:
            return _StagedResult(
                OrchestratorResult("permission_denied", mode=GOAL, command=goal, goal=result)
            )
        if act and status == "dry_run":
            return _StagedResult(
                OrchestratorResult(
                    "goal_failed",
                    mode=GOAL,
                    command=goal,
                    goal=result,
                    detail="injected GOAL loop did not honor act=True",
                )
            )
        if not act and _goal_has_committed_step(result):
            return _StagedResult(
                OrchestratorResult(
                    "goal_failed",
                    mode=GOAL,
                    command=goal,
                    goal=result,
                    detail="injected GOAL loop acted during dry-run",
                )
            )

        final_snapshot = _goal_snapshot(result) or capture.latest
        perceived_foreground = capture.foreground
        staged_foreground = self._resolve_foreground(
            explicit=foreground,
            perceived=perceived_foreground,
        )
        if staged_foreground is _UNSET and initial_foreground is not _UNSET:
            staged_foreground = initial_foreground
        update = _ContextUpdate(
            foreground=staged_foreground,
            elements=_context_elements(final_snapshot) if final_snapshot is not None else _UNSET,
        )
        update.records.extend(_goal_records(goal, result))
        detail = _goal_detail(result)
        return _StagedResult(
            OrchestratorResult(
                status,
                mode=GOAL,
                command=goal,
                goal=result,
                detail=detail,
            ),
            update,
        )

    def _run_goal_loop(
        self,
        goal: str,
        *,
        act: bool,
        cancellation: Any,
        capture: _CapturingPerception,
        foreground: Any,
    ) -> Any:
        base_loop = self.goal_loop
        original_chooser = self.chooser or getattr(base_loop, "chooser", None)
        if original_chooser is None:
            raise RuntimeError("GOAL chooser is not configured")
        chooser = _ChooserProxy(
            original_chooser,
            self.context,
            capture,
            cancellation,
            foreground,
        )

        if base_loop is None:
            action = self.goal_action or DryRunActionExecutor()
            if act:
                action = _ActionProxy(action, cancellation)
            loop: Any = GoalLoop(
                capture,
                chooser,
                action=action,
                dry_run=not act,
            )
        else:
            loop = copy.copy(base_loop)
            for name, value in (
                ("perception", capture),
                ("chooser", chooser),
                ("dry_run", not act),
            ):
                try:
                    setattr(loop, name, value)
                except (AttributeError, TypeError):
                    pass
            action = self.goal_action or getattr(base_loop, "action", None)
            if act and action is not None:
                try:
                    setattr(loop, "action", _ActionProxy(action, cancellation))
                except (AttributeError, TypeError):
                    pass

        run = _call_target(loop, "run")
        initial_state = _build_reference_state(
            self.context,
            goal,
            foreground=foreground,
        )
        return _call_compatible(
            run,
            (
                ((goal,), {"act": act, "dry_run": not act, "state": initial_state}),
                ((goal,), {"dry_run": not act, "state": initial_state}),
                ((goal,), {"act": act, "state": initial_state}),
                ((goal,), {"dry_run": not act}),
                ((goal,), {"act": act}),
                ((goal,), {}),
            ),
        )

    def _read_foreground_provider(self) -> Any:
        provider = self.foreground_provider
        if provider is None:
            return _UNSET
        target = None
        for name in ("get_foreground_app", "foreground_app", "current_app", "current"):
            candidate = getattr(provider, name, None)
            if callable(candidate):
                target = candidate
                break
            if candidate is not None:
                return _safe_app(candidate)
        if target is None and callable(provider):
            target = provider
        if target is None:
            return _UNSET
        try:
            return _safe_app(target())
        except Exception:
            return _UNSET

    def _resolve_foreground(
        self,
        *,
        explicit: Any = _UNSET,
        perceived: Any = _UNSET,
        decision: RouteDecision | None = None,
        action: ActionResult | None = None,
    ) -> Any:
        if explicit is not _UNSET:
            return _safe_app(explicit)
        if perceived is not _UNSET:
            return _safe_app(perceived)
        provided = self._read_foreground_provider()
        if provided is not _UNSET:
            return provided
        if (
            decision is not None
            and action is not None
            and action.ok
            and decision.kind.strip().casefold() == "open_app"
        ):
            return _safe_app({"name": decision.target})
        return _UNSET

    def _commit_if_current(self, update: _ContextUpdate, cancellation: Any) -> bool:
        _raise_if_cancelled(cancellation)
        if update.foreground is not _UNSET:
            self.context.set_foreground_app(update.foreground)
        if update.elements is not _UNSET:
            self.context.set_elements(update.elements)
        for record in update.records:
            self.context.record_action(
                said=record.said,
                action=record.action,
                target=record.target,
                ok=record.ok,
                outcome=record.outcome,
                detail=record.detail,
            )
        return True


def _call_target(value: Any, method_name: str) -> Callable[..., Any]:
    method = getattr(value, method_name, None)
    target = method if callable(method) else value
    if not callable(target):
        raise TypeError(f"injected component must be callable or expose {method_name}()")
    return target


def _call_compatible(
    target: Callable[..., Any],
    variants: Sequence[tuple[tuple[Any, ...], Mapping[str, Any]]],
) -> Any:
    """Choose a compatible signature without swallowing callable-body errors."""

    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        args, kwargs = variants[0]
        return target(*args, **dict(kwargs))
    for args, kwargs in variants:
        try:
            signature.bind(*args, **dict(kwargs))
        except TypeError:
            continue
        return target(*args, **dict(kwargs))
    raise TypeError("injected callable has an unsupported signature")


def _invoke_router(router: Any, command: str, state: Mapping[str, Any]) -> Any:
    target = _call_target(router, "route")
    return _call_compatible(
        target,
        (
            ((command,), {"state": state}),
            ((command,), {"context_state": state}),
            ((command, state), {}),
            ((command,), {}),
        ),
    )


def _invoke_executor(executor: Any, decision: RouteDecision) -> Any:
    target = _call_target(executor, "execute")
    return _call_compatible(target, (((decision,), {}),))


def _invoke_capture(perception: Any) -> Any:
    if perception is None:
        return Snapshot((), source="none", fallback_used=True)
    target = _call_target(perception, "capture")
    return _call_compatible(target, (((), {}),))


def _normalise_snapshot(raw: Any) -> tuple[Snapshot, Any]:
    foreground: Any = _UNSET
    if isinstance(raw, Snapshot):
        return raw, foreground
    if isinstance(raw, Mapping):
        for key in ("foreground_app", "foreground", "app"):
            if key in raw:
                foreground = raw[key]
                break
        source = str(raw.get("source") or "injected")
        elements = raw.get("elements", raw.get("candidates", ()))
        return Snapshot(tuple(elements or ()), source=source), foreground
    if raw is None:
        return Snapshot((), source="none", fallback_used=True), foreground
    if isinstance(raw, UIElement):
        return Snapshot((raw,), source="injected"), foreground
    try:
        values = tuple(raw)
    except TypeError:
        values = (raw,)
    return Snapshot(values, source="injected"), foreground


def _context_elements(snapshot: Snapshot | None) -> list[dict[str, Any]]:
    if snapshot is None:
        return []
    return [
        {
            "element_id": element.id,
            "role": element.role,
            "label": element.text,
            "enabled": element.enabled,
            "visible": True,
            "password": element.password or element.sensitive,
            "source": element.source,
        }
        for element in snapshot.elements
    ]


def _build_reference_state(
    context: VoiceContext,
    utterance: str,
    *,
    foreground: Any = _UNSET,
    snapshot: Snapshot | None = None,
) -> dict[str, Any]:
    base = context.snapshot()
    current = base.foreground_app
    previous = base.previous_app
    if foreground is not _UNSET:
        candidate = _safe_app(foreground)
        if candidate != current:
            previous = current
        current = candidate
    elements = base.elements
    if snapshot is not None:
        elements = tuple(sanitize_elements(_context_elements(snapshot)))
    transient = ContextSnapshot(
        foreground_app=current,
        previous_app=previous,
        actions=base.actions,
        pending_confirmation=base.pending_confirmation,
        elements=elements,
        last_target=base.last_target,
        captured_at=base.captured_at,
    )
    return build_jev_state(utterance, snapshot=transient)


def _coerce_decision(value: Any) -> RouteDecision:
    if isinstance(value, RouteDecision):
        return value
    if isinstance(value, Mapping):
        raw = value.get("raw")
        raw_mapping = dict(raw) if isinstance(raw, Mapping) else {
            key: item
            for key, item in value.items()
            if key
            not in {
                "accepted",
                "kind",
                "target",
                "confidence",
                "destructive",
                "complete",
                "reason",
            }
        }
        return RouteDecision(
            accepted=bool(value.get("accepted", False)),
            kind=str(value.get("kind") or "none"),
            target=str(value.get("target") or "none"),
            confidence=float(value.get("confidence") or 0.0),
            destructive=bool(value.get("destructive", False)),
            complete=bool(value.get("complete", True)),
            reason=str(value.get("reason") or ""),
            raw=raw_mapping,
        )
    if value is None:
        raise TypeError("router returned no decision")
    raw = getattr(value, "raw", {})
    raw_mapping = dict(raw) if isinstance(raw, Mapping) else {}
    needs_screen = getattr(value, "needs_screen", None)
    if needs_screen is not None:
        raw_mapping.setdefault("needs_screen", bool(needs_screen))
    return RouteDecision(
        accepted=bool(getattr(value, "accepted", False)),
        kind=str(getattr(value, "kind", "none") or "none"),
        target=str(getattr(value, "target", "none") or "none"),
        confidence=float(getattr(value, "confidence", 0.0) or 0.0),
        destructive=bool(getattr(value, "destructive", False)),
        complete=bool(getattr(value, "complete", True)),
        reason=str(getattr(value, "reason", "") or ""),
        raw=raw_mapping,
    )


def _decision_needs_screen(raw: Any, decision: RouteDecision) -> bool:
    if decision.kind.strip().casefold() == "goal":
        return _goal_decision_safe(decision)
    if isinstance(raw, Mapping) and _truthy(raw.get("needs_screen")):
        return _decision_safe(decision)
    if _truthy(getattr(raw, "needs_screen", False)):
        return _decision_safe(decision)
    return _truthy(decision.raw.get("needs_screen")) and _decision_safe(decision)


def _decision_safe(decision: RouteDecision) -> bool:
    return bool(decision.complete and not decision.destructive)


def _goal_decision_safe(decision: RouteDecision) -> bool:
    if not _decision_safe(decision):
        return False
    answers = decision.raw.get("answers")
    if not isinstance(answers, Mapping):
        # Explicitly injected decisions can mark GOAL at the top level.  The
        # JevFastRouter path always includes ``answers`` and is validated below.
        return True
    kind = answers.get("kind")
    addressed = answers.get("addressed")
    complete = answers.get("complete")
    destructive = answers.get("destructive")
    if not isinstance(kind, Mapping):
        return False
    if str(kind.get("choice") or "").strip().casefold() != "goal":
        return False
    kind_confidence = _safe_probability(kind.get("confidence"))
    addressed_probability = _safe_probability(_mapping_value(addressed, "noul"))
    complete_probability = _safe_probability(_mapping_value(complete, "noul"))
    destructive_probability = _safe_probability(_mapping_value(destructive, "noul"))
    if None in {
        kind_confidence,
        addressed_probability,
        complete_probability,
        destructive_probability,
    }:
        return False
    return (
        kind_confidence >= 0.5
        and addressed_probability >= 0.5
        and complete_probability >= 0.6
        and destructive_probability <= 0.5
    )


def _mapping_value(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else None


def _safe_probability(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if result != result or result in {float("inf"), float("-inf")} or not 0.0 <= result <= 1.0:
        return None
    return result


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on", "goal", "screen"}
    return bool(value)


def _coerce_action_result(value: Any, decision: RouteDecision) -> ActionResult:
    if isinstance(value, ActionResult):
        return value
    if isinstance(value, Mapping):
        metadata = value.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {
            key: item for key, item in value.items() if key not in {"ok", "action", "detail", "message"}
        }
        return ActionResult(
            bool(value.get("ok", True)),
            str(value.get("action") or _decision_action_name(decision)),
            str(value.get("detail") or value.get("message") or ""),
            metadata,
        )
    if isinstance(value, bool):
        return ActionResult(value, _decision_action_name(decision))
    if value is None:
        return ActionResult(True, _decision_action_name(decision))
    return ActionResult(
        bool(getattr(value, "ok", True)),
        str(getattr(value, "action", "") or _decision_action_name(decision)),
        str(getattr(value, "detail", "") or getattr(value, "message", "")),
        dict(getattr(value, "metadata", {}) or {}),
    )


def _decision_action_name(decision: RouteDecision) -> str:
    kind = decision.kind.strip().casefold() or "none"
    target = decision.target.strip()
    if kind in _CONTENT_BEARING_KINDS:
        return "type_text" if kind in {"type", "input_text"} else kind
    return f"{kind}:{target}" if target else kind


def _result_code(action: ActionResult) -> str:
    return str(action.metadata.get("code") or "").strip().casefold()


def _fast_record(command: str, decision: RouteDecision, action: ActionResult) -> _ContextRecord:
    kind = decision.kind.strip().casefold()
    content_bearing = kind in _CONTENT_BEARING_KINDS
    said = _safe_context_text(command, redact_all=content_bearing)
    target = "" if content_bearing else _safe_context_text(decision.target)
    detail = _safe_context_text(action.detail, redact_all=content_bearing)
    return _ContextRecord(
        said=said,
        action=_safe_context_text(action.action) or _safe_context_text(kind),
        target=target,
        ok=action.ok,
        outcome="ok" if action.ok else "failed",
        detail=detail,
    )


def _goal_records(goal: str, result: Any) -> list[_ContextRecord]:
    records: list[_ContextRecord] = []
    safe_goal = _safe_context_text(goal)
    last_target = ""
    for step in _goal_steps(result):
        target = _step_value(step, "element_id", "id", "target")
        action = _step_value(step, "action") or "activate"
        status = _step_value(step, "status") or "planned"
        detail = _step_value(step, "detail")
        if target:
            last_target = _safe_context_text(target)
        records.append(
            _ContextRecord(
                said=safe_goal,
                action=_safe_context_text(f"goal_step:{action}"),
                target=last_target,
                ok=status in {"changed", "completed", "ok"},
                outcome=_safe_context_text(status) or "planned",
                detail=_safe_context_text(detail),
            )
        )
    status = _goal_status(result)
    records.append(
        _ContextRecord(
            said=safe_goal,
            action="goal",
            target=last_target,
            ok=status == "completed",
            outcome=_safe_context_text(status) or "failed",
            detail=_safe_context_text(_goal_detail(result)),
        )
    )
    return records


def _goal_status(result: Any) -> str:
    if isinstance(result, Mapping):
        return str(result.get("status") or "goal_failed").strip().casefold()
    return str(getattr(result, "status", "goal_failed") or "goal_failed").strip().casefold()


def _goal_detail(result: Any) -> str:
    if isinstance(result, Mapping):
        return str(result.get("detail") or "")
    return str(getattr(result, "detail", "") or "")


def _goal_steps(result: Any) -> tuple[Any, ...]:
    if isinstance(result, Mapping):
        value = result.get("steps", ())
    else:
        value = getattr(result, "steps", ())
    try:
        return tuple(value or ())
    except TypeError:
        return ()


def _goal_snapshot(result: Any) -> Snapshot | None:
    if isinstance(result, Mapping):
        raw = result.get("snapshot")
    else:
        raw = getattr(result, "snapshot", None)
    if raw is None:
        return None
    try:
        return _normalise_snapshot(raw)[0]
    except Exception:
        return None


def _goal_has_committed_step(result: Any) -> bool:
    for step in _goal_steps(result):
        dry_run = step.get("dry_run", True) if isinstance(step, Mapping) else getattr(step, "dry_run", True)
        status = _step_value(step, "status").strip().casefold()
        if dry_run is False or status in {"changed", "stalled", "action_failed", "completed", "ok"}:
            return True
    return False


def _step_value(step: Any, *names: str) -> str:
    if isinstance(step, Mapping):
        for name in names:
            value = step.get(name)
            if value is not None:
                return str(value)
        return ""
    for name in names:
        value = getattr(step, name, None)
        if value is not None:
            return str(value)
    return ""


def _safe_app(value: Any) -> AppContext | None:
    if value is None:
        return None
    if isinstance(value, AppContext):
        name, title = value.name, value.window_title
    elif isinstance(value, Mapping):
        name = value.get("name") or value.get("app") or value.get("process") or ""
        title = value.get("window_title") or value.get("title") or ""
    else:
        name = getattr(value, "name", "") or getattr(value, "app", "") or str(value)
        title = getattr(value, "window_title", "") or getattr(value, "title", "")
    safe_name = _safe_context_text(name)
    safe_title = _safe_context_text(title)
    if not safe_name and not safe_title:
        return None
    return AppContext(safe_name, safe_title)


def _safe_context_text(value: Any, *, redact_all: bool = False) -> str:
    text = _bounded_text(value, 400)
    if not text:
        return ""
    if redact_all or _SENSITIVE_RE.search(text):
        return "[redacted]"
    return text


def _bounded_text(value: Any, maximum: int) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _raise_if_cancelled(cancellation: Any) -> None:
    if cancellation is None:
        return
    raiser = getattr(cancellation, "raise_if_cancelled", None)
    if callable(raiser):
        raiser()
        return
    value = getattr(cancellation, "cancelled", False)
    if callable(value):
        value = value()
    if value:
        raise VoiceCancelled("voice operation cancelled")


__all__ = [
    "FAST",
    "GOAL",
    "NONE",
    "OrchestratorResult",
    "VoiceOrchestrator",
]
