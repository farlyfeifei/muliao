"""Authoritative planning schema and compatibility projection for Ghost swarms.

This module is the only place that decides recipes, risk gates, clarification,
confirmation, deterministic fallback, and fixed stage topology.  It accepts the
two historical planner dialects but always emits ``ghost.planner/1`` plus the
legacy flat aliases still consumed by ``server.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Sequence


PLANNER_SCHEMA = "ghost.planner/1"
ROLE_POOL: tuple[str, ...] = (
    "compiler",
    "investigator",
    "extractor",
    "builder",
    "verifier",
    "integrator",
)
RECIPE_STAGES: dict[str, tuple[tuple[str, ...], ...]] = {
    "single": (("integrator",),),
    "research": (
        ("compiler",),
        ("investigator", "extractor"),
        ("verifier",),
        ("integrator",),
    ),
    "diagnose": (
        ("compiler",),
        ("investigator", "builder"),
        ("verifier",),
        ("integrator",),
    ),
    "build": (
        ("compiler",),
        ("investigator",),
        ("builder",),
        ("verifier",),
        ("integrator",),
    ),
    "sensitive": (
        ("compiler",),
        ("investigator",),
        ("builder",),
        ("verifier",),
        ("integrator",),
    ),
}
RECIPES: dict[str, dict[str, Any]] = {
    recipe_id: {"id": recipe_id, "stages": [list(stage) for stage in stages]}
    for recipe_id, stages in RECIPE_STAGES.items()
}

SWARM_PLAN_QUESTIONS: dict[str, dict[str, Any]] = {
    "swarm_worthy": {
        "type": "noul",
        "instructions": "Would multiple specialized agents materially improve this task over one direct response?",
    },
    "recipe_id": {
        "type": "choice",
        "instructions": "Choose exactly one fixed Ghost recipe for the task.",
        "criteria": {
            "single": "Short, low-complexity conversation, explanation, or creation",
            "research": "Multi-source research, extraction, comparison, or evidence gathering",
            "diagnose": "Failure, error, notification, log, or application diagnosis",
            "build": "Create code, a report, a plan, or another structured artifact",
            "sensitive": "Deletion, external transmission, system change, or irreversible action",
        },
    },
    "needs_clarification": {
        "type": "noul",
        "instructions": "Must the user clarify a material fact or decision before any bee starts?",
    },
    "risk_score": {
        "type": "score",
        "instructions": "Rate execution risk from 1 (harmless read-only) to 9 (irreversible or severely harmful).",
        "criteria": [
            "1 harmless read-only", "2 minor", "3 limited", "4 moderate", "5 consequential",
            "6 explicit confirmation required", "7 serious", "8 severe", "9 irreversible",
        ],
    },
    "evidence_heavy": {
        "type": "noul",
        "instructions": "Does success depend on independent investigation and source evidence?",
    },
    "parallelizable": {
        "type": "noul",
        "instructions": "Can at least two useful work streams proceed independently and later be combined?",
    },
}

_RECIPE_ALIASES = {
    "chat": "single",
    "query": "single",
    "question": "single",
    "fact": "single",
    "code": "build",
    "coding": "build",
    "software": "build",
    "create": "build",
    "writing": "build",
    "mixed": "build",
    "multi": "build",
    "action": "sensitive",
    "operation": "sensitive",
    "debug": "diagnose",
    "troubleshoot": "diagnose",
}
_RISK_LABELS = {
    "none": 1.0,
    "safe": 1.0,
    "low": 1.5,
    "medium": 4.5,
    "moderate": 4.5,
    "med": 4.5,
    "high": 7.0,
    "severe": 9.0,
    "critical": 9.0,
    "very_high": 9.0,
    "very high": 9.0,
    "低": 1.5,
    "中": 4.5,
    "高": 7.0,
    "极高": 9.0,
}


@dataclass(frozen=True, slots=True)
class PlannerDecision:
    recipe_id: str
    swarm_worthy: bool
    risk_score: float
    needs_clarification: bool = False
    evidence_heavy: bool = False
    parallelizable: bool = False
    clarification_reasons: tuple[str, ...] = ()
    confirmation_required: bool = False
    confirmation_reasons: tuple[str, ...] = ()
    planner_source: str = "provided"
    planner_confidence: float | None = None
    degraded: bool = False
    degraded_reason: str | None = None
    max_parallel: int = 3

    def __post_init__(self) -> None:
        if self.recipe_id not in RECIPES:
            raise ValueError(f"unknown recipe: {self.recipe_id}")
        if not 0.0 <= float(self.risk_score) <= 10.0:
            raise ValueError("risk_score must be between 0 and 10")
        if self.max_parallel < 1:
            raise ValueError("max_parallel must be positive")


@dataclass(frozen=True, slots=True)
class UserGate:
    kind: str
    reasons: tuple[str, ...] = ()

    @property
    def required(self) -> bool:
        return self.kind != "none"


def answer_value(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    for key in ("choice", "noul", "value", "answer", "score", "label", "probability", "result"):
        if key in value:
            return value[key]
    raise ValueError("planner answer has no recognized value field")


def answer_confidence(value: Any) -> float | None:
    if not isinstance(value, Mapping) or value.get("confidence") is None:
        return None
    try:
        return float(value["confidence"])
    except (TypeError, ValueError):
        return None


def coerce_bool(value: Any, field: str = "value") -> bool:
    value = answer_value(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) > 0.5
    text = str(value).strip().lower().replace("-", "_")
    if text in {"yes", "true", "1", "y", "on", "是", "需要", "适合", "值得", "swarm", "high"}:
        return True
    if text in {"no", "false", "0", "n", "off", "否", "不需要", "不适合", "single", "low"}:
        return False
    raise ValueError(f"planner answer {field!r} is not boolean-like: {value!r}")


def coerce_bool_default(value: Any, default: bool, field: str) -> bool:
    try:
        return coerce_bool(value, field)
    except ValueError:
        return bool(default)


def coerce_risk_score(value: Any, default: float = 4.5) -> float:
    value = answer_value(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.0, min(10.0, float(value)))
    text = str(value).strip().lower().replace("-", "_")
    if text in _RISK_LABELS:
        return _RISK_LABELS[text]
    try:
        return max(0.0, min(10.0, float(text)))
    except (TypeError, ValueError):
        return max(0.0, min(10.0, float(default)))


def risk_gate(score: float) -> str:
    if score >= 6.0:
        return "confirm"
    if score >= 3.5:
        return "review"
    return "auto"


def risk_label(score: float) -> str:
    if score >= 9.0:
        return "critical"
    if score >= 6.0:
        return "high"
    if score >= 3.5:
        return "medium"
    return "low"


def coerce_recipe(value: Any, *, default: str | None = None) -> str:
    raw = answer_value(value)
    text = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    text = _RECIPE_ALIASES.get(text, text)
    if text in RECIPES:
        return text
    if default is not None:
        return default
    raise ValueError(f"unknown recipe: {raw!r}")


def recipe_stages(recipe_id: str) -> list[dict[str, Any]]:
    if recipe_id not in RECIPE_STAGES:
        raise ValueError(f"unknown recipe: {recipe_id}")
    return [
        {"id": f"stage_{index + 1}", "index": index, "bees": list(bees)}
        for index, bees in enumerate(RECIPE_STAGES[recipe_id])
    ]


def _dedupe(values: Sequence[Any]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _finalize_decision(
    *,
    recipe_id: str,
    swarm_worthy: bool,
    risk_score: float,
    needs_clarification: bool,
    evidence_heavy: bool,
    parallelizable: bool,
    clarification_reasons: Sequence[str] = (),
    confirmation_required: bool = False,
    confirmation_reasons: Sequence[str] = (),
    planner_source: str,
    planner_confidence: float | None = None,
    degraded: bool = False,
    degraded_reason: str | None = None,
    max_parallel: int = 3,
) -> PlannerDecision:
    risk_score = coerce_risk_score(risk_score)
    recipe_id = coerce_recipe(recipe_id)
    if risk_score >= 6.0:
        recipe_id = "sensitive"
        swarm_worthy = True
    elif recipe_id == "sensitive":
        risk_score = max(6.0, risk_score)
        swarm_worthy = True
    elif not swarm_worthy:
        recipe_id = "single"
    if recipe_id == "single":
        swarm_worthy = False

    clarify_reasons = list(clarification_reasons)
    if needs_clarification and "needs_clarification" not in clarify_reasons:
        clarify_reasons.append("needs_clarification")

    confirm_reasons = list(confirmation_reasons)
    confirmation_required = bool(confirmation_required or risk_score >= 6.0 or recipe_id == "sensitive")
    if risk_score >= 6.0 and "high_risk" not in confirm_reasons:
        confirm_reasons.append("high_risk")
    if recipe_id == "sensitive" and "sensitive_recipe" not in confirm_reasons:
        confirm_reasons.append("sensitive_recipe")

    return PlannerDecision(
        recipe_id=recipe_id,
        swarm_worthy=bool(swarm_worthy),
        risk_score=risk_score,
        needs_clarification=bool(needs_clarification),
        evidence_heavy=bool(evidence_heavy),
        parallelizable=bool(parallelizable),
        clarification_reasons=_dedupe(clarify_reasons),
        confirmation_required=confirmation_required,
        confirmation_reasons=_dedupe(confirm_reasons),
        planner_source=str(planner_source or "provided"),
        planner_confidence=planner_confidence,
        degraded=bool(degraded),
        degraded_reason=str(degraded_reason) if degraded_reason else None,
        max_parallel=max(1, int(max_parallel)),
    )


def parse_jev_response(response: Mapping[str, Any]) -> PlannerDecision:
    if not isinstance(response, Mapping):
        raise ValueError("Jev returned a non-object response")
    if response.get("ok") is False:
        raise RuntimeError(str(response.get("err") or response.get("error") or "Jev planning failed"))
    answers = response.get("answers") if isinstance(response.get("answers"), Mapping) else response
    if not isinstance(answers, Mapping):
        raise ValueError("Jev answers must be an object")

    def pick(*names: str) -> Any:
        for name in names:
            if name in answers:
                return answers[name]
        raise ValueError(f"Jev answers missing: {'/'.join(names)}")

    raw_recipe = pick("recipe_id", "task_type", "recipe")
    confidence = answer_confidence(raw_recipe)
    recipe_id = coerce_recipe(raw_recipe)
    worthy = coerce_bool(pick("swarm_worthy"), "swarm_worthy")
    needs_clarification = coerce_bool(
        pick("needs_clarification", "needs_clarify"), "needs_clarification"
    )
    clarification_reasons: list[str] = []
    if confidence is not None and confidence < 0.60:
        needs_clarification = True
        clarification_reasons.append("low_recipe_confidence")

    risk_score = coerce_risk_score(pick("risk_score", "risk_level"))
    evidence_heavy = coerce_bool(pick("evidence_heavy"), "evidence_heavy")
    parallelizable = coerce_bool(pick("parallelizable"), "parallelizable")
    return _finalize_decision(
        recipe_id=recipe_id,
        swarm_worthy=worthy,
        risk_score=risk_score,
        needs_clarification=needs_clarification,
        evidence_heavy=evidence_heavy,
        parallelizable=parallelizable,
        clarification_reasons=clarification_reasons,
        planner_source="jev",
        planner_confidence=confidence,
    )


def plan_for_recipe(
    recipe_id: str,
    *,
    risk_score: float | None = None,
    needs_clarification: bool = False,
    source: str = "explicit",
    degraded: bool = False,
    degraded_reason: str | None = None,
) -> PlannerDecision:
    recipe_id = coerce_recipe(recipe_id, default="")
    if recipe_id not in RECIPES:
        raise ValueError(f"unknown recipe: {recipe_id}")
    if risk_score is None:
        risk_score = 7.0 if recipe_id == "sensitive" else 1.0
    return _finalize_decision(
        recipe_id=recipe_id,
        swarm_worthy=recipe_id != "single",
        risk_score=risk_score,
        needs_clarification=needs_clarification,
        evidence_heavy=recipe_id in {"research", "diagnose"},
        parallelizable=recipe_id in {"research", "diagnose"},
        planner_source=source,
        degraded=degraded,
        degraded_reason=degraded_reason,
    )


def _contains_term(text: str, term: str) -> bool:
    if any(ord(char) > 127 for char in term):
        return term in text
    pattern = r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _contains_any(text: str, terms: Sequence[str]) -> bool:
    return any(_contains_term(text, term) for term in terms)


def deterministic_fallback(
    goal: str,
    permissions_snapshot: Any = None,
    *,
    degraded_reason: str | None = None,
) -> PlannerDecision:
    text = " ".join(str(goal or "").strip().lower().split())
    sensitive_terms = (
        "删除", "清空", "抹除", "格式化", "卸载", "删库", "外发", "发送给", "发邮件",
        "发布", "上传", "生产环境", "部署生产", "系统设置", "注册表", "付款", "转账", "购买",
        "密码", "凭据", "密钥", "管理员权限",
        "delete", "remove all", "wipe", "format disk", "uninstall", "drop database", "rm -rf",
        "send to", "send email", "publish", "upload", "deploy production", "production deploy",
        "registry", "system setting", "purchase", "pay", "transfer money", "credential", "password",
        "secret key", "sudo", "administrator",
    )
    diagnose_terms = (
        "故障", "报错", "错误", "失败", "崩溃", "异常", "日志", "通知", "诊断", "排查",
        "bug", "error", "failed", "failure", "crash", "exception", "log", "diagnose", "troubleshoot",
    )
    research_terms = (
        "研究", "调查", "分析", "多个来源", "多来源", "资料", "证据", "核验", "比较", "对比",
        "research", "investigate", "analyze", "multiple sources", "evidence", "verify", "compare", "sources",
    )
    build_terms = (
        "实现", "编写", "生成", "创建", "构建", "开发", "代码", "报告", "方案", "修复",
        "implement", "write", "generate", "create", "build", "develop", "code", "report", "fix",
    )
    decomposition_terms = (
        "多步骤", "端到端", "架构", "审计", "分工", "并行", "分别", "多个", "多路", "同时",
        "multi-step", "end-to-end", "architecture", "audit", "parallel", "independent", "multiple", "several",
    )
    vague_exact = {
        "", "do it", "fix it", "handle it", "take care of it", "帮我弄一下", "处理一下", "修一下",
        "帮我看看", "搞定它", "照办",
    }
    compound = len(text) >= 160 and any(token in text for token in (" and ", "、", "以及", ";", "；"))
    decomposable = compound or len(text) >= 280 or _contains_any(text, decomposition_terms)

    if _contains_any(text, sensitive_terms):
        recipe_id, risk = "sensitive", 7.0
    elif _contains_any(text, diagnose_terms):
        recipe_id, risk = "diagnose", 2.5
    elif _contains_any(text, research_terms):
        recipe_id, risk = "research", 1.5
    elif _contains_any(text, build_terms) or decomposable:
        recipe_id, risk = "build", 2.0
    else:
        recipe_id, risk = "single", 0.5
    _ = permissions_snapshot
    return _finalize_decision(
        recipe_id=recipe_id,
        swarm_worthy=recipe_id != "single" or decomposable,
        risk_score=risk,
        needs_clarification=text in vague_exact or len(text) < 4,
        evidence_heavy=recipe_id in {"research", "diagnose"},
        parallelizable=recipe_id in {"research", "diagnose"} or decomposable,
        planner_source="deterministic_fallback",
        degraded=True,
        degraded_reason=degraded_reason,
    )


def normalize_plan(plan: Mapping[str, Any]) -> PlannerDecision:
    if not isinstance(plan, Mapping):
        raise TypeError("plan must be a mapping")
    nested_risk = plan.get("risk") if isinstance(plan.get("risk"), Mapping) else {}
    nested_clarification = plan.get("clarification") if isinstance(plan.get("clarification"), Mapping) else {}
    nested_confirmation = plan.get("confirmation") if isinstance(plan.get("confirmation"), Mapping) else {}

    raw_recipe = plan.get("recipe_id", plan.get("recipe", plan.get("task_type", "single")))
    recipe_id = coerce_recipe(raw_recipe)
    default_worthy = recipe_id != "single"
    try:
        worthy = coerce_bool(plan.get("swarm_worthy", default_worthy), "swarm_worthy")
    except ValueError:
        worthy = default_worthy
    risk_score = coerce_risk_score(
        plan.get("risk_score", nested_risk.get("score", plan.get("risk_level", 7.0 if recipe_id == "sensitive" else 1.0)))
    )
    clarify = coerce_bool_default(
        nested_clarification.get(
            "required", plan.get("needs_clarification", plan.get("needs_clarify", False))
        ),
        False,
        "needs_clarification",
    )
    clarify_reasons = nested_clarification.get("reasons", ())
    if not isinstance(clarify_reasons, Sequence) or isinstance(clarify_reasons, (str, bytes, bytearray)):
        clarify_reasons = ()

    nested_confirmation_required = coerce_bool_default(
        nested_confirmation.get("required", False),
        False,
        "confirmation.required",
    )
    legacy_confirmation_required = coerce_bool_default(
        plan.get("requires_confirmation", False),
        False,
        "requires_confirmation",
    )
    explicit_confirmation = bool(
        nested_confirmation_required
        or (legacy_confirmation_required and not clarify)
    )
    confirm_reasons = nested_confirmation.get("reasons", plan.get("confirmation_reasons", ()))
    if not isinstance(confirm_reasons, Sequence) or isinstance(confirm_reasons, (str, bytes, bytearray)):
        confirm_reasons = ()

    return _finalize_decision(
        recipe_id=recipe_id,
        swarm_worthy=worthy,
        risk_score=risk_score,
        needs_clarification=clarify,
        evidence_heavy=coerce_bool_default(
            plan.get("evidence_heavy", recipe_id in {"research", "diagnose"}),
            recipe_id in {"research", "diagnose"},
            "evidence_heavy",
        ),
        parallelizable=coerce_bool_default(
            plan.get("parallelizable", recipe_id in {"research", "diagnose"}),
            recipe_id in {"research", "diagnose"},
            "parallelizable",
        ),
        clarification_reasons=clarify_reasons,
        confirmation_required=explicit_confirmation,
        confirmation_reasons=confirm_reasons,
        planner_source=str(plan.get("planner_source") or plan.get("planner") or plan.get("source") or "provided"),
        planner_confidence=plan.get("planner_confidence"),
        degraded=coerce_bool_default(
            plan.get("degraded", False), False, "degraded"
        ),
        degraded_reason=plan.get("degraded_reason") or plan.get("fallback_reason"),
        max_parallel=int(plan.get("max_parallel", 3) or 3),
    )


def evaluate_user_gate(decision: PlannerDecision, *, confirmed: bool = False) -> UserGate:
    """判断一个规划是否需要用户先表态。

    注意：``confirmed`` **不能**解除澄清闸门（见
    test_confirmation_never_bypasses_clarification_aliases）。澄清意味着目标里有
    实质信息缺失，而「确认」只是「照你说的办」——它补不上缺失的事实。用户必须
    真的把缺的信息说出来（重新提交更具体的目标，或经澄清应答通道），闸门才放行。
    """
    if decision.needs_clarification:
        return UserGate("clarification", decision.clarification_reasons)
    if decision.confirmation_required and not confirmed:
        return UserGate("confirmation", decision.confirmation_reasons)
    return UserGate("none", ())


def materialize_plan(
    decision: PlannerDecision,
    *,
    confirmed: bool = False,
    include_legacy: bool = True,
) -> dict[str, Any]:
    gate = evaluate_user_gate(decision, confirmed=confirmed)
    stages = recipe_stages(decision.recipe_id)
    bees = [bee for stage in stages for bee in stage["bees"]]
    risk = {
        "score": decision.risk_score,
        "level": risk_label(decision.risk_score),
        "gate": risk_gate(decision.risk_score),
    }
    confirmation_satisfied = bool(confirmed and decision.confirmation_required and not decision.needs_clarification)
    value: dict[str, Any] = {
        "planner_schema": PLANNER_SCHEMA,
        "swarm_worthy": decision.swarm_worthy,
        "recipe_id": decision.recipe_id,
        "risk": risk,
        "risk_score": decision.risk_score,
        "clarification": {
            "required": decision.needs_clarification,
            "reasons": list(decision.clarification_reasons),
        },
        "confirmation": {
            "required": decision.confirmation_required,
            "reasons": list(decision.confirmation_reasons),
            "satisfied": confirmation_satisfied,
        },
        "user_gate": {"kind": gate.kind, "reasons": list(gate.reasons)},
        "evidence_heavy": decision.evidence_heavy,
        "parallelizable": decision.parallelizable,
        "planner_source": decision.planner_source,
        "planner_confidence": decision.planner_confidence,
        "degraded": decision.degraded,
        "degraded_reason": decision.degraded_reason,
        "stages": stages,
        "bees": bees,
        "max_parallel": decision.max_parallel,
        "execution_allowed": decision.swarm_worthy and not gate.required,
        "should_execute": decision.swarm_worthy and not gate.required,
        "status": "requires_confirmation" if gate.required else "planned",
    }
    if include_legacy:
        value.update({
            "recipe": decision.recipe_id,
            "task_type": decision.recipe_id,
            "risk_level": decision.risk_score,
            "risk_gate": risk["gate"],
            "needs_clarification": decision.needs_clarification,
            "needs_clarify": decision.needs_clarification,
            "requires_confirmation": gate.required,
            "confirmation_reasons": list(_dedupe((*decision.clarification_reasons, *decision.confirmation_reasons))),
            "planner": decision.planner_source,
            "source": "fallback" if decision.degraded and decision.planner_source == "fallback" else decision.planner_source,
        })
    return value
