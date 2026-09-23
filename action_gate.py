r"""动作门控 · 让 Jev 参与「每一次」工具行为

这是幕僚 Muliáo 的执行前风险闸门。`/api/chat` 里每个 tool_call 在真正交给
machine_tools.execute_tool 执行**之前**，先经 `evaluate()` 裁决：

    allow    —— 直接执行（只读采集、低风险动作）
    confirm  —— 先请用户在前端点「允许执行」，确认后才执行（高风险/不可逆）
    deny     —— 拒绝执行（控制类动作遇到 Jev 不可用时的 fail-closed 兜底）

设计原则（与用户要求一一对应）：
  1) **Jev 参与每次行为**：无论只读还是控制类，每个动作都先过 Jev 打分。
  2) **高风险需用户确认**：risk≥CONFIRM_THRESHOLD、或 Jev 判 needs_confirm、
     或动作不可逆、或与用户意图不符 → confirm。
  3) **fail-closed 只针对控制类**：Jev 不可用（超时/断网/额度）时，控制类动作
     一律 deny，绝不因判断引擎掉线就放行危险操作；只读采集降级放行（allow）。
  4) **本地危险词兜底（defense in depth）**：即便 Jev 判低风险，只要动作/参数
     命中本地危险词表且是控制类，至少 confirm，不可被 Jev 放行绕过。

本模块是**纯逻辑**：不碰 IO、不 import server / machine_tools / permissions
（避免循环依赖），只复用 swarm_planner 的纯函数 + 标准库。jev_ask 由调用方注入。
"""
from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from swarm_planner import coerce_bool_default, coerce_risk_score, answer_value

# ---- 阈值：收敛 swarm_planner.risk_gate 里散落的字面量，保持全局一致 ----
CONFIRM_THRESHOLD = 6.0   # ≥ 此分：高风险，需用户确认（对齐 risk_gate 的 "confirm"）
REVIEW_THRESHOLD = 3.5    # ≥ 此分：中风险，复核（对齐 risk_gate 的 "review"）

# noul 判定为「是」的概率门槛
_NOUL_YES = 0.5


# ============ 动作级危险词表（针对「即将执行的动作 + 参数」，不是用户目标）============
# 这些是「执行类动作」一旦出现就至少需要确认的词。带边界，避免 payload 命中 pay。
# 中英文混合；匹配前把文本归一为小写、压缩空白。
DANGER_TERMS: tuple[str, ...] = (
    # 破坏性文件/磁盘
    "rm -rf", "rm -r ", "del /", "rmdir", "format", "mkfs", "diskpart",
    "删除", "清空", "抹除", "格式化", "粉碎",
    # 关机 / 重启 / 注销
    "shutdown", "restart-computer", "reboot", "logoff",
    "关机", "重启", "注销",
    # 强制关闭窗口 / 应用（alt+f4 会直接关闭当前窗口，可能丢失未保存内容）
    "alt+f4", "alt + f4",
    # 进程 / 服务
    "taskkill", "kill -9", "stop-service", "taskkill /f",
    "结束进程", "强制结束", "停止服务",
    # 注册表 / 系统设置
    "reg delete", "reg add", "regedit", "registry",
    "注册表", "系统设置", "组策略", "gpedit",
    # 卸载 / 安装
    "uninstall", "卸载",
    # 外发 / 发布（数据离开本机）
    "send email", "send-mail", "publish", "upload", "deploy",
    "发邮件", "发送", "外发", "上传", "发布", "部署",
    # 支付 / 转账
    "purchase", "transfer money", "pay ", "payment", "wire",
    "付款", "支付", "转账", "汇款", "购买",
    # 凭据 / 密钥
    "password", "credential", "secret key", "api key", "private key",
    "密码", "凭据", "密钥", "口令",
    # 提权
    "sudo", "runas", "administrator", "提权", "管理员权限",
)


def _normalize(text: str) -> str:
    """归一化用于危险词匹配的文本：小写、压缩空白、保留首尾空格语义。"""
    return " ".join(str(text or "").strip().lower().split())


