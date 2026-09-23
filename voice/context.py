"""受限、线程安全的语音指代上下文与 Jev state 构建器。

这里只保存当前桌面任务所需的短期事实。它不采集也不接受浏览历史、书签或
文件正文；敏感控件在进入 state 前会被丢弃。
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import json
import re
import threading
import time
from typing import Any, Callable

MAX_STATE_CHARS = 24_000
MAX_TRANSCRIPT_CHARS = 400
MAX_ELEMENTS = 100
MAX_ELEMENT_TEXT = 60
MAX_CONTEXT_ACTIONS = 3

_DEFAULT_TTL_SECONDS = 120.0
_SENSITIVE_RE = re.compile(r"(?:secret|token|password|passwd|pwd|cookie)", re.IGNORECASE)
_PASSWORD_ROLES = {
    "password",
    "passwordbox",
    "password box",
    "securetext",
    "secure text",
    "securetextbox",
    "secure text box",
}
_FORBIDDEN_SOURCE_KEYS = {
    "history",
    "browser_history",
    "browsing_history",
    "bookmarks",
    "bookmark",
    "recent_files",
    "recent_documents",
    "file_body",
    "file_content",
    "document_body",
    "document_content",
}
_FORBIDDEN_SOURCE_RE = re.compile(
    r"(?:browser[ _-]?history|browsing[ _-]?history|bookmarks?|recent[ _-]?(?:files|documents)|"
    r"(?:file|document)[ _-]?(?:body|content))",
    re.IGNORECASE,
)


def _clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text[:limit]


def _finite_ttl(value: float, name: str) -> float:
    value = float(value)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _sanitize_value(value: Any, *, depth: int = 0) -> Any:
    """Convert a bounded value to JSON-safe data while removing private fields."""

    if depth >= 6:
        return _clip(value, MAX_ELEMENT_TEXT)
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for raw_key in sorted(value, key=str):
            key = str(raw_key)
            normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
            if (
                normalized in _FORBIDDEN_SOURCE_KEYS
                or _FORBIDDEN_SOURCE_RE.search(key)
                or _SENSITIVE_RE.search(key)
            ):
                continue
            item = _sanitize_value(value[raw_key], depth=depth + 1)
            if isinstance(item, str) and (
                _FORBIDDEN_SOURCE_RE.search(item) or _SENSITIVE_RE.search(item)
            ):
                continue
            cleaned[key] = item
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item, depth=depth + 1) for item in value[:MAX_ELEMENTS]]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return _clip(value, MAX_ELEMENT_TEXT)


def _clean_mapping(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    cleaned = _sanitize_value(value)
    return cleaned or None


def _app_payload(app: "AppContext | None") -> dict[str, str] | None:
    if app is None:
        return None
    payload = {"name": app.name, "window_title": app.window_title}
    return {key: value for key, value in payload.items() if value}


def _previous_app_payload(app: "AppContext | None") -> dict[str, str] | None:
    if app is None:
        return None
    payload = {"app": app.name, "title": app.window_title}
    return {key: value for key, value in payload.items() if value}


def _coerce_app(value: "AppContext | Mapping[str, Any] | None") -> "AppContext | None":
    if value is None:
        return None
    if isinstance(value, AppContext):
        return AppContext(_clip(value.name, MAX_ELEMENT_TEXT), _clip(value.window_title, MAX_ELEMENT_TEXT))
    if not isinstance(value, Mapping):
        raise TypeError("app context must be AppContext, mapping, or None")
    return AppContext(
        name=_clip(value.get("name") or value.get("app") or value.get("process"), MAX_ELEMENT_TEXT),
        window_title=_clip(value.get("window_title") or value.get("title"), MAX_ELEMENT_TEXT),
    )


@dataclass(frozen=True)
class AppContext:
    name: str = ""
    window_title: str = ""


@dataclass(frozen=True)
class ElementSummary:
    """一个可供 Jev 选择的非敏感 UI 元素摘要。"""

    element_id: str
    role: str = ""
    label: str = ""
    enabled: bool = True
    visible: bool = True
    password: bool = False
    source: str = "uia"

    @classmethod
    def from_value(cls, value: "ElementSummary | Mapping[str, Any] | str", index: int) -> "ElementSummary":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(element_id=f"e{index:02d}", label=value)
        if not isinstance(value, Mapping):
            raise TypeError("element must be ElementSummary, mapping, or string")
        return cls(
            element_id=str(value.get("element_id") or value.get("id") or f"e{index:02d}"),
            role=str(value.get("role") or value.get("control_type") or value.get("type") or ""),
            label=str(value.get("label") or value.get("name") or value.get("text") or ""),
            enabled=bool(value.get("enabled", True)),
            visible=bool(value.get("visible", value.get("on_screen", True))),
            password=bool(
                value.get("password", False)
                or value.get("is_password", False)
                or value.get("is_password_field", False)
                or value.get("protected", False)
            ),
            source=str(value.get("source") or "uia"),
        )

    def is_safe(self) -> bool:
        role = self.role.strip().lower().replace("_", " ")
        source = self.source.strip().lower().replace("-", "_")
        if not self.enabled or not self.visible or self.password:
            return False
        if role in _PASSWORD_ROLES or "password" in role or role.startswith("secure text"):
            return False
        if source in _FORBIDDEN_SOURCE_KEYS or _FORBIDDEN_SOURCE_RE.search(source):
            return False
        return not (
            _SENSITIVE_RE.search(f"{self.element_id} {self.role} {self.label}")
            or _FORBIDDEN_SOURCE_RE.search(f"{self.role} {self.label}")
        )

    def render(self, index: int) -> str:
        element_id = _clip(self.element_id.strip() or f"e{index:02d}", 24)
        role = _clip(self.role.strip(), 24)
        label = _clip(self.label.strip(), MAX_ELEMENT_TEXT)
        if not label:
            return ""
        escaped = label.replace("\\", "\\\\").replace('"', '\\"')
        return " ".join(part for part in (element_id, role, f'"{escaped}"') if part)


@dataclass(frozen=True)
class ActionContext:
    said: str
    action: str
    target: str = ""
    outcome: str = "ok"
    detail: str = ""
    at: float = 0.0

    def to_state(self, now: float) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "said": _clip(self.said, MAX_TRANSCRIPT_CHARS),
            "action": _clip(self.action, MAX_ELEMENT_TEXT),
            "outcome": _clip(self.outcome, 24),
            "seconds_ago": max(0, int(now - self.at)),
        }
        if self.target:
            payload["target"] = _clip(self.target, MAX_ELEMENT_TEXT)
        if self.detail:
            payload["detail"] = _clip(self.detail, MAX_ELEMENT_TEXT)
        return payload


@dataclass(frozen=True)
class ContextSnapshot:
    foreground_app: AppContext | None
    previous_app: AppContext | None
    actions: tuple[ActionContext, ...]
    pending_confirmation: Mapping[str, Any] | None
    elements: tuple[str, ...]
    last_target: str | None
    captured_at: float


class VoiceContext:
    """带 TTL 的受限短期状态。

    ``snapshot``、``build_state`` 和 ``to_json`` 都会先清除过期数据。所有读写均
    由同一把重入锁保护，适合采集、路由和动作线程并发调用。
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        confirmation_ttl_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = _finite_ttl(ttl_seconds, "ttl_seconds")
        self.confirmation_ttl_seconds = _finite_ttl(
            confirmation_ttl_seconds if confirmation_ttl_seconds is not None else ttl_seconds,
            "confirmation_ttl_seconds",
        )
        self._clock = clock
        self._lock = threading.RLock()
        self._foreground_app: AppContext | None = None
        self._previous_app: AppContext | None = None
        self._apps_at = 0.0
        self._actions: deque[ActionContext] = deque(maxlen=MAX_CONTEXT_ACTIONS)
        self._pending_confirmation: dict[str, Any] | None = None
        self._pending_at = 0.0
        self._elements: tuple[str, ...] = ()
        self._elements_at = 0.0

    def set_foreground_app(self, app: AppContext | Mapping[str, Any] | None) -> None:
        current = _coerce_app(app)
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            if current != self._foreground_app:
                self._previous_app = self._foreground_app
            self._foreground_app = current
            self._apps_at = now if current is not None else 0.0

    def set_elements(self, elements: Iterable[ElementSummary | Mapping[str, Any] | str]) -> None:
        safe = sanitize_elements(elements)
        with self._lock:
            self._elements = tuple(safe)
            self._elements_at = self._clock() if safe else 0.0

    def record_action(
        self,
        *,
        said: str,
        action: str,
        target: str = "",
        ok: bool = True,
        outcome: str | None = None,
        detail: str = "",
    ) -> ActionContext:
        now = self._clock()
        entry = ActionContext(
            said=_clip(said, MAX_TRANSCRIPT_CHARS),
            action=_clip(action, MAX_ELEMENT_TEXT),
            target=_clip(target, MAX_ELEMENT_TEXT),
            outcome=_clip(outcome or ("ok" if ok else "failed"), 24),
            detail=_clip(detail, MAX_ELEMENT_TEXT),
            at=now,
        )
        with self._lock:
            self._purge_locked(now)
            self._actions.append(entry)
        return entry

    def record_success(self, *, said: str, action: str, target: str = "", detail: str = "") -> ActionContext:
        return self.record_action(said=said, action=action, target=target, ok=True, detail=detail)

    def record_failure(self, *, said: str, action: str, target: str = "", detail: str = "") -> ActionContext:
        return self.record_action(said=said, action=action, target=target, ok=False, detail=detail)

    def set_pending_confirmation(self, value: Mapping[str, Any] | None) -> None:
        cleaned = _clean_mapping(value)
        with self._lock:
            self._pending_confirmation = cleaned
            self._pending_at = self._clock() if cleaned is not None else 0.0

    def clear_pending_confirmation(self) -> None:
        self.set_pending_confirmation(None)

    def clear(self) -> None:
        with self._lock:
            self._foreground_app = None
            self._previous_app = None
            self._apps_at = 0.0
            self._actions.clear()
            self._pending_confirmation = None
            self._pending_at = 0.0
            self._elements = ()
            self._elements_at = 0.0

    def purge(self) -> bool:
        with self._lock:
            return self._purge_locked(self._clock())

    def _purge_locked(self, now: float) -> bool:
        changed = False
        if self._apps_at and now - self._apps_at >= self.ttl_seconds:
            self._foreground_app = None
            self._previous_app = None
            self._apps_at = 0.0
            changed = True
        while self._actions and now - self._actions[0].at >= self.ttl_seconds:
            self._actions.popleft()
            changed = True
        if self._elements_at and now - self._elements_at >= self.ttl_seconds:
            self._elements = ()
            self._elements_at = 0.0
            changed = True
        if self._pending_at and now - self._pending_at >= self.confirmation_ttl_seconds:
            self._pending_confirmation = None
            self._pending_at = 0.0
            changed = True
        return changed

    def snapshot(self) -> ContextSnapshot:
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            actions = tuple(self._actions)
            last_target = next((item.target for item in reversed(actions) if item.target), None)
            pending = dict(self._pending_confirmation) if self._pending_confirmation is not None else None
            return ContextSnapshot(
                foreground_app=self._foreground_app,
                previous_app=self._previous_app,
                actions=actions,
                pending_confirmation=pending,
                elements=self._elements,
                last_target=last_target,
                captured_at=now,
            )

    def build_state(
        self,
        utterance: str,
        *,
        candidates: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return build_jev_state(utterance, snapshot=self.snapshot(), candidates=candidates)

    def to_json(self, utterance: str, *, candidates: Mapping[str, Any] | None = None) -> str:
        return serialize_jev_state(self.build_state(utterance, candidates=candidates))


VoiceContextStore = VoiceContext


def sanitize_elements(elements: Iterable[ElementSummary | Mapping[str, Any] | str]) -> list[str]:
    """返回最多 100 个稳定、可读且非敏感的元素摘要。"""

    safe: list[str] = []
    for index, raw in enumerate(elements, 1):
        element = ElementSummary.from_value(raw, index)
        if not element.is_safe():
            continue
        rendered = element.render(index)
        if rendered:
            safe.append(rendered)
        if len(safe) >= MAX_ELEMENTS:
            break
    return safe


def _candidate_payload(candidates: Mapping[str, Any] | None) -> dict[str, str]:
    if not candidates:
        return {}
    result: dict[str, str] = {}
    for key in sorted(candidates, key=str):
        name = _clip(key, 24)
        value = _clip(candidates[key], MAX_TRANSCRIPT_CHARS)
        if not name or not value:
            continue
        if _SENSITIVE_RE.search(name) or _FORBIDDEN_SOURCE_RE.search(name):
            continue
        result[name] = value
    return result


def _state_from_snapshot(
    utterance: str,
    snapshot: ContextSnapshot,
    candidates: Mapping[str, Any] | None,
) -> dict[str, Any]:
    actions = [item.to_state(snapshot.captured_at) for item in snapshot.actions[-MAX_CONTEXT_ACTIONS:]]
    context: dict[str, Any] = {
        "previous": _previous_app_payload(snapshot.previous_app),
        "recent_actions": actions,
        "last_target": snapshot.last_target,
    }
    return {
        "utterance": _clip(utterance, MAX_TRANSCRIPT_CHARS),
        "foreground_app": _app_payload(snapshot.foreground_app),
        "elements": list(snapshot.elements[:MAX_ELEMENTS]),
        "context": context,
        "candidates": _candidate_payload(candidates),
        "pending_confirmation": dict(snapshot.pending_confirmation) if snapshot.pending_confirmation else None,
    }


def _encoded_length(state: Mapping[str, Any]) -> int:
    return len(serialize_jev_state(state))


def _fit_state(state: dict[str, Any]) -> dict[str, Any]:
    """确定性地移除低优先级信息，直到满足 24k 字符硬上限。"""

    if _encoded_length(state) <= MAX_STATE_CHARS:
        return state

    candidates = state["candidates"]
    for key in sorted(tuple(candidates), reverse=True):
        if _encoded_length(state) <= MAX_STATE_CHARS:
            break
        candidates.pop(key, None)

    elements = state["elements"]
    while elements and _encoded_length(state) > MAX_STATE_CHARS:
        elements.pop()

    actions = state["context"]["recent_actions"]
    for action in actions:
        action.pop("detail", None)
    while len(actions) > 1 and _encoded_length(state) > MAX_STATE_CHARS:
        actions.pop(0)

    pending = state.get("pending_confirmation")
    if isinstance(pending, dict):
        for key in sorted(tuple(pending), reverse=True):
            if _encoded_length(state) <= MAX_STATE_CHARS:
                break
            pending.pop(key, None)

    if _encoded_length(state) > MAX_STATE_CHARS:
        state["candidates"] = {}
        state["elements"] = []
    if _encoded_length(state) > MAX_STATE_CHARS:
        state["pending_confirmation"] = None
    if _encoded_length(state) > MAX_STATE_CHARS:
        state["context"]["recent_actions"] = []
        state["context"]["last_target"] = None
    if _encoded_length(state) > MAX_STATE_CHARS:
        state["context"]["previous"] = None
        state["foreground_app"] = None
    if _encoded_length(state) > MAX_STATE_CHARS:
        state["utterance"] = _clip(state["utterance"], max(0, MAX_TRANSCRIPT_CHARS // 2))
    if _encoded_length(state) > MAX_STATE_CHARS:
        raise ValueError("Jev state could not be reduced below MAX_STATE_CHARS")
    return state


def build_jev_state(
    utterance: str,
    *,
    snapshot: ContextSnapshot | None = None,
    foreground_app: AppContext | Mapping[str, Any] | None = None,
    previous_app: AppContext | Mapping[str, Any] | None = None,
    recent_actions: Sequence[ActionContext] = (),
    elements: Iterable[ElementSummary | Mapping[str, Any] | str] = (),
    candidates: Mapping[str, Any] | None = None,
    pending_confirmation: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """纯函数：把已知短期事实构建成受限 Jev state。"""

    if snapshot is None:
        captured_at = time.monotonic() if now is None else float(now)
        limited_actions = tuple(recent_actions[-MAX_CONTEXT_ACTIONS:])
        snapshot = ContextSnapshot(
            foreground_app=_coerce_app(foreground_app),
            previous_app=_coerce_app(previous_app),
            actions=limited_actions,
            pending_confirmation=_clean_mapping(pending_confirmation),
            elements=tuple(sanitize_elements(elements)),
            last_target=next((item.target for item in reversed(limited_actions) if item.target), None),
            captured_at=captured_at,
        )
    state = _state_from_snapshot(utterance, snapshot, candidates)
    return _fit_state(state)


def serialize_jev_state(state: Mapping[str, Any]) -> str:
    """稳定序列化；同一 state 永远得到相同 JSON 字符串。"""

    return json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "ActionContext",
    "AppContext",
    "ContextSnapshot",
    "ElementSummary",
    "MAX_CONTEXT_ACTIONS",
    "MAX_ELEMENTS",
    "MAX_ELEMENT_TEXT",
    "MAX_STATE_CHARS",
    "MAX_TRANSCRIPT_CHARS",
    "VoiceContext",
    "VoiceContextStore",
    "build_jev_state",
    "sanitize_elements",
    "serialize_jev_state",
]
