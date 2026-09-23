"""Ghost 蜂群第二轮的供应商无关编排核心。

本模块只负责确定性规划、任务契约和 asyncio 编排，不直接依赖模型、HTTP、
权限或持久化实现。调用方注入两个异步函数：

* ``bee_runner(bee_id, context)`` 执行一只蜂；可返回 dict/string，或返回异步迭代器。
* ``jev_checker(state, questions)`` 做批量小判断；接口兼容现有 ``jev_ask``。

``orchestrate`` / ``run_swarm`` 是异步事件流，产出可直接交给 SSE 层的事件字典。
未来的 swarm_models.py / capsule.py 可以后装；当前实现使用延迟导入与纯标准库 fallback。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import inspect
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any

from swarm_planner import (
    RECIPES,
    RECIPE_STAGES,
    ROLE_POOL,
    SWARM_PLAN_QUESTIONS,
    deterministic_fallback,
    evaluate_user_gate,
    materialize_plan,
    normalize_plan,
    parse_jev_response,
    plan_for_recipe,
    recipe_stages,
    risk_gate,
)

_RECIPE_STAGES = RECIPE_STAGES

_ALLOWED_CORRECTIONS = {"accept", "retry", "need_context", "pause", "escalate"}
_STREAM_EVENT_TYPES = {"bee.reasoning", "bee.delta", "bee.tool_call", "bee.tool_result"}

BeeRunner = Callable[[str, dict[str, Any]], Awaitable[Any] | AsyncIterator[Any]]
JevChecker = Callable[[str, dict[str, Any]], Awaitable[Mapping[str, Any]]]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _stable_id(prefix: str, value: Any, size: int = 20) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()[:size]
    return f"{prefix}_{digest}"


def _risk_gate(risk_level: float) -> str:
    return risk_gate(float(risk_level))


def _recipe_stages(recipe: str) -> list[dict[str, Any]]:
    return recipe_stages(recipe)


def _plan_for_recipe(
    recipe: str,
    *,
    source: str,
    risk_level: float | None = None,
    degraded: bool = False,
    needs_clarification: bool = False,
) -> dict[str, Any]:
    decision = plan_for_recipe(
        recipe,
        source=source,
        risk_score=risk_level,
        degraded=degraded,
        needs_clarification=needs_clarification,
    )
    return materialize_plan(decision)


def deterministic_fallback_plan(goal: str) -> dict[str, Any]:
    """Return the canonical deterministic fallback with legacy compatibility fields."""

    return materialize_plan(deterministic_fallback(goal))


def _permission_tools(permission_snapshot: Any) -> list[str]:
    if not isinstance(permission_snapshot, Mapping):
        return []
    explicit = permission_snapshot.get("allowed_tools")
    if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes, bytearray)):
        return sorted({str(item) for item in explicit})
    scopes = permission_snapshot.get("scopes")
    if isinstance(scopes, Mapping):
        return sorted(str(name) for name, granted in scopes.items() if bool(granted))
    return []


def _normalize_stages(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    recipe = str(plan.get("recipe") or "")
    canonical = _recipe_stages(recipe)
    raw_stages = plan.get("stages")
    if not isinstance(raw_stages, Sequence) or isinstance(raw_stages, (str, bytes, bytearray)):
        return canonical

    stages: list[dict[str, Any]] = []
    for index, raw_stage in enumerate(raw_stages):
        if isinstance(raw_stage, Mapping):
            raw_bees = raw_stage.get("bees", [])
            stage_id = str(raw_stage.get("id") or f"stage_{index + 1}")
        else:
            raw_bees = raw_stage
            stage_id = f"stage_{index + 1}"
        if not isinstance(raw_bees, Sequence) or isinstance(raw_bees, (str, bytes, bytearray)):
            raise ValueError(f"stage {index + 1} bees must be a sequence")
        bees = [str(bee) for bee in raw_bees]
        if not bees:
            raise ValueError(f"stage {index + 1} cannot be empty")
        unknown = sorted(set(bees) - set(ROLE_POOL))
        if unknown:
            raise ValueError(f"unknown bee roles: {', '.join(unknown)}")
        if len(bees) != len(set(bees)):
            raise ValueError(f"stage {index + 1} contains duplicate bees")
        stages.append({"id": stage_id, "index": index, "bees": bees})
    if not stages:
        raise ValueError("plan must contain at least one stage")
    if [stage["bees"] for stage in stages] != [stage["bees"] for stage in canonical]:
        raise ValueError(f"recipe {recipe} must use its fixed stage topology")
    return stages


def build_contract(goal: str, plan: Mapping[str, Any], permission_snapshot: Any) -> dict[str, Any]:
    """Build a deterministic TaskContract-compatible plain dictionary.

    No clock, UUID, import, mutation, or I/O is used, so equal inputs produce equal output.
    Persistence may add a storage timestamp later without changing this contract revision.
    """

    normalized_plan = copy.deepcopy(dict(plan))
    stages = _normalize_stages(normalized_plan)
    normalized_plan["stages"] = stages
    recipe = str(normalized_plan.get("recipe") or "")
    if recipe not in RECIPES:
        raise ValueError(f"unknown recipe: {recipe}")

    risk_level = float(normalized_plan.get("risk_level", 7.0 if recipe == "sensitive" else 1.0))
    risk_level = max(0.0, min(10.0, risk_level))
    if recipe == "sensitive":
        risk_level = max(6.0, risk_level)
    constraints = copy.deepcopy(normalized_plan.get("required_constraints") or [])
    acceptance_tests = copy.deepcopy(normalized_plan.get("acceptance_tests") or [])
    scope = copy.deepcopy(normalized_plan.get("scope") or {"included": [], "excluded": []})
    snapshot = copy.deepcopy(permission_snapshot)
    identity = {
        "goal": str(goal),
        "recipe": recipe,
        "stages": stages,
        "permission_snapshot": snapshot,
        "constraints": constraints,
    }
    unique_bees = {bee for stage in stages for bee in stage["bees"]}
    requested_parallel = int(normalized_plan.get("max_parallel", 3))
    max_parallel = max(1, min(requested_parallel, max(len(stage["bees"]) for stage in stages)))

    return {
        "task_id": _stable_id("tsk", identity),
        "contract_rev": 1,
        "goal": str(goal),
        "recipe": recipe,
        "scope": scope,
        "required_constraints": constraints,
        "acceptance_tests": acceptance_tests,
        "allowed_tools": _permission_tools(snapshot),
        "permission_snapshot": snapshot,
        "risk_gate": _risk_gate(risk_level),
        "budgets": {
            "max_bees": max(1, len(unique_bees)),
            "max_parallel": max_parallel,
            "max_stages": len(stages),
            "max_jev_calls": 5,
            "max_retries_per_bee": 1,
        },
        # 纯函数不能写入墙钟时间；持久化层负责补充存储时间。
        "created_at": None,
    }


def make_event(
    event_type: str,
    payload: Mapping[str, Any] | None,
    *,
    run_id: str,
    task_id: str,
    seq: int,
    ts: float | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Create one append-only event envelope."""

    if not event_type or "." not in event_type:
        raise ValueError("event_type must be a non-empty dotted name")
    if seq < 1:
        raise ValueError("seq must start at 1")
    if ts is None:
        ts = time.time()
    if event_id is None:
        event_id = _stable_id("evt", [run_id, task_id, seq, event_type])
    return {
        "event_id": str(event_id),
        "seq": int(seq),
        "run_id": str(run_id),
        "task_id": str(task_id),
        "ts": float(ts),
        "type": str(event_type),
        "payload": copy.deepcopy(dict(payload or {})),
    }