def _contains_term(haystack: str, term: str) -> bool:
    """带边界的子串匹配：避免 'payload' 命中 'pay'、'passwords' 之外的误伤。

    对纯 ASCII 词要求左右不是字母数字；对含中文/空格的词直接子串匹配。
    """
    hay = _normalize(haystack)
    t = term.strip().lower()
    if not t:
        return False
    # 含空格或以空格结尾的词（如 "rm -rf"、"pay "）——按子串，允许尾随空格被压缩
    probe = " ".join(t.split())
    idx = 0
    n = len(hay)
    m = len(probe)
    while True:
        pos = hay.find(probe, idx)
        if pos < 0:
            return False
        before = hay[pos - 1] if pos > 0 else " "
        after = hay[pos + m] if pos + m < n else " "
        # ASCII 字母数字边界检查（中文/标点/空格都算边界）
        left_ok = not (before.isascii() and before.isalnum())
        right_ok = not (after.isascii() and after.isalnum())
        if left_ok and right_ok:
            return True
        idx = pos + 1


def local_danger(tool_name: str, args: Any) -> bool:
    """本地危险词兜底：动作名 + 参数文本命中 DANGER_TERMS 即视为危险。"""
    try:
        args_text = json.dumps(args, ensure_ascii=False, default=str) if args else ""
    except (TypeError, ValueError):
        args_text = str(args or "")
    blob = f"{tool_name} {args_text}"
    return any(_contains_term(blob, term) for term in DANGER_TERMS)


# ============ Jev 题目集：结构对齐 swarm_planner.SWARM_PLAN_QUESTIONS ============
ACTION_GATE_QUESTIONS: dict[str, dict] = {
    "risk_score": {
        "type": "score",
        "instructions": (
            "Rate the execution risk of THIS specific tool call (tool + arguments) on the "
            "user's own computer, from 1 (harmless read-only) to 10 (irreversible or "
            "destructive). Consider reversibility, blast radius, and whether data leaves "
            "the machine."
        ),
        "criteria": [
            "1 read-only lookup, no side effect",
            "2 trivial reversible UI action",
            "3 minor reversible change",
            "4 moderate change, easily undone",
            "5 noticeable change, recoverable",
            "6 explicit confirmation required",
            "7 serious or hard to undo",
            "8 severe, wide blast radius",
            "9 destructive or irreversible",
            "10 catastrophic / data loss / sends data off-machine",
        ],
    },
    "reversible": {
        "type": "noul",
        "instructions": (
            "Can this exact action be cleanly undone by an ordinary user afterwards, "
            "without data loss? True if reversible, false if not."
        ),
    },
    "needs_confirm": {
        "type": "noul",
        "instructions": (
            "Should a human explicitly confirm this action before it runs, given its "
            "side effects? True if it should require confirmation."
        ),
    },
    "matches_intent": {
        "type": "noul",
        "instructions": (
            "Does this tool call faithfully serve the user's stated request, rather than "
            "being an unrequested, excessive, or off-target action? True if it matches intent."
        ),
    },
}


@dataclass(frozen=True, slots=True)
class GateDecision:
    """一次动作门控的裁决结果（不可变）。"""
    action: str                 # "allow" | "confirm" | "deny"
    risk: float                 # 0-10 风险分（Jev 不可用时为本地估计）
    reason: str                 # 中文一句话，给前端/日志
    source: str                 # "jev" | "fallback" | "fail_closed" | "local"
    needs_confirmation: bool    # action == "confirm"
    jev_ok: bool                # Jev 本次是否成功返回
    jev_code: str | None        # Jev 失败时的诊断码（timeout/rate_limit/quota/...）
    local_danger: bool          # 是否命中本地危险词


def _noul_yes(answers: Mapping[str, Any], key: str, default: bool = False) -> bool:
    """从 Jev answers 取一个 noul 判定；缺失/异常时取 default。"""
    raw = answers.get(key)
    if raw is None:
        return default
    try:
        val = answer_value(raw)
    except (ValueError, TypeError):
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return float(val) >= _NOUL_YES
    return coerce_bool_default(raw, default, key)


def _risk_from(answers: Mapping[str, Any]) -> float | None:
    raw = answers.get("risk_score")
    if raw is None:
        return None
    try:
        return coerce_risk_score(raw, default=4.5)
    except (ValueError, TypeError):
        return None


def _args_brief(args: Any, limit: int = 600) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False, default=str, sort_keys=True)
    except (TypeError, ValueError):
        s = str(args)
    return s if len(s) <= limit else s[:limit] + "…"


def _state_for(tool_name: str, args: Any, user_text: str) -> str:
    """把「工具名 + 参数 + 用户当前意图」拼成给 Jev 的判断上下文。"""
    payload = {
        "tool": tool_name,
        "args": args if isinstance(args, (dict, list)) else _args_brief(args),
        "user_request": str(user_text or "")[:1500],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)[:24000]


