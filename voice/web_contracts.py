"""WEB-GOAL 通道的冻结数据契约（M3A 地基）。

本模块只含数据类、枚举与纯字段校验：无 I/O、无 Jev、无浏览器、无副作用。
设计依据《16-幕僚浏览器WEB-GOAL升级方案.md》§4 与 jev-ultrafast@1231850a 核验结论：

- 代码持有真实 DOM 节点；模型只从**本次 observation** 的候选里选 ``target_id``；
- 动作必须绑定 ``observation_id + page_revision + target_id``，过期即 ``TARGET_STALE``；
- 模型不得产生 CSS selector、XPath、坐标、JavaScript、CDP method、shell 命令；
- 文本输入坚持 select-not-generate：:class:`ActionProposal` **没有自由文本字段**，
  只有 ``text_candidate_id``，指向代码从用户原话抠出的 :class:`TextCandidate`；
- 网页文本是不可信数据：state 只送 origin+title+截断可见文本，**不送完整 URL**
  （query/fragment 可能含 token）；页面文字只能使 policy 更严，不能放宽。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Any, Mapping, Sequence

# 与 jev-ultrafast 一致：Jev Choice 上限 255，留余量取 250。
MAX_TARGETS = 250
MAX_TARGET_LABEL_CHARS = 60
MAX_PAGE_TEXT_CHARS = 6_000
MAX_STATE_CHARS = 24_000
MAX_RECENT_ACTIONS = 3
DEFAULT_CONFIRM_TTL_SECONDS = 60.0


class Channel:
    """voice.* 事件 payload 的通道标记（doc 16 §7.2）。"""

    FAST = "fast"
    BROWSER = "browser"
    DESKTOP = "desktop"
    ALL = (FAST, BROWSER, DESKTOP)


class WebOperation:
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE_TEXT = "type_text"
    SELECT = "select"
    SCROLL_UP = "scroll_up"
    SCROLL_DOWN = "scroll_down"
    WAIT = "wait"
    DONE = "done"
    BLOCKED = "blocked"
    ALL = (
        NAVIGATE, CLICK, TYPE_TEXT, SELECT,
        SCROLL_UP, SCROLL_DOWN, WAIT, DONE, BLOCKED,
    )
    # 需要 target_id 的操作。
    TARGETED = (CLICK, TYPE_TEXT, SELECT)
    # 需要 text_candidate_id 的操作（输入与导航目的地都是 select-not-generate）。
    TEXT_BEARING = (TYPE_TEXT, NAVIGATE)


class EffectClass:
    READ = "read"
    SCROLL = "scroll"
    NAVIGATE = "navigate"
    INPUT = "input"
    SUBMIT = "submit"
    DOWNLOAD = "download"
    UPLOAD = "upload"
    AUTH = "auth"
    PAYMENT = "payment"
    ACCOUNT = "account"
    UNKNOWN = "unknown"
    ALL = (
        READ, SCROLL, NAVIGATE, INPUT, SUBMIT, DOWNLOAD,
        UPLOAD, AUTH, PAYMENT, ACCOUNT, UNKNOWN,
    )


class PolicyDecision:
    ALLOW = "allow"
    REQUIRE_CONFIRMATION = "require_confirmation"
    NEEDS_INPUT = "needs_input"
    BLOCK = "block"
    ALL = (ALLOW, REQUIRE_CONFIRMATION, NEEDS_INPUT, BLOCK)


class CommitState:
    """动作副作用的三态；UNKNOWN 绝不自动重试（doc 16 §7.3）。"""

    NOT_COMMITTED = "not_committed"
    COMMITTED = "committed"
    UNKNOWN = "unknown"
    ALL = (NOT_COMMITTED, COMMITTED, UNKNOWN)


class CandidateSource:
    """text_candidate 的三种合法来源（doc 16 §4.4）；此外一律 NEEDS_INPUT。"""

    USER_SPAN = "user_span"
    FOLLOWUP = "followup"
    DETERMINISTIC = "deterministic"
    ALL = (USER_SPAN, FOLLOWUP, DETERMINISTIC)


class WebErrorCode:
    TARGET_STALE = "target_stale"
    UNSUPPORTED_SURFACE = "unsupported_surface"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    NEEDS_INPUT = "needs_input"
    POLICY_BLOCKED = "policy_blocked"
    CONFIRM_REQUIRED = "confirm_required"
    CONFIRM_EXPIRED = "confirm_expired"
    CONFIRM_USED = "confirm_used"
    CONFIRM_MISMATCH = "confirm_mismatch"
    ORIGIN_CHANGED = "origin_changed"
    COMMIT_UNKNOWN = "commit_unknown"
    BUDGET_EXHAUSTED = "budget_exhausted"
    VERIFICATION_UNSATISFIED = "verification_unsatisfied"
    INVALID_PROPOSAL = "invalid_proposal"
    ALL = (
        TARGET_STALE, UNSUPPORTED_SURFACE, CAPABILITY_UNAVAILABLE,
        NEEDS_INPUT, POLICY_BLOCKED, CONFIRM_REQUIRED, CONFIRM_EXPIRED,
        CONFIRM_USED, CONFIRM_MISMATCH, ORIGIN_CHANGED, COMMIT_UNKNOWN,
        BUDGET_EXHAUSTED, VERIFICATION_UNSATISFIED, INVALID_PROPOSAL,
    )


def _clean(value: Any, limit: int | None) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] if limit is not None else text


def text_hash(text: str) -> str:
    """确认令牌只存文本 SHA-256，绝不存输入正文。"""

    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WebTarget:
    """本次 observation 内的一个可交互候选；``target_id`` 仅本次有效。"""

    target_id: str
    role: str = "generic"
    label: str = ""
    enabled: bool = True
    visible: bool = True
    editable: bool = False
    checked: bool | None = None
    selected: bool | None = None
    input_type: str = ""
    destination_origin: str = ""
    sensitive: bool = False
    operations: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_id", _clean(self.target_id, None))
        object.__setattr__(self, "role", _clean(self.role, None) or "generic")
        object.__setattr__(self, "label", _clean(self.label, MAX_TARGET_LABEL_CHARS))
        object.__setattr__(self, "input_type", _clean(self.input_type, None))
        object.__setattr__(self, "destination_origin", _clean(self.destination_origin, None))
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(self, "visible", bool(self.visible))
        object.__setattr__(self, "editable", bool(self.editable))
        object.__setattr__(self, "sensitive", bool(self.sensitive))
        object.__setattr__(self, "operations", tuple(str(op) for op in (self.operations or ())))
        if not self.target_id:
            raise ValueError("WebTarget requires a non-empty target_id")

    def as_jev_element(self) -> str:
        """给 Jev 的一行元素描述；只含结构化语义，不含坐标。"""

        state = [flag for flag, on in (("enabled", self.enabled), ("visible", self.visible),
                                       ("editable", self.editable), ("checked", self.checked),
                                       ("selected", self.selected)) if on]
        line = f"{self.target_id} | role={self.role} | label={self.label} | state={','.join(state) or 'none'}"
        if self.operations:
            line += f" | ops={','.join(self.operations)}"
        return line


@dataclass(frozen=True)
class TextCandidate:
    """代码从用户原话/follow-up/确定性变换产出的 verbatim 候选文本。"""

    candidate_id: str
    text: str
    source: str = CandidateSource.USER_SPAN

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _clean(self.candidate_id, None))
        object.__setattr__(self, "text", str(self.text or ""))
        object.__setattr__(self, "source", _clean(self.source, None))
        if not self.candidate_id:
            raise ValueError("TextCandidate requires a non-empty candidate_id")
        if self.source not in CandidateSource.ALL:
            raise ValueError(f"unknown candidate source: {self.source}")


@dataclass(frozen=True)
class Observation:
    """一次网页观察的冻结快照；所有 target_id 仅在此 observation 内有效。"""

    observation_id: str
    page_revision: str
    session_id: str
    tab_id: str
    origin: str
    title: str = ""
    loading_state: str = "unknown"
    text: str = ""
    targets: tuple[WebTarget, ...] = ()
    omitted_target_count: int = 0
    supported_actions: tuple[str, ...] = WebOperation.ALL

    def __post_init__(self) -> None:
        for name in ("observation_id", "page_revision", "session_id", "tab_id"):
            if not _clean(getattr(self, name), None):
                raise ValueError(f"Observation requires a non-empty {name}")
        object.__setattr__(self, "title", _clean(self.title, MAX_TARGET_LABEL_CHARS * 2))
        object.__setattr__(self, "text", _clean(self.text, MAX_PAGE_TEXT_CHARS))
        object.__setattr__(self, "origin", _clean(self.origin, None))
        object.__setattr__(self, "loading_state", _clean(self.loading_state, None) or "unknown")
        targets = tuple(self.targets or ())
        seen: set[str] = set()
        for target in targets:
            if target.target_id in seen:
                raise ValueError(f"duplicate target_id: {target.target_id}")
            seen.add(target.target_id)
        omitted = int(self.omitted_target_count or 0)
        if len(targets) > MAX_TARGETS:
            omitted += len(targets) - MAX_TARGETS
            targets = targets[:MAX_TARGETS]
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "omitted_target_count", max(0, omitted))
        object.__setattr__(self, "supported_actions", tuple(str(a) for a in (self.supported_actions or ())))

    def target(self, target_id: str) -> WebTarget | None:
        wanted = _clean(target_id, None)
        return next((item for item in self.targets if item.target_id == wanted), None)

    def to_jev_state(
        self,
        goal: str,
        candidates: Mapping[str, str] | None = None,
        recent_actions: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """构建送给 Jev 的 state。

        隐私硬规则：只送 origin+title+截断可见文本，**不送完整 URL**；
        候选文本以 ``candidates`` 映射进入（模型只选 id，不读原文也可决策）。
        """

        return {
            "goal": _clean(goal, 400),
            "page": {
                "origin": self.origin,
                "title": self.title,
                "text": self.text,
                "loading": self.loading_state,
                "omitted_targets": self.omitted_target_count,
            },
            "elements": [target.as_jev_element() for target in self.targets],
            "candidates": dict(candidates or {}),
            "recent_actions": [dict(item) for item in (recent_actions or ())][:MAX_RECENT_ACTIONS],
        }


@dataclass(frozen=True)
class ActionProposal:
    """代码组装的候选执行计划。

    契约级 select-not-generate：**本类没有自由文本字段**。输入与导航目的地
    只能通过 ``text_candidate_id`` 引用 :class:`TextCandidate`。
    """

    observation_id: str
    page_revision: str
    operation: str
    target_id: str = ""
    text_candidate_id: str = ""
    select_option_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation_id", _clean(self.observation_id, None))
        object.__setattr__(self, "page_revision", _clean(self.page_revision, None))
        object.__setattr__(self, "operation", _clean(self.operation, None))
        object.__setattr__(self, "target_id", _clean(self.target_id, None))
        object.__setattr__(self, "text_candidate_id", _clean(self.text_candidate_id, None))
        object.__setattr__(self, "select_option_id", _clean(self.select_option_id, None))

    def validate(self) -> str | None:
        """返回 :class:`WebErrorCode` 之一或 ``None``（合法）。"""

        if not self.observation_id or not self.page_revision:
            return WebErrorCode.INVALID_PROPOSAL
        if self.operation not in WebOperation.ALL:
            return WebErrorCode.INVALID_PROPOSAL
        if self.operation in WebOperation.TARGETED and not self.target_id:
            return WebErrorCode.INVALID_PROPOSAL
        if self.operation in WebOperation.TEXT_BEARING and not self.text_candidate_id:
            # 没有候选文本绝不编造：调用方应转为 NEEDS_INPUT。
            return WebErrorCode.NEEDS_INPUT
        if self.operation == WebOperation.SELECT and not self.select_option_id:
            return WebErrorCode.INVALID_PROPOSAL
        return None

    def binds(self, observation: Observation) -> bool:
        """三元组绑定检查：observation_id + page_revision + target 仍在册。"""

        if (
            self.observation_id != observation.observation_id
            or self.page_revision != observation.page_revision
        ):
            return False
        if self.operation in WebOperation.TARGETED:
            return observation.target(self.target_id) is not None
        return True


@dataclass(frozen=True)
class PolicyVerdict:
    decision: str
    effect_class: str = EffectClass.UNKNOWN
    reason: str = ""
    sensitive_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", _clean(self.reason, 400))
        object.__setattr__(self, "sensitive_flags", tuple(str(f) for f in (self.sensitive_flags or ())))
        if self.decision not in PolicyDecision.ALL:
            raise ValueError(f"unknown policy decision: {self.decision}")
        if self.effect_class not in EffectClass.ALL:
            raise ValueError(f"unknown effect class: {self.effect_class}")


@dataclass(frozen=True)
class ConfirmationToken:
    """一次性确认令牌；绑定最终结构化计划而非用户原话（doc 16 §5.1-5.2）。"""

    token_id: str
    operation_run_id: str
    session_id: str
    tab_id: str
    origin: str
    observation_id: str
    page_revision: str
    target_id: str
    operation: str
    input_text_hash: str = ""
    effect_class: str = EffectClass.UNKNOWN
    expires_after_seconds: float = DEFAULT_CONFIRM_TTL_SECONDS

    def __post_init__(self) -> None:
        for name in ("token_id", "operation_run_id", "session_id", "tab_id",
                     "origin", "observation_id", "page_revision", "operation"):
            if not _clean(getattr(self, name), None):
                raise ValueError(f"ConfirmationToken requires a non-empty {name}")
        if self.expires_after_seconds <= 0:
            raise ValueError("expires_after_seconds must be positive")

    def plan_fingerprint(self) -> str:
        """被确认计划的确定性指纹；任何绑定字段变化都会改变它。"""

        parts = (
            self.operation_run_id, self.session_id, self.tab_id, self.origin,
            self.observation_id, self.page_revision, self.target_id,
            self.operation, self.input_text_hash, self.effect_class,
        )
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()

    @staticmethod
    def fingerprint_for(
        *,
        operation_run_id: str,
        session_id: str,
        tab_id: str,
        origin: str,
        observation_id: str,
        page_revision: str,
        target_id: str,
        operation: str,
        input_text_hash: str = "",
        effect_class: str = EffectClass.UNKNOWN,
    ) -> str:
        parts = (
            operation_run_id, session_id, tab_id, origin,
            observation_id, page_revision, target_id,
            operation, input_text_hash, effect_class,
        )
        return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VerificationCheck:
    kind: str
    expected: str
    actual: str = ""
    passed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _clean(self.kind, None))
        object.__setattr__(self, "expected", _clean(self.expected, 400))
        object.__setattr__(self, "actual", _clean(self.actual, 400))
        if not self.kind:
            raise ValueError("VerificationCheck requires a non-empty kind")


@dataclass(frozen=True)
class VerificationResult:
    """三态验证结论；只有 ``satisfied`` 才允许上层发 completed。"""

    status: str
    checks: tuple[VerificationCheck, ...] = ()
    commit_state: str = CommitState.NOT_COMMITTED
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", tuple(self.checks or ()))
        object.__setattr__(self, "detail", _clean(self.detail, 400))
        if self.status not in ("satisfied", "unverified", "contradicted"):
            raise ValueError(f"unknown verification status: {self.status}")
        if self.commit_state not in CommitState.ALL:
            raise ValueError(f"unknown commit state: {self.commit_state}")

    @property
    def satisfied(self) -> bool:
        return self.status == "satisfied"


@dataclass(frozen=True)
class WebGoalError:
    """结构化错误；日志与事件只允许携带 code，不得携带页面正文或 URL token。"""

    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", _clean(self.detail, 200))
        if self.code not in WebErrorCode.ALL:
            raise ValueError(f"unknown web goal error code: {self.code}")


__all__ = [
    "ActionProposal",
    "CandidateSource",
    "Channel",
    "CommitState",
    "ConfirmationToken",
    "DEFAULT_CONFIRM_TTL_SECONDS",
    "EffectClass",
    "MAX_PAGE_TEXT_CHARS",
    "MAX_RECENT_ACTIONS",
    "MAX_STATE_CHARS",
    "MAX_TARGETS",
    "MAX_TARGET_LABEL_CHARS",
    "Observation",
    "PolicyDecision",
    "PolicyVerdict",
    "TextCandidate",
    "VerificationCheck",
    "VerificationResult",
    "WebErrorCode",
    "WebGoalError",
    "WebOperation",
    "WebTarget",
    "text_hash",
]