# 兼容不同装配代码可能采用的 helper 名。
event = make_event
event_helper = make_event


def _answer_value(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    for key in ("value", "answer", "choice", "label", "score", "noul", "probability", "result"):
        if key in value:
            return value[key]
    return value


def _answer_confidence(value: Any) -> float | None:
    if not isinstance(value, Mapping):
        return None
    raw = value.get("confidence")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    value = _answer_value(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"yes", "true", "1", "y", "on"}:
            return True
        if lowered in {"no", "false", "0", "n", "off"}:
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) > 0.5
    return bool(value)


def _float_or(value: Any, default: float) -> float:
    try:
        return float(_answer_value(value))
    except (TypeError, ValueError):
        return default


def _jev_plan(goal: str, response: Mapping[str, Any]) -> dict[str, Any] | None:
    del goal
    try:
        return materialize_plan(parse_jev_response(response))
    except (TypeError, ValueError, RuntimeError):
        return None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    return repr(value)


def _result_summary(result: Any) -> Any:
    safe = _json_safe(result)
    if isinstance(safe, str):
        return safe[:4000]
    if isinstance(safe, Mapping):
        preferred = ("status", "summary", "output", "text", "artifacts", "evidence", "open_questions")
        selected = {key: safe[key] for key in preferred if key in safe}
        return selected or safe
    return safe


