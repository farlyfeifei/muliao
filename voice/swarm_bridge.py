"""语音 → Ghost 蜂群 桥接。

让「言出法随」能把**复杂多步任务**升级给主线的多 Agent 蜂群处理，而不是只能做
FAST 白名单里的单动作。复用主线 `SwarmService.plan/run`（其内部已跑 Jev 规划门控
+ 权限快照），因此安全边界不因「从语音来」而松动。

设计：纯桥接、依赖注入。`plan_callable` / `run_callable` 由 server.py 注入真实的
`_swarm_service.plan` / `run`；测试注入假的可调用对象。本模块不 import server，
不碰 voice 的 runtime/service（避免与桌面执行器改动冲突），只定义协议与编排。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence, Union


@dataclass(frozen=True, slots=True)
class SwarmSubmitResult:
    """一次语音→蜂群升级的结果。"""

    ok: bool
    escalated: bool            # 是否真的升级到了蜂群（swarm_worthy 且已建 run）
    run_id: str = ""
    recipe: str = ""
    swarm_worthy: bool = False
    requires_confirmation: bool = True
    status: str = ""
    detail: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)


# plan_callable 可以同步或异步（主线规划需要 await Jev）：
PlanCallable = Union[
    Callable[[str, str], Mapping[str, Any]],
    Callable[[str, str], Awaitable[Mapping[str, Any]]],
]
RunCallable = Union[
    Callable[[Mapping[str, Any], str, str], Mapping[str, Any]],
    Callable[[Mapping[str, Any], str, str], Awaitable[Mapping[str, Any]]],
]


# 触发「考虑升级蜂群」的启发式：语音里出现这些多步/协作信号词时，才去问 Jev 规划。
# 单动作（开记事本、调音量）不该升级——它们走 FAST 更快更省。最终是否升级由
# plan 返回的 swarm_worthy 决定，这里只是「要不要花一次 Jev 规划调用」的预筛。
_ESCALATE_HINTS: tuple[str, ...] = (
    "分别", "各自", "然后汇总", "再复核", "对比", "多个", "批量", "一系列",
    "调研", "竞品", "排期", "拆解", "分步", "逐步", "先", "接着", "最后",
    "汇总", "整理成", "清单", "报告", "方案", "计划", "多步", "协作",
)


def looks_swarm_worthy(text: str) -> bool:
    """粗筛：这段语音指令是否**可能**值得交给蜂群（多步/协作信号）。

    只做省钱的预筛，不做最终裁决——真正的 swarm_worthy 由 Jev 规划判定。
    宁可放过（交给 plan 判），也不要把单动作误升级。
    """
    t = str(text or "")
    if len(t) >= 24:            # 足够长的复合指令更可能是多步任务
        return True
    return any(h in t for h in _ESCALATE_HINTS)


class SwarmBridge:
    """把语音指令升级给主线蜂群。线程安全由注入的 callable 自身保证（server 侧有锁）。"""

    def __init__(
        self,
        *,
        plan_callable: PlanCallable | None = None,
        run_callable: RunCallable | None = None,
        enabled: bool = True,
        auto_run: bool = False,
    ) -> None:
        self._plan = plan_callable
        self._run = run_callable
        self.enabled = bool(enabled)
        # auto_run=False（默认，安全优先）：只产出蜂群计划、等用户在前端确认派蜂；
        # True 时才自动起 run（仍受 plan 的 requires_confirmation / 权限门约束）。
        self.auto_run = bool(auto_run)

    @property
    def available(self) -> bool:
        return self.enabled and self._plan is not None

    def submit(
        self,
        goal: str,
        *,
        session_id: str = "voice",
        force: bool = False,
    ) -> SwarmSubmitResult:
        """把语音目标提交给蜂群规划；swarm_worthy 则建 run（auto_run 时才执行）。

        force=True 跳过启发式预筛，直接问 Jev 规划（用户明确要「用蜂群」时）。
        任何异常都收敛成 ok=False 的结果，绝不让语音回合崩掉。
        """
        goal = str(goal or "").strip()
        if not goal:
            return SwarmSubmitResult(False, False, detail="空指令")
        if not self.available:
            return SwarmSubmitResult(False, False, detail="蜂群桥未启用/未接线")
        if not force and not looks_swarm_worthy(goal):
            return SwarmSubmitResult(False, False,
                                     detail="非多步任务，走单动作通道更合适")
        try:
            planned = self._plan(goal, str(session_id or "voice"))
        except Exception as exc:  # noqa: BLE001
            return SwarmSubmitResult(False, False, detail=f"蜂群规划失败：{type(exc).__name__}: {exc}")
        if not isinstance(planned, Mapping) or not planned.get("ok"):
            err = (planned or {}).get("err") if isinstance(planned, Mapping) else "规划无响应"
            return SwarmSubmitResult(False, False, detail=f"蜂群规划未通过：{err}")

        plan = planned.get("plan") if isinstance(planned.get("plan"), Mapping) else planned
        swarm_worthy = bool(plan.get("swarm_worthy"))
        recipe = str(plan.get("recipe") or plan.get("recipe_id") or "")
        requires_confirmation = bool(plan.get("requires_confirmation", True))
        run_id = str(plan.get("run_id") or "")
        if not swarm_worthy:
            return SwarmSubmitResult(True, False, run_id=run_id, recipe=recipe,
                                     swarm_worthy=False, requires_confirmation=requires_confirmation,
                                     status="single", detail="Jev 判定不必蜂群，建议走单 Agent",
                                     raw=dict(plan))
        if not self.auto_run or self._run is None:
            return SwarmSubmitResult(True, True, run_id=run_id, recipe=recipe,
                                     swarm_worthy=True, requires_confirmation=requires_confirmation,
                                     status="planned", detail="蜂群计划已就绪，等待确认派蜂",
                                     raw=dict(plan))
        try:
            ran = self._run(plan, goal, str(session_id or "voice"))
        except Exception as exc:  # noqa: BLE001
            return SwarmSubmitResult(True, True, run_id=run_id, recipe=recipe,
                                     swarm_worthy=True, requires_confirmation=requires_confirmation,
                                     status="planned", detail=f"计划就绪但起 run 失败：{exc}",
                                     raw=dict(plan))
        ok = bool(isinstance(ran, Mapping) and ran.get("ok"))
        rid = str((ran or {}).get("run_id") or run_id) if isinstance(ran, Mapping) else run_id
        return SwarmSubmitResult(ok, True, run_id=rid, recipe=recipe, swarm_worthy=True,
                                 requires_confirmation=requires_confirmation,
                                 status="running" if ok else "plan_only",
                                 detail=(ran or {}).get("err", "") if not ok else "蜂群已启动",
                                 raw=dict(plan))

    async def submit_async(
        self,
        goal: str,
        *,
        session_id: str = "voice",
        force: bool = False,
    ) -> SwarmSubmitResult:
        """submit 的异步版：plan/run callable 可以是协程（主线规划要 await Jev）。

        与 submit 同一套裁决逻辑；同步 callable 直接调，异步的 await。
        """
        goal = str(goal or "").strip()
        if not goal:
            return SwarmSubmitResult(False, False, detail="空指令")
        if not self.available:
            return SwarmSubmitResult(False, False, detail="蜂群桥未启用/未接线")
        if not force and not looks_swarm_worthy(goal):
            return SwarmSubmitResult(False, False,
                                     detail="非多步任务，走单动作通道更合适")

        async def _call(fn: Any, *args: Any) -> Any:
            res = fn(*args)
            if asyncio.iscoroutine(res):
                return await res
            return res

        try:
            planned = await _call(self._plan, goal, str(session_id or "voice"))
        except Exception as exc:  # noqa: BLE001
            return SwarmSubmitResult(False, False, detail=f"蜂群规划失败：{type(exc).__name__}: {exc}")
        if not isinstance(planned, Mapping) or not planned.get("ok"):
            err = (planned or {}).get("err") if isinstance(planned, Mapping) else "规划无响应"
            return SwarmSubmitResult(False, False, detail=f"蜂群规划未通过：{err}")

        plan = planned.get("plan") if isinstance(planned.get("plan"), Mapping) else planned
        swarm_worthy = bool(plan.get("swarm_worthy"))
        recipe = str(plan.get("recipe") or plan.get("recipe_id") or "")
        requires_confirmation = bool(plan.get("requires_confirmation", True))
        run_id = str(plan.get("run_id") or "")
        if not swarm_worthy:
            return SwarmSubmitResult(True, False, run_id=run_id, recipe=recipe,
                                     swarm_worthy=False, requires_confirmation=requires_confirmation,
                                     status="single", detail="Jev 判定不必蜂群，建议走单 Agent",
                                     raw=dict(plan))
        if not self.auto_run or self._run is None:
            return SwarmSubmitResult(True, True, run_id=run_id, recipe=recipe,
                                     swarm_worthy=True, requires_confirmation=requires_confirmation,
                                     status="planned", detail="蜂群计划已就绪，等待确认派蜂",
                                     raw=dict(plan))
        try:
            ran = await _call(self._run, plan, goal, str(session_id or "voice"))
        except Exception as exc:  # noqa: BLE001
            return SwarmSubmitResult(True, True, run_id=run_id, recipe=recipe,
                                     swarm_worthy=True, requires_confirmation=requires_confirmation,
                                     status="planned", detail=f"计划就绪但起 run 失败：{exc}",
                                     raw=dict(plan))
        ok = bool(isinstance(ran, Mapping) and ran.get("ok"))
        rid = str((ran or {}).get("run_id") or run_id) if isinstance(ran, Mapping) else run_id
        return SwarmSubmitResult(ok, True, run_id=rid, recipe=recipe, swarm_worthy=True,
                                 requires_confirmation=requires_confirmation,
                                 status="running" if ok else "plan_only",
                                 detail=(ran or {}).get("err", "") if not ok else "蜂群已启动",
                                 raw=dict(plan))


__all__ = ["SwarmBridge", "SwarmSubmitResult", "looks_swarm_worthy"]