async def evaluate(
    *,
    tool_name: str,
    args: Any,
    user_text: str,
    control: bool,
    jev_ask: Callable[..., Awaitable[dict]],
    permission_snapshot: Mapping[str, Any] | None = None,
    timeout: float = 20.0,
) -> GateDecision:
    """对单个工具动作做 Jev 风险门控。

    参数：
      tool_name / args —— 即将执行的工具与参数。
      user_text        —— 本轮用户原始诉求（判断 matches_intent 用）。
      control          —— 该工具是否控制类（scope==computer_control）。决定 fail-closed 策略。
      jev_ask          —— 注入的异步 Jev 调用（server.jev_ask），返回 {ok, answers, code, ...}。
      permission_snapshot —— 可选，当前权限快照（仅供 Jev 语境，不影响本地裁决）。
      timeout          —— Jev 调用超时（秒）。

    返回 GateDecision。绝不抛异常（Jev 异常按失败处理，走 fail-closed/fallback）。
    """
    tool_name = str(tool_name or "")
    danger = local_danger(tool_name, args)

    # ---- 调 Jev（任何异常都吞成「不可用」，不让门控本身崩掉回合）----
    jev_ok = False
    jev_code: str | None = None
    answers: dict = {}
    try:
        state = _state_for(tool_name, args, user_text)
        questions = copy.deepcopy(ACTION_GATE_QUESTIONS)
        if permission_snapshot is not None:
            try:
                state = json.dumps(
                    {"action": json.loads(state) if isinstance(state, str) else state,
                     "permissions": dict(permission_snapshot)},
                    ensure_ascii=False, sort_keys=True,
                )[:24000]
            except (TypeError, ValueError):
                pass
        res = await asyncio.wait_for(jev_ask(state, questions, timeout), timeout=timeout + 5)
        if isinstance(res, Mapping):
            jev_ok = bool(res.get("ok"))
            jev_code = res.get("code")
            if jev_ok and isinstance(res.get("answers"), Mapping):
                answers = dict(res["answers"])
    except asyncio.TimeoutError:
        jev_ok, jev_code = False, "timeout"
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        jev_ok, jev_code = False, "upstream"

    # ---- Jev 失败：fail-closed 仅针对控制类 ----
    if not jev_ok:
        if control:
            reason = f"Jev 不可用（{jev_code or '未知'}），控制类动作已拒绝执行。"
            return GateDecision(
                action="deny", risk=CONFIRM_THRESHOLD, reason=reason,
                source="fail_closed", needs_confirmation=False,
                jev_ok=False, jev_code=jev_code, local_danger=danger,
            )
        reason = "Jev 不可用，只读动作降级放行。"
        return GateDecision(
            action="allow", risk=0.0, reason=reason, source="fallback",
            needs_confirmation=False, jev_ok=False, jev_code=jev_code, local_danger=danger,
        )

    # ---- Jev 成功：综合打分 ----
    risk = _risk_from(answers)
    if risk is None:
        risk = 4.5
    needs_confirm = _noul_yes(answers, "needs_confirm", default=False)
    reversible = _noul_yes(answers, "reversible", default=True)
    matches_intent = _noul_yes(answers, "matches_intent", default=True)

    reasons: list[str] = []
    confirm = False
    if risk >= CONFIRM_THRESHOLD:
        confirm = True
        reasons.append(f"风险{risk:.1f}≥{CONFIRM_THRESHOLD:.0f}")
    # 「副作用类」升级条件只对**控制类动作**生效。只读采集没有副作用，
    # 不因「意图不符/不可逆/需确认」就弹确认骚扰用户——它只受风险分约束。
    if control:
        if needs_confirm:
            confirm = True
            reasons.append("Jev判定需确认")
        if not reversible:
            confirm = True
            reasons.append("不可逆")
        if not matches_intent:
            confirm = True
            reasons.append("与用户意图不符")
        if danger:
            # defense in depth：本地危险词命中且是控制类，至少 confirm，不被 Jev 放行绕过
            confirm = True
            reasons.append("命中本地危险动作词")

    if confirm:
        return GateDecision(
            action="confirm", risk=risk, reason="；".join(reasons) or "需确认",
            source="jev", needs_confirmation=True,
            jev_ok=True, jev_code=None, local_danger=danger,
        )
    return GateDecision(
        action="allow", risk=risk, reason=f"风险{risk:.1f}，放行",
        source="jev", needs_confirmation=False,
        jev_ok=True, jev_code=None, local_danger=danger,
    )