def _stream_item(item: Any, bee_id: str) -> tuple[str, dict[str, Any]] | None:
    if isinstance(item, str):
        return "bee.delta", {"bee_id": bee_id, "text": item}
    if not isinstance(item, Mapping):
        return None
    item_type = str(item.get("type") or "")
    if item_type in _STREAM_EVENT_TYPES:
        payload = item.get("payload")
        if isinstance(payload, Mapping):
            normalized = dict(payload)
        else:
            normalized = {key: value for key, value in item.items() if key != "type"}
        normalized.setdefault("bee_id", bee_id)
        return item_type, _json_safe(normalized)
    return None


class BeeExecutionError(RuntimeError):
    def __init__(self, bee_id: str, stage_id: str, cause: BaseException):
        super().__init__(f"bee {bee_id} failed in {stage_id}: {cause}")
        self.bee_id = bee_id
        self.stage_id = stage_id
        self.cause = cause


class SwarmGateError(RuntimeError):
    def __init__(self, action: str, bee_id: str):
        super().__init__(f"Jev gate requested {action} for {bee_id}")
        self.action = action
        self.bee_id = bee_id


def _optional_capsule_builder() -> Callable[..., Any] | None:
    """Resolve a future capsule helper lazily; absence or API drift is harmless."""

    module_names = ["capsule"]
    if __package__:
        module_names.insert(0, f"{__package__}.capsule")
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except (ImportError, ModuleNotFoundError):
            continue
        for name in ("build_capsule", "create_capsule", "make_capsule"):
            candidate = getattr(module, name, None)
            if callable(candidate):
                return candidate
    return None


def _fallback_capsule(
    *,
    run_id: str,
    contract: Mapping[str, Any],
    from_bee: str,
    to_bee: str,
    stage_id: str,
    result: Any,
) -> dict[str, Any]:
    identity = [run_id, contract["task_id"], contract["contract_rev"], stage_id, from_bee, to_bee]
    required = []
    for item in contract.get("required_constraints", []):
        if isinstance(item, Mapping):
            required.append(str(item.get("id") or item.get("text") or ""))
        else:
            required.append(str(item))
    return {
        "protocol": "GCTX/0.1",
        "message_id": _stable_id("msg", identity),
        "capsule_id": _stable_id("cap", identity),
        "task_id": contract["task_id"],
        "contract_rev": contract["contract_rev"],
        "from_bee": from_bee,
        "to_bee": to_bee,
        "required_constraints": required,
        "facts": [],
        "artifacts": [],
        "open_questions": [],
        "side_effects": [],
        "loss_manifest": [],
        "depends_on": [],
        "payload": _result_summary(result),
        "sender_claims": {"required_payload_complete": True},
    }


def _build_handoff_capsule(**kwargs: Any) -> dict[str, Any]:
    builder = _optional_capsule_builder()
    if builder is not None:
        try:
            built = builder(**kwargs)
            if isinstance(built, Mapping):
                return _json_safe(built)
        except (TypeError, ValueError, KeyError):
            # 并行开发中的 capsule API 尚未稳定时，继续使用协议兼容 fallback。
            pass
    return _fallback_capsule(**kwargs)


def _stage_questions(bees: Sequence[str]) -> dict[str, dict[str, Any]]:
    questions: dict[str, dict[str, Any]] = {}
    for bee in bees:
        questions[f"on_contract_{bee}"] = {
            "type": "noul",
            "instructions": f"Did {bee} stay within the immutable task contract?",
        }
        questions[f"evidence_sufficient_{bee}"] = {
            "type": "noul",
            "instructions": f"Is {bee}'s evidence sufficient for the next stage?",
        }
        questions[f"safe_to_continue_{bee}"] = {
            "type": "noul",
            "instructions": f"Is it safe to continue after {bee}?",
        }
        questions[f"conflict_present_{bee}"] = {
            "type": "noul",
            "instructions": f"Did {bee} expose an unresolved conflict?",
        }
        questions[f"correction_action_{bee}"] = {
            "type": "choice",
            "instructions": f"Choose the bounded correction action for {bee}.",
            "criteria": {
                "accept": "Continue without correction",
                "retry": "Retry this bee once with the contract restated",
                "need_context": "Pause because required context is missing",
                "pause": "Pause because it is unsafe to continue",
                "escalate": "Escalate an unresolved conflict to the user",
            },
        }
    return questions


