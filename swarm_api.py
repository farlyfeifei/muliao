"""Compatibility boundary for the optional Ghost swarm implementation.

This module intentionally depends only on the Python standard library.  It keeps
``server.py`` insulated from the concrete APIs eventually provided by
``swarm.py``, capsule storage, and orchestration code.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import dataclasses
import datetime as _datetime
import enum
import importlib
import inspect
import json
import math
import pathlib
import threading
import time
import types
import uuid
from collections.abc import AsyncIterable, Iterable, Mapping
from typing import Any, Callable


SWARM_PLAN_QUESTIONS: dict[str, dict[str, Any]] = {
    "swarm_worthy": {
        "type": "choice",
        "instructions": "Would multiple specialized agents materially improve this task?",
        "criteria": {
            "yes": "The task benefits from decomposition, independent checks, or specialists.",
            "no": "A single agent can complete it directly without meaningful loss.",
        },
    },
    "task_type": {
        "type": "choice",
        "instructions": "Classify the task by its primary execution pattern.",
        "criteria": {
            "chat": "Conversation or emotional support.",
            "query": "A direct factual question or explanation.",
            "research": "Evidence gathering, comparison, or source-heavy analysis.",
            "code": "Software implementation, debugging, or review.",
            "create": "Writing, design, or content creation.",
            "action": "An operation that changes an external system or user data.",
            "mixed": "Several materially different task types are combined.",
        },
    },
    "needs_clarify": {
        "type": "choice",
        "instructions": "Is a material user decision or missing fact required before execution?",
        "criteria": {
            "yes": "Execution would require guessing something that changes the outcome or risk.",
            "no": "Reasonable defaults are sufficient and the requested outcome is clear.",
        },
    },
    "risk_level": {
        "type": "choice",
        "instructions": "Rate execution risk, including irreversibility and external side effects.",
        "criteria": {
            "low": "Read-only or readily reversible.",
            "medium": "Limited side effects with a straightforward recovery path.",
            "high": "Sensitive, consequential, destructive, costly, or externally visible.",
            "critical": "Potentially irreversible or severely harmful.",
        },
    },
    "evidence_heavy": {
        "type": "choice",
        "instructions": "Does success depend on collecting and reconciling substantial evidence?",
        "criteria": {
            "yes": "Multiple sources, citations, measurements, or verification are central.",
            "no": "The task can be completed without substantial evidence collection.",
        },
    },
    "parallelizable": {
        "type": "choice",
        "instructions": "Can independent work streams run in parallel and later be combined?",
        "criteria": {
            "yes": "At least two useful sub-tasks can proceed independently.",
            "no": "The task is inherently sequential or too small to parallelize.",
        },
    },
}

_REQUIRED_PLAN_ANSWERS = tuple(SWARM_PLAN_QUESTIONS)
_TERMINAL_STATUSES = {"cancelled", "completed", "failed", "skipped"}
_HIGH_RISK_LEVELS = {"high", "critical", "severe", "very_high", "very high"}


class SwarmBackendUnavailable(RuntimeError):
    """Raised internally when no usable optional swarm backend can be loaded."""


class SwarmBackendError(RuntimeError):
    """Raised internally when an available backend has an incompatible API."""


class _CombinedCancelEvent:
    """Small event facade understood by both sync and async orchestrators."""

    def __init__(self, internal: threading.Event, external: Any = None) -> None:
        self._internal = internal
        self._external = external

    def is_set(self) -> bool:
        if self._internal.is_set():
            return True
        checker = getattr(self._external, "is_set", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return False

    def set(self) -> None:
        self._internal.set()
        setter = getattr(self._external, "set", None)
        if callable(setter):
            try:
                setter()
            except Exception:
                pass

    async def wait(self) -> bool:
        while not self.is_set():
            await asyncio.sleep(0.02)
        return True


class SwarmService:
    """Provider-neutral adapter around an optional swarm orchestrator.

    ``orchestrator`` is primarily an injection seam for tests and incremental
    migration.  When omitted, the backend is imported lazily only when a run is
    actually requested.
    """

    def __init__(self, orchestrator: Any = None, module_name: str | None = None) -> None:
        self._orchestrator = orchestrator
        self._module_name = module_name
        self._loaded_backend: Any = orchestrator
        self._runs: dict[str, dict[str, Any]] = {}
        self._cancel_signals: dict[str, threading.Event] = {}
        self._lock = threading.RLock()

    def plan(
        self,
        goal: Any,
        session_id: Any,
        permissions_snapshot: Any,
        jev_ask_callable: Callable[..., Any] | None,
    ) -> dict[str, Any]:
        """Return a non-executing swarm preview after exactly one Jev batch.

        Jev callables may be synchronous or asynchronous.  Async callables are
        resolved here so this public method keeps the requested synchronous
        ``dict`` contract.  Any Jev exception, negative response, or malformed
        answer set switches to the deterministic local fallback.
        """

        goal_text = str(goal or "").strip()
        session_text = str(session_id or "").strip()
        permissions = _json_safe(permissions_snapshot)
        fallback = _fallback_decision(goal_text, permissions)
        source = "fallback"
        jev_error: str | None = None
        jev_meta: dict[str, Any] = {"ok": False}

        try:
            if not callable(jev_ask_callable):
                raise TypeError("jev_ask_callable is not callable")
            state = json.dumps(
                {
                    "goal": goal_text,
                    "session_id": session_text,
                    "permissions_snapshot": permissions,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            response = jev_ask_callable(state, copy.deepcopy(SWARM_PLAN_QUESTIONS))
            response = _resolve_awaitable_sync(response)
            decision, jev_meta = _decision_from_jev(response)
            source = "jev"
        except Exception as exc:  # A planner outage must never block deterministic routing.
            decision = fallback
            jev_error = f"{type(exc).__name__}: {exc}"
            jev_meta = {"ok": False, "error": jev_error}

        requires_confirmation, confirmation_reasons = _confirmation_gate(decision)
        run_id = f"run_{uuid.uuid4().hex}"
        executable = bool(decision["swarm_worthy"]) and not requires_confirmation
        status = "requires_confirmation" if requires_confirmation else "planned"
        result: dict[str, Any] = {
            "ok": True,
            "run_id": run_id,
            "session_id": session_text,
            "goal": goal_text,
            "permissions_snapshot": permissions,
            **decision,
            "source": source,
            "planner": source,
            "requires_confirmation": requires_confirmation,
            "confirmation_reasons": confirmation_reasons,
            "execution_allowed": executable,
            "should_execute": executable,
            "status": status,
            "jev": jev_meta,
        }
        if jev_error:
            result["fallback_reason"] = jev_error

        now = time.time()
        with self._lock:
            self._runs[run_id] = {
                "found": True,
                "run_id": run_id,
                "session_id": session_text,
                "goal": goal_text,
                "status": status,
                "source": source,
                "requires_confirmation": requires_confirmation,
                "created_at": now,
                "updated_at": now,
                "events": 0,
                "error": None,
            }
            self._cancel_signals[run_id] = threading.Event()
        return result

    async def stream_run(
        self,
        plan: Mapping[str, Any],
        goal: Any,
        session_id: Any,
        llm_config: Any,
        jev_ask_callable: Callable[..., Any] | None,
        tool_executor: Callable[..., Any] | None,
        cancel_event: Any,
    ):
        """Yield JSON-serializable normalized events from the orchestrator."""

        if not isinstance(plan, Mapping):
            raise TypeError("plan must be a mapping")
        plan_data = dict(plan)
        run_id = str(plan_data.get("run_id") or f"run_{uuid.uuid4().hex}")
        session_text = str(session_id or plan_data.get("session_id") or "")
        goal_text = str(goal or plan_data.get("goal") or "")
        internal_cancel = self._ensure_run(run_id, session_text, goal_text, plan_data)
        combined_cancel = _CombinedCancelEvent(internal_cancel, cancel_event)
        seq = 0

        if combined_cancel.is_set():
            self._update_run(run_id, status="cancelled")
            yield self._service_event(
                run_id,
                seq + 1,
                "swarm.cancelled",
                {"reason": "cancelled before execution"},
            )
            return

        requires_confirmation, reasons = _confirmation_gate(plan_data)
        if bool(plan_data.get("requires_confirmation")) or requires_confirmation:
            self._update_run(run_id, status="requires_confirmation", requires_confirmation=True)
            yield self._service_event(
                run_id,
                seq + 1,
                "swarm.waiting_user",
                {"requires_confirmation": True, "reasons": reasons or plan_data.get("confirmation_reasons", [])},
            )
            return

        if plan_data.get("swarm_worthy") is False:
            self._update_run(run_id, status="skipped")
            yield self._service_event(
                run_id,
                seq + 1,
                "swarm.skipped",
                {"reason": "planner selected single-agent execution"},
            )
            return

        self._update_run(run_id, status="running", started_at=time.time())
        try:
            backend = await self._get_backend()
            handler = _find_run_handler(backend)
            result = _invoke_compatible(
                handler,
                {
                    "plan": plan_data,
                    "goal": goal_text,
                    "session_id": session_text,
                    "llm_config": llm_config,
                    "jev_ask_callable": jev_ask_callable,
                    "tool_executor": tool_executor,
                    "cancel_event": combined_cancel,
                    "run_id": run_id,
                    "recipe": plan_data.get("recipe", plan_data.get("recipe_id")),
                    "refs": plan_data.get("refs", []),
                },
            )
            async for raw_event in _iterate_events(result):
                if combined_cancel.is_set():
                    break
                seq += 1
                event = _normalize_event(raw_event, run_id, seq)
                self._record_event(run_id, event)
                yield event

            if combined_cancel.is_set():
                self._update_run(run_id, status="cancelled", finished_at=time.time())
                seq += 1
                event = self._service_event(
                    run_id,
                    seq,
                    "swarm.cancelled",
                    {"reason": "cancel requested"},
                )
                self._record_event(run_id, event)
                yield event
            else:
                current = self.status(run_id).get("status")
                if current not in {"failed", "cancelled", "paused", "requires_confirmation"}:
                    self._update_run(run_id, status="completed", finished_at=time.time())
        except asyncio.CancelledError:
            self._update_run(run_id, status="cancelled", finished_at=time.time())
            raise
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self._update_run(run_id, status="failed", error=message, finished_at=time.time())
            seq += 1
            event = self._service_event(
                run_id,
                seq,
                "swarm.error",
                {
                    "code": "backend_unavailable" if isinstance(exc, SwarmBackendUnavailable) else "backend_error",
                    "error": message,
                },
            )
            self._record_event(run_id, event)
            yield event

    def cancel(self, run_id: Any) -> bool:
        """Request cancellation; return ``False`` for unknown/terminal/repeated runs."""

        key = str(run_id or "")
        with self._lock:
            state = self._runs.get(key)
            signal = self._cancel_signals.get(key)
            if state is None or signal is None:
                return False
            if state.get("status") in _TERMINAL_STATUSES or signal.is_set():
                return False
            signal.set()
            state["status"] = "cancelled" if state.get("status") in {"planned", "requires_confirmation"} else "cancelling"
            state["updated_at"] = time.time()

        backend = self._loaded_backend
        cancel_method = getattr(backend, "cancel", None) if backend is not None else None
        if callable(cancel_method):
            try:
                value = _call_cancel(cancel_method, key)
                if inspect.isawaitable(value):
                    _schedule_awaitable(value)
            except Exception:
                # The shared signal remains authoritative even if a backend hook fails.
                pass
        return True

    def status(self, run_id: Any) -> dict[str, Any]:
        """Return a JSON-safe snapshot without exposing mutable internal state."""

        key = str(run_id or "")
        with self._lock:
            state = self._runs.get(key)
            if state is None:
                return {"found": False, "run_id": key, "status": "unknown"}
            return dict(_json_safe(state))

    def _ensure_run(
        self,
        run_id: str,
        session_id: str,
        goal: str,
        plan: Mapping[str, Any],
    ) -> threading.Event:
        now = time.time()
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                requires_confirmation, _ = _confirmation_gate(plan)
                state = {
                    "found": True,
                    "run_id": run_id,
                    "session_id": session_id,
                    "goal": goal,
                    "status": "requires_confirmation" if requires_confirmation else "planned",
                    "source": plan.get("source", "external"),
                    "requires_confirmation": requires_confirmation,
                    "created_at": now,
                    "updated_at": now,
                    "events": 0,
                    "error": None,
                }
                self._runs[run_id] = state
            signal = self._cancel_signals.setdefault(run_id, threading.Event())
            return signal

    def _update_run(self, run_id: str, **changes: Any) -> None:
        with self._lock:
            state = self._runs.setdefault(run_id, {"found": True, "run_id": run_id, "created_at": time.time()})
            state.update(changes)
            state["updated_at"] = time.time()

    def _record_event(self, run_id: str, event: Mapping[str, Any]) -> None:
        event_type = str(event.get("type", ""))
        changes: dict[str, Any] = {}
        if event_type == "swarm.error":
            changes["status"] = "failed"
            changes["error"] = _json_safe(event.get("payload"))
        elif event_type == "swarm.cancelled":
            changes["status"] = "cancelled"
        elif event_type == "swarm.done":
            changes["status"] = "completed"
        elif event_type == "swarm.paused":
            changes["status"] = "paused"
        elif event_type == "swarm.waiting_user":
            changes["status"] = "requires_confirmation"
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return
            state["events"] = int(state.get("events", 0)) + 1
            state.update(changes)
            state["updated_at"] = time.time()

    def _service_event(
        self,
        run_id: str,
        seq: int,
        event_type: str,
        payload: Any,
    ) -> dict[str, Any]:
        return _normalize_event({"type": event_type, "payload": payload}, run_id, seq)

    async def _get_backend(self) -> Any:
        if self._loaded_backend is not None:
            return self._loaded_backend

        module = _lazy_import_swarm(self._module_name)
        direct_handler = _find_run_handler(module, required=False)
        if direct_handler is not None:
            self._loaded_backend = module
            return module

        singleton = getattr(module, "orchestrator", None)
        if singleton is not None:
            self._loaded_backend = singleton
            return singleton

        for factory_name in ("get_orchestrator", "create_orchestrator"):
            factory = getattr(module, factory_name, None)
            if callable(factory):
                value = factory()
                if inspect.isawaitable(value):
                    value = await value
                self._loaded_backend = value
                return value

        for class_name in ("SwarmOrchestrator", "Orchestrator"):
            cls = getattr(module, class_name, None)
            if inspect.isclass(cls):
                try:
                    value = cls()
                except TypeError as exc:
                    raise SwarmBackendError(
                        f"{class_name} could not be constructed without arguments; "
                        "inject a configured instance into SwarmService(orchestrator=...)."
                    ) from exc
                self._loaded_backend = value
                return value

        raise SwarmBackendUnavailable(
            "The optional swarm backend was imported but exposes no supported runner. "
            "Expected stream_run(), run_swarm(), run(), an orchestrator singleton, "
            "or a zero-argument Orchestrator class."
        )


def _decision_from_jev(response: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(response, Mapping):
        raise ValueError("Jev returned a non-object response")
    if response.get("ok") is False:
        raise RuntimeError(str(response.get("err") or response.get("error") or "Jev planning failed"))

    if "answers" in response:
        answers = response.get("answers")
    elif all(key in response for key in _REQUIRED_PLAN_ANSWERS):
        answers = response
    else:
        raise ValueError("Jev response has no complete answers object")
    if not isinstance(answers, Mapping):
        raise ValueError("Jev answers must be an object")
    missing = [key for key in _REQUIRED_PLAN_ANSWERS if key not in answers]
    if missing:
        raise ValueError(f"Jev answers missing: {', '.join(missing)}")

    decision = {
        "swarm_worthy": _coerce_bool(_answer_value(answers["swarm_worthy"]), "swarm_worthy"),
        "task_type": _coerce_task_type(_answer_value(answers["task_type"])),
        "needs_clarify": _coerce_bool(_answer_value(answers["needs_clarify"]), "needs_clarify"),
        "risk_level": _coerce_risk(_answer_value(answers["risk_level"])),
        "evidence_heavy": _coerce_bool(_answer_value(answers["evidence_heavy"]), "evidence_heavy"),
        "parallelizable": _coerce_bool(_answer_value(answers["parallelizable"]), "parallelizable"),
    }
    meta = {
        "ok": True,
        "model": _json_safe(response.get("model")),
        "usage": _json_safe(response.get("usage")),
        "ms": _json_safe(response.get("ms")),
    }
    return decision, meta


def _answer_value(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    for key in ("choice", "noul", "value", "answer", "score", "label"):
        if key in value:
            return value[key]
    raise ValueError("Jev answer has no recognized value field")


def _coerce_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    text = str(value).strip().lower().replace("-", "_")
    truthy = {"yes", "true", "1", "y", "是", "需要", "适合", "值得", "swarm", "high"}
    falsy = {"no", "false", "0", "n", "否", "不需要", "不适合", "single", "low"}
    if text in truthy:
        return True
    if text in falsy:
        return False
    raise ValueError(f"Jev answer {field!r} is not boolean-like: {value!r}")


def _coerce_task_type(value: Any) -> str:
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "coding": "code",
        "software": "code",
        "fact": "query",
        "question": "query",
        "writing": "create",
        "operation": "action",
        "multi": "mixed",
    }
    return aliases.get(text, text or "task")


def _coerce_risk(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        score = float(value)
        if score >= 9:
            return "critical"
        if score >= 7:
            return "high"
        if score >= 4:
            return "medium"
        return "low"
    text = str(value).strip().lower().replace("-", "_")
    try:
        return _coerce_risk(float(text))
    except ValueError:
        pass
    aliases = {
        "none": "low",
        "safe": "low",
        "moderate": "medium",
        "med": "medium",
        "severe": "critical",
        "very_high": "critical",
        "极高": "critical",
        "高": "high",
        "中": "medium",
        "低": "low",
    }
    return aliases.get(text, text or "medium")


def _confirmation_gate(decision: Mapping[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    risk = _coerce_risk(decision.get("risk_level", "low"))
    if risk in _HIGH_RISK_LEVELS:
        reasons.append("high_risk")
    try:
        clarify = _coerce_bool(decision.get("needs_clarify", False), "needs_clarify")
    except ValueError:
        clarify = bool(decision.get("needs_clarify"))
    if clarify:
        reasons.append("needs_clarification")
    return bool(reasons), reasons


def _fallback_decision(goal: str, permissions_snapshot: Any) -> dict[str, Any]:
    text = " ".join(goal.lower().split())
    evidence_terms = (
        "research", "source", "citation", "evidence", "compare", "benchmark", "verify",
        "调研", "来源", "引用", "证据", "对比", "比较", "核实", "验证",
    )
    parallel_terms = (
        "parallel", "independent", "multiple", "several", "across", "in parallel",
        "并行", "分别", "多个", "多路", "同时", "逐个", "各自",
    )
    swarm_terms = (
        "multi-step", "end-to-end", "architecture", "audit", "investigate", "plan and implement",
        "多步骤", "端到端", "架构", "审计", "排查", "规划并实现", "蜂群", "分工",
    )
    high_risk_terms = (
        "delete", "erase", "wipe", "format disk", "uninstall", "drop database", "rm -rf",
        "purchase", "pay", "transfer money", "publish", "send email", "deploy production",
        "credential", "password", "secret key", "sudo", "administrator",
        "删除", "清空", "抹除", "格式化", "卸载", "删库", "付款", "转账", "购买",
        "发布", "群发", "发邮件", "生产环境", "密码", "凭据", "密钥", "管理员权限",
    )
    medium_risk_terms = (
        "install", "modify", "edit", "write file", "execute", "run command", "deploy",
        "安装", "修改", "编辑", "写入", "执行", "运行命令", "部署",
    )
    vague_exact = {
        "", "do it", "fix it", "handle it", "take care of it", "帮我弄一下", "处理一下",
        "修一下", "帮我看看", "搞定它", "照办",
    }

    evidence_heavy = any(term in text for term in evidence_terms)
    parallelizable = any(term in text for term in parallel_terms)
    decomposition = any(term in text for term in swarm_terms)
    if len(text) >= 160 and any(token in text for token in (" and ", "、", "以及", ";", "；")):
        parallelizable = True
    swarm_worthy = bool(decomposition or (evidence_heavy and parallelizable) or len(text) >= 280)
    needs_clarify = text in vague_exact or len(text) < 4

    if any(term in text for term in high_risk_terms):
        risk_level = "high"
    elif any(term in text for term in medium_risk_terms):
        risk_level = "medium"
    else:
        risk_level = "low"

    if any(term in text for term in ("code", "bug", "test", "python", "javascript", "api", "代码", "修复", "测试", "接口")):
        task_type = "code"
    elif evidence_heavy:
        task_type = "research"
    elif any(term in text for term in ("write", "draft", "design", "create", "写", "撰写", "设计", "创作")):
        task_type = "create"
    elif risk_level != "low" or any(term in text for term in ("send", "change", "发送", "更改")):
        task_type = "action"
    elif "?" in text or "？" in text or text.startswith(("what", "why", "how", "什么", "为什么", "怎么")):
        task_type = "query"
    else:
        task_type = "task"

    # A snapshot is intentionally accepted but does not invent permissions.  It
    # remains part of the deterministic input for future policy refinements.
    _ = permissions_snapshot
    return {
        "swarm_worthy": swarm_worthy,
        "task_type": task_type,
        "needs_clarify": needs_clarify,
        "risk_level": risk_level,
        "evidence_heavy": evidence_heavy,
        "parallelizable": parallelizable,
    }


def _resolve_awaitable_sync(value: Any) -> Any:
    if not inspect.isawaitable(value):
        return value

    async def resolve() -> Any:
        return await value

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(resolve())

    result: list[Any] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(resolve()))
        except BaseException as exc:  # Propagate the original planner failure.
            error.append(exc)

    thread = threading.Thread(target=runner, name="swarm-jev-plan", daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0] if result else None


def _lazy_import_swarm(module_name: str | None) -> types.ModuleType:
    candidates: list[tuple[str, str | None]] = []
    if module_name:
        candidates.append((module_name, None))
    else:
        package = __package__ or None
        if package:
            candidates.append((".swarm", package))
        candidates.append(("swarm", None))

    absent: list[str] = []
    for name, package in candidates:
        display = f"{package}{name}" if package and name.startswith(".") else name
        try:
            return importlib.import_module(name, package)
        except ModuleNotFoundError as exc:
            resolved = importlib.util.resolve_name(name, package) if package else name
            if exc.name not in {resolved, resolved.split(".")[0]}:
                raise SwarmBackendUnavailable(
                    f"Optional swarm backend '{resolved}' exists but dependency '{exc.name}' is missing."
                ) from exc
            absent.append(display)
        except Exception as exc:
            raise SwarmBackendUnavailable(
                f"Optional swarm backend '{display}' failed to import: {type(exc).__name__}: {exc}"
            ) from exc

    attempted = ", ".join(absent) or "swarm"
    raise SwarmBackendUnavailable(
        f"Optional swarm backend is not installed (tried {attempted}). "
        "Create the future swarm.py module or inject a configured backend with "
        "SwarmService(orchestrator=...)."
    )


def _find_run_handler(backend: Any, required: bool = True) -> Callable[..., Any] | None:
    for name in ("stream_run", "run_swarm", "run"):
        candidate = getattr(backend, name, None)
        if callable(candidate):
            return candidate
    if callable(backend) and not isinstance(backend, types.ModuleType):
        return backend
    if required:
        raise SwarmBackendError(
            "Swarm backend has no callable stream_run(), run_swarm(), run(), or __call__()."
        )
    return None


def _invoke_compatible(handler: Callable[..., Any], canonical: Mapping[str, Any]) -> Any:
    aliases = {
        "jev_ask": canonical["jev_ask_callable"],
        "executor": canonical["tool_executor"],
        "tools": canonical["tool_executor"],
        "cancel": canonical["cancel_event"],
        "config": canonical["llm_config"],
    }
    available = {**canonical, **aliases}
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        return handler(**dict(canonical))

    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    used: set[str] = set()
    has_var_keyword = False
    unsupported: list[str] = []
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            continue
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            has_var_keyword = True
            continue
        if parameter.name in available:
            value = available[parameter.name]
            used.add(parameter.name)
            if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
                args.append(value)
            else:
                kwargs[parameter.name] = value
        elif parameter.default is inspect.Parameter.empty:
            unsupported.append(parameter.name)

    if unsupported:
        raise SwarmBackendError(
            "Swarm runner requires unsupported argument(s): " + ", ".join(unsupported)
        )
    if has_var_keyword:
        for name, value in canonical.items():
            if name not in used:
                kwargs[name] = value
    return handler(*args, **kwargs)


async def _iterate_events(result: Any):
    if inspect.isawaitable(result):
        result = await result
    if result is None:
        return
    if isinstance(result, AsyncIterable) or hasattr(result, "__aiter__"):
        async for item in result:
            yield item
        return
    if isinstance(result, Mapping) or isinstance(result, (str, bytes, bytearray)):
        yield result
        return
    if isinstance(result, Iterable):
        for item in result:
            yield item
        return
    yield result


def _normalize_event(raw_event: Any, run_id: str, seq: int) -> dict[str, Any]:
    safe = _json_safe(raw_event)
    if isinstance(safe, Mapping):
        event = dict(safe)
    else:
        event = {"type": "swarm.event", "payload": {"value": safe}}

    event_type = event.get("type") or event.get("event_type") or event.get("event") or "swarm.event"
    event["type"] = str(event_type)
    event.setdefault("event_id", f"evt_{uuid.uuid4().hex}")
    event.setdefault("seq", seq)
    event.setdefault("run_id", run_id)
    event.setdefault("ts", time.time())
    event.setdefault("payload", {})
    return dict(_json_safe(event))


def _json_safe(value: Any, seen: set[int] | None = None) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, enum.Enum):
        return _json_safe(value.value, seen)
    if isinstance(value, (_datetime.datetime, _datetime.date, _datetime.time)):
        return value.isoformat()
    if isinstance(value, _datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, (pathlib.Path, uuid.UUID)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return "base64:" + base64.b64encode(raw).decode("ascii")
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}

    if seen is None:
        seen = set()
    object_id = id(value)
    if object_id in seen:
        return "<cycle>"
    seen.add(object_id)
    try:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return _json_safe(dataclasses.asdict(value), seen)
        if isinstance(value, Mapping):
            return {str(key): _json_safe(item, seen) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(item, seen) for item in value]
        if isinstance(value, (set, frozenset)):
            ordered = sorted(value, key=lambda item: repr(item))
            return [_json_safe(item, seen) for item in ordered]

        for method_name in ("model_dump", "to_dict", "as_dict"):
            method = getattr(value, method_name, None)
            if callable(method):
                try:
                    return _json_safe(method(), seen)
                except Exception:
                    pass
        attributes = getattr(value, "__dict__", None)
        if isinstance(attributes, Mapping):
            public = {key: item for key, item in attributes.items() if not str(key).startswith("_")}
            if public:
                return _json_safe(public, seen)
        return str(value)
    finally:
        seen.discard(object_id)


def _call_cancel(cancel_method: Callable[..., Any], run_id: str) -> Any:
    try:
        signature = inspect.signature(cancel_method)
    except (TypeError, ValueError):
        return cancel_method(run_id)
    parameters = list(signature.parameters.values())
    if not parameters:
        return cancel_method()
    if "run_id" in signature.parameters:
        return cancel_method(run_id=run_id)
    return cancel_method(run_id)


def _schedule_awaitable(value: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        thread = threading.Thread(
            target=lambda: asyncio.run(value),
            name="swarm-cancel-hook",
            daemon=True,
        )
        thread.start()
    else:
        loop.create_task(value)
