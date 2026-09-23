"""语音子系统的稳定数据契约与可注入接口。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class AudioSegment:
    """单声道 16-bit PCM 语音段。"""

    pcm: bytes
    sample_rate: int = 16_000
    sample_width: int = 2
    channels: int = 1


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str = "zh"
    confidence: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteDecision:
    """Jev 路由后的最小可执行决定。

    M0 只允许 ``open_app:notepad``。后续阶段可扩展 kind/target，但执行器仍需白名单校验。
    """

    accepted: bool
    kind: str = "none"
    target: str = "none"
    confidence: float = 0.0
    destructive: bool = False
    complete: bool = True
    reason: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    action: str
    detail: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VoiceResult:
    status: str
    transcript: str = ""
    command: str = ""
    decision: RouteDecision | None = None
    action: ActionResult | None = None
    detail: str = ""
    decisions: tuple[RouteDecision, ...] = ()
    actions: tuple[ActionResult, ...] = ()


class PermissionGate(Protocol):
    def allowed(self) -> bool: ...

    def run_if_allowed(self, callback: Callable[[], T]) -> tuple[bool, T | None]: ...


class AudioCapture(Protocol):
    def capture_utterance(self, *, cancellation: Any | None = None) -> AudioSegment: ...

    def stop(self) -> None: ...


class Recognizer(Protocol):
    def transcribe(self, audio: AudioSegment, *, cancellation: Any | None = None) -> Transcript: ...


class CommandRouter(Protocol):
    def route(self, command: str) -> RouteDecision: ...


class ActionExecutor(Protocol):
    def execute(self, decision: RouteDecision) -> ActionResult: ...


class Speaker(Protocol):
    def speak(self, text: str) -> None: ...


class EventSink(Protocol):
    def emit(self, event_type: str, payload: Mapping[str, Any]) -> None: ...