def _stage_actions(response: Mapping[str, Any], bees: Sequence[str]) -> dict[str, dict[str, Any]] | None:
    if response.get("ok") is False:
        return None
    raw_answers = response.get("answers")
    answers: Mapping[str, Any] = raw_answers if isinstance(raw_answers, Mapping) else response
    direct_actions = response.get("actions")
    if not isinstance(direct_actions, Mapping):
        direct_actions = answers.get("actions") if isinstance(answers.get("actions"), Mapping) else {}

    checks: dict[str, dict[str, Any]] = {}
    for bee in bees:
        raw_action = direct_actions.get(bee, answers.get(f"correction_action_{bee}", answers.get(bee, "accept")))
        action = str(_answer_value(raw_action) or "accept").strip().lower()
        if action not in _ALLOWED_CORRECTIONS:
            action = "accept"
        on_contract = _as_bool(answers.get(f"on_contract_{bee}", True))
        evidence_sufficient = _as_bool(answers.get(f"evidence_sufficient_{bee}", True))
        safe = _as_bool(answers.get(f"safe_to_continue_{bee}", True))
        conflict = _as_bool(answers.get(f"conflict_present_{bee}", False))
        if action == "accept":
            if not safe:
                action = "pause"
            elif conflict:
                action = "escalate"
            elif not on_contract or not evidence_sufficient:
                action = "retry"
        checks[bee] = {
            "action": action,
            "on_contract": on_contract,
            "evidence_sufficient": evidence_sufficient,
            "safe_to_continue": safe,
            "conflict_present": conflict,
            "confidence": _answer_confidence(raw_action),
        }
    return checks


class SwarmOrchestrator:
    """Run fixed Ghost recipes as an append-only async event stream."""

    def __init__(
        self,
        bee_runner: BeeRunner,
        jev_checker: JevChecker | None,
        *,
        max_parallel: int = 3,
        max_jev_calls: int = 5,
    ) -> None:
        if not callable(bee_runner):
            raise TypeError("bee_runner must be callable")
        if jev_checker is not None and not callable(jev_checker):
            raise TypeError("jev_checker must be callable or None")
        if int(max_parallel) < 1:
            raise ValueError("max_parallel must be at least 1")
        if int(max_jev_calls) < 0:
            raise ValueError("max_jev_calls cannot be negative")
        self.bee_runner = bee_runner
        self.jev_checker = jev_checker
        self.max_parallel = int(max_parallel)
        self.max_jev_calls = int(max_jev_calls)

    async def _call_jev(
        self,
        state: str,
        questions: dict[str, Any],
        jev_state: dict[str, Any],
    ) -> Mapping[str, Any] | None:
        if jev_state["degraded"] or self.jev_checker is None:
            jev_state["degraded"] = True
            jev_state.setdefault("reason", "checker_unavailable")
            return None
        if jev_state["calls"] >= self.max_jev_calls:
            jev_state["degraded"] = True
            jev_state["reason"] = "jev_budget_exhausted"
            return None
        jev_state["calls"] += 1
        try:
            response = await self.jev_checker(state, questions)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # checker failure must degrade, not fabricate an answer
            jev_state["degraded"] = True
            jev_state["reason"] = f"checker_error:{type(exc).__name__}"
            return None
        if not isinstance(response, Mapping) or response.get("ok") is False:
            jev_state["degraded"] = True
            error = response.get("err") if isinstance(response, Mapping) else "invalid_response"
            jev_state["reason"] = f"checker_unavailable:{error or 'unknown'}"
            return None
        return response

    async def _consume_runner(
        self,
        bee_id: str,
        context: dict[str, Any],
        queue: asyncio.Queue[tuple[str, dict[str, Any]]],
    ) -> Any:
        invocation = self.bee_runner(bee_id, context)
        if inspect.isawaitable(invocation):
            invocation = await invocation

        final_result: Any = None
        if hasattr(invocation, "__aiter__"):
            async for item in invocation:
                streamed = _stream_item(item, bee_id)
                if streamed is not None:
                    await queue.put(streamed)
                if isinstance(item, Mapping) and item.get("type") == "result":
                    final_result = item.get("result", item.get("payload"))
                elif streamed is None:
                    final_result = item
        else:
            final_result = invocation

        if isinstance(final_result, Mapping):
            raw_events = final_result.get("events")
            if isinstance(raw_events, Sequence) and not isinstance(raw_events, (str, bytes, bytearray)):
                for item in raw_events:
                    streamed = _stream_item(item, bee_id)
                    if streamed is not None:
                        await queue.put(streamed)
            text = final_result.get("text") or final_result.get("output") or final_result.get("summary")
            if isinstance(text, str) and text:
                await queue.put(("bee.delta", {"bee_id": bee_id, "text": text}))
        elif isinstance(final_result, str) and final_result:
            await queue.put(("bee.delta", {"bee_id": bee_id, "text": final_result}))

        return _json_safe(final_result)

    async def _run_batch(
        self,
        *,
        bees: Sequence[str],
        stage: Mapping[str, Any],
        attempt_by_bee: Mapping[str, int],
        goal: str,
        plan: Mapping[str, Any],
        contract: Mapping[str, Any],
        inputs: Sequence[Mapping[str, Any]],
        correction_by_bee: Mapping[str, Any],
        cancel_event: asyncio.Event,
        queue: asyncio.Queue[tuple[str, dict[str, Any]]],
        parallel_limit: int,
    ) -> dict[str, Any]:
        semaphore = asyncio.Semaphore(parallel_limit)
        results: dict[str, Any] = {}
        failures: list[BeeExecutionError] = []

        for bee_id in bees:
            await queue.put((
                "bee.queued",
                {
                    "bee_id": bee_id,
                    "role": bee_id,
                    "stage": stage["id"],
                    "stage_index": stage["index"],
                    "attempt": attempt_by_bee[bee_id],
                },
            ))

        async def run_one(bee_id: str) -> None:
            try:
                async with semaphore:
                    if cancel_event.is_set():
                        raise asyncio.CancelledError
                    attempt = attempt_by_bee[bee_id]
                    await queue.put((
                        "bee.start",
                        {
                            "bee_id": bee_id,
                            "role": bee_id,
                            "stage": stage["id"],
                            "stage_index": stage["index"],
                            "attempt": attempt,
                        },
                    ))
                    context = {
                        "goal": goal,
                        "recipe": plan["recipe"],
                        "plan": copy.deepcopy(dict(plan)),
                        "contract": copy.deepcopy(dict(contract)),
                        "bee_id": bee_id,
                        "role": bee_id,
                        "stage": stage["id"],
                        "stage_index": stage["index"],
                        "attempt": attempt,
                        "inputs": copy.deepcopy(list(inputs)),
                        "correction": copy.deepcopy(correction_by_bee.get(bee_id)),
                        "cancel_event": cancel_event,
                    }
                    results[bee_id] = await self._consume_runner(bee_id, context, queue)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures.append(BeeExecutionError(bee_id, str(stage["id"]), exc))

        tasks = [asyncio.create_task(run_one(bee_id)) for bee_id in bees]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        if failures:
            raise failures[0]
        return results

    async def _stream_batch(
        self,
        *,
        holder: dict[str, Any],
        cancel_event: asyncio.Event,
        **batch_kwargs: Any,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        batch_task = asyncio.create_task(
            self._run_batch(cancel_event=cancel_event, queue=queue, **batch_kwargs)
        )
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            while True:
                if cancel_event.is_set():
                    batch_task.cancel()
                    await asyncio.gather(batch_task, return_exceptions=True)
                    holder["cancelled"] = True
                    return
                if batch_task.done() and queue.empty():
                    break

                get_task = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {batch_task, cancel_task, get_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done and cancel_event.is_set():
                    get_task.cancel()
                    await asyncio.gather(get_task, return_exceptions=True)
                    batch_task.cancel()
                    await asyncio.gather(batch_task, return_exceptions=True)
                    holder["cancelled"] = True
                    return
                if get_task in done:
                    yield get_task.result()
                else:
                    get_task.cancel()
                    await asyncio.gather(get_task, return_exceptions=True)

            holder["results"] = batch_task.result()
            while not queue.empty():
                yield queue.get_nowait()
        finally:
            if not batch_task.done():
                batch_task.cancel()
                await asyncio.gather(batch_task, return_exceptions=True)
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

    async def run(
        self,
        goal: str,
        *,
        recipe: str | None = None,
        plan: Mapping[str, Any] | None = None,
        permission_snapshot: Any = None,
        cancel_event: asyncio.Event | None = None,
        confirmed: bool = False,
        run_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Plan and run a swarm, yielding ordered event envelopes.

        A high-risk or ambiguous plan yields ``swarm.waiting_user`` and returns before any
        bee starts unless ``confirmed=True``. Cancellation stops all in-flight bee tasks;
        ``swarm.cancelled`` is terminal and no later events are emitted.
        """

        goal = str(goal or "").strip()
        if not goal:
            raise ValueError("goal cannot be empty")
        cancel_event = cancel_event or asyncio.Event()
        run_id = str(run_id or f"run_{uuid.uuid4().hex}")
        seq = 0
        task_id = _stable_id("tsk", [goal, recipe, permission_snapshot])
        jev_state: dict[str, Any] = {"calls": 0, "degraded": self.jev_checker is None}
        if self.jev_checker is None:
            jev_state["reason"] = "checker_unavailable"

        def emit(event_type: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
            nonlocal seq, task_id
            seq += 1
            return make_event(
                event_type,
                payload,
                run_id=run_id,
                task_id=task_id,
                seq=seq,
            )

        if cancel_event.is_set():
            yield emit("swarm.cancelled", {"status": "cancelled", "reason": "cancel_requested"})
            return

        try:
            if plan is not None:
                original_plan = copy.deepcopy(dict(plan))
                decision = normalize_plan(original_plan)
                if self.jev_checker is None and not decision.degraded:
                    original_plan["degraded"] = True
                    original_plan["degraded_reason"] = "checker_unavailable"
                    decision = normalize_plan(original_plan)
                selected_plan = {**original_plan, **materialize_plan(decision, confirmed=confirmed)}
            elif recipe is not None:
                decision = plan_for_recipe(
                    str(recipe),
                    source="explicit",
                    degraded=self.jev_checker is None,
                    degraded_reason="checker_unavailable" if self.jev_checker is None else None,
                )
                selected_plan = materialize_plan(decision, confirmed=confirmed)
            else:
                plan_response = await self._call_jev(goal, SWARM_PLAN_QUESTIONS, jev_state)
                selected_plan = _jev_plan(goal, plan_response) if plan_response is not None else None
                if selected_plan is None:
                    jev_state["degraded"] = True
                    jev_state.setdefault("reason", "invalid_planner_response")
                    selected_plan = materialize_plan(
                        deterministic_fallback(goal, degraded_reason=jev_state.get("reason")),
                        confirmed=confirmed,
                    )
                else:
                    selected_plan = materialize_plan(
                        normalize_plan(selected_plan),
                        confirmed=confirmed,
                    )

            decision = normalize_plan(selected_plan)
            recipe_id = decision.recipe_id
            selected_plan["stages"] = _normalize_stages(selected_plan)
            selected_plan["degraded"] = bool(decision.degraded or jev_state["degraded"])
            selected_plan["degraded_reason"] = (
                decision.degraded_reason or jev_state.get("reason")
            ) if selected_plan["degraded"] else None
            selected_plan["bees"] = [
                bee for stage in selected_plan["stages"] for bee in stage["bees"]
            ]
            risk_level = decision.risk_score

            contract = build_contract(goal, selected_plan, permission_snapshot)
            task_id = contract["task_id"]
            yield emit("swarm.plan", {
                "plan": copy.deepcopy(selected_plan),
                "recipe": recipe_id,
                "bees": list(selected_plan["bees"]),
                "stages": copy.deepcopy(selected_plan["stages"]),
                "degraded": selected_plan["degraded"],
                "degraded_reason": selected_plan.get("degraded_reason"),
            })
            yield emit("contract.created", {"contract": copy.deepcopy(contract)})

            gate = evaluate_user_gate(decision, confirmed=confirmed)
            if gate.required:
                waiting_reason = (
                    "clarification_required"
                    if gate.kind == "clarification"
                    else "high_risk_confirmation_required"
                )
                yield emit("swarm.waiting_user", {
                    "status": "waiting_user",
                    "reason": waiting_reason,
                    "gate": gate.kind,
                    "reasons": list(gate.reasons),
                    "risk_level": risk_level,
                    "risk_gate": selected_plan["risk_gate"],
                })
                return

            if cancel_event.is_set():
                yield emit("swarm.cancelled", {"status": "cancelled", "reason": "cancel_requested"})
                return

            parallel_limit = max(
                1,
                min(
                    self.max_parallel,
                    int(selected_plan.get("max_parallel", self.max_parallel)),
                    int(contract["budgets"]["max_parallel"]),
                ),
            )
            stage_inputs: list[dict[str, Any]] = []
            all_results: dict[str, Any] = {}
            retries: dict[str, int] = {bee: 0 for bee in ROLE_POOL}
            stages = selected_plan["stages"]

            for stage_index, stage in enumerate(stages):
                if cancel_event.is_set():
                    yield emit("swarm.cancelled", {"status": "cancelled", "reason": "cancel_requested"})
                    return

                bees = list(stage["bees"])
                holder: dict[str, Any] = {}
                attempt_by_bee = {bee: retries[bee] + 1 for bee in bees}
                async for descriptor_type, descriptor_payload in self._stream_batch(
                    holder=holder,
                    cancel_event=cancel_event,
                    bees=bees,
                    stage=stage,
                    attempt_by_bee=attempt_by_bee,
                    goal=goal,
                    plan=selected_plan,
                    contract=contract,
                    inputs=stage_inputs,
                    correction_by_bee={},
                    parallel_limit=parallel_limit,
                ):
                    yield emit(descriptor_type, descriptor_payload)
                if holder.get("cancelled"):
                    yield emit("swarm.cancelled", {"status": "cancelled", "reason": "cancel_requested"})
                    return
                stage_results = dict(holder.get("results") or {})
                all_results.update(stage_results)

                # 同一阶段一次批量检查。降级时明确记录并关闭自动纠偏。
                stage_state = _canonical_json({
                    "goal": goal,
                    "contract_rev": contract["contract_rev"],
                    "stage": stage["id"],
                    "results": {bee: _result_summary(stage_results.get(bee)) for bee in bees},
                })
                check_response = await self._call_jev(stage_state, _stage_questions(bees), jev_state)
                checks = _stage_actions(check_response, bees) if check_response is not None else None
                if checks is None:
                    selected_plan["degraded"] = True
                    selected_plan["degraded_reason"] = jev_state.get("reason")
                    checks = {
                        bee: {
                            "action": "accept",
                            "status": "degraded",
                            "auto_correction": False,
                            "reason": jev_state.get("reason", "checker_unavailable"),
                        }
                        for bee in bees
                    }

                for bee in bees:
                    yield emit("bee.check", {
                        "bee_id": bee,
                        "stage": stage["id"],
                        "attempt": retries[bee] + 1,
                        **_json_safe(checks[bee]),
                    })

                retry_bees = [bee for bee in bees if checks[bee]["action"] == "retry"]
                for bee in bees:
                    action = checks[bee]["action"]
                    if action in {"need_context", "pause", "escalate"}:
                        raise SwarmGateError(action, bee)

                if retry_bees:
                    for bee in retry_bees:
                        if retries[bee] >= 1:
                            raise SwarmGateError("retry_exhausted", bee)
                        retries[bee] += 1
                        yield emit("bee.correct", {
                            "bee_id": bee,
                            "stage": stage["id"],
                            "reason": checks[bee],
                            "retry": retries[bee],
                        })

                    retry_holder: dict[str, Any] = {}
                    async for descriptor_type, descriptor_payload in self._stream_batch(
                        holder=retry_holder,
                        cancel_event=cancel_event,
                        bees=retry_bees,
                        stage=stage,
                        attempt_by_bee={bee: retries[bee] + 1 for bee in retry_bees},
                        goal=goal,
                        plan=selected_plan,
                        contract=contract,
                        inputs=stage_inputs,
                        correction_by_bee={bee: checks[bee] for bee in retry_bees},
                        parallel_limit=parallel_limit,
                    ):
                        yield emit(descriptor_type, descriptor_payload)
                    if retry_holder.get("cancelled"):
                        yield emit("swarm.cancelled", {"status": "cancelled", "reason": "cancel_requested"})
                        return
                    retry_results = dict(retry_holder.get("results") or {})
                    stage_results.update(retry_results)
                    all_results.update(retry_results)

                    retry_state = _canonical_json({
                        "goal": goal,
                        "contract_rev": contract["contract_rev"],
                        "stage": stage["id"],
                        "retry_results": {
                            bee: _result_summary(retry_results.get(bee)) for bee in retry_bees
                        },
                    })
                    retry_response = await self._call_jev(
                        retry_state,
                        _stage_questions(retry_bees),
                        jev_state,
                    )
                    retry_checks = _stage_actions(retry_response, retry_bees) if retry_response is not None else None
                    if retry_checks is None:
                        selected_plan["degraded"] = True
                        selected_plan["degraded_reason"] = jev_state.get("reason")
                        retry_checks = {
                            bee: {
                                "action": "accept",
                                "status": "degraded",
                                "auto_correction": False,
                                "reason": jev_state.get("reason", "checker_unavailable"),
                            }
                            for bee in retry_bees
                        }
                    for bee in retry_bees:
                        action = retry_checks[bee]["action"]
                        yield emit("bee.check", {
                            "bee_id": bee,
                            "stage": stage["id"],
                            "attempt": retries[bee] + 1,
                            "retry_exhausted": action == "retry",
                            **_json_safe(retry_checks[bee]),
                        })
                        if action != "accept":
                            terminal_action = "retry_exhausted" if action == "retry" else action
                            raise SwarmGateError(terminal_action, bee)

                # 交给下一阶段；接收方回执由未来 capsule.py 可替换，目前为 accepted fallback。
                next_stage = stages[stage_index + 1] if stage_index + 1 < len(stages) else None
                next_inputs: list[dict[str, Any]] = []
                if next_stage is not None:
                    for from_bee in bees:
                        for to_bee in next_stage["bees"]:
                            capsule = _build_handoff_capsule(
                                run_id=run_id,
                                contract=contract,
                                from_bee=from_bee,
                                to_bee=to_bee,
                                stage_id=str(stage["id"]),
                                result=stage_results.get(from_bee),
                            )
                            next_inputs.append(capsule)
                            yield emit("handoff.created", {
                                "from": from_bee,
                                "to": to_bee,
                                "capsule_id": capsule.get("capsule_id"),
                                "message_id": capsule.get("message_id"),
                                "capsule": capsule,
                            })
                            yield emit("handoff.ack", {
                                "from": from_bee,
                                "to": to_bee,
                                "capsule_id": capsule.get("capsule_id"),
                                "message_id": capsule.get("message_id"),
                                "ack": "accepted",
                            })
                stage_inputs = next_inputs

            yield emit("swarm.done", {
                "status": "completed",
                "recipe": recipe_id,
                "results": _json_safe(all_results),
                "degraded": bool(selected_plan.get("degraded")),
                "degraded_reason": selected_plan.get("degraded_reason"),
                "jev_calls": jev_state["calls"],
                "retries": {bee: count for bee, count in retries.items() if count},
            })
        except asyncio.CancelledError:
            raise
        except BeeExecutionError as exc:
            yield emit("swarm.error", {
                "status": "error",
                "code": "bee_failed",
                "bee_id": exc.bee_id,
                "stage": exc.stage_id,
                "message": str(exc.cause),
                "error_type": type(exc.cause).__name__,
            })
        except SwarmGateError as exc:
            yield emit("swarm.error", {
                "status": "error",
                "code": "jev_gate",
                "action": exc.action,
                "bee_id": exc.bee_id,
                "message": str(exc),
            })
        except Exception as exc:
            yield emit("swarm.error", {
                "status": "error",
                "code": "orchestrator_error",
                "message": str(exc),
                "error_type": type(exc).__name__,
            })


async def orchestrate(
    goal: str,
    bee_runner: BeeRunner,
    jev_checker: JevChecker | None,
    *,
    recipe: str | None = None,
    plan: Mapping[str, Any] | None = None,
    permission_snapshot: Any = None,
    max_parallel: int = 3,
    max_jev_calls: int = 5,
    cancel_event: asyncio.Event | None = None,
    confirmed: bool = False,
    run_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Convenience async-generator wrapper around :class:`SwarmOrchestrator`."""

    orchestrator = SwarmOrchestrator(
        bee_runner,
        jev_checker,
        max_parallel=max_parallel,
        max_jev_calls=max_jev_calls,
    )
    async for item in orchestrator.run(
        goal,
        recipe=recipe,
        plan=plan,
        permission_snapshot=permission_snapshot,
        cancel_event=cancel_event,
        confirmed=confirmed,
        run_id=run_id,
    ):
        yield item


run_swarm = orchestrate


__all__ = [
    "ROLE_POOL",
    "RECIPES",
    "SWARM_PLAN_QUESTIONS",
    "BeeExecutionError",
    "SwarmGateError",
    "SwarmOrchestrator",
    "build_contract",
    "deterministic_fallback_plan",
    "event",
    "event_helper",
    "make_event",
    "orchestrate",
    "run_swarm",
]
