"""独立 ``voice.*`` 事件信封。"""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Mapping


@dataclass(frozen=True)
class VoiceEvent:
    type: str
    seq: int
    ts: float
    payload: Mapping[str, Any] = field(default_factory=dict)


class MemoryEventSink:
    """测试和独立开发页面使用的内存事件接收器。"""

    def __init__(self) -> None:
        self.events: list[VoiceEvent] = []

    def emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if not event_type.startswith("voice."):
            raise ValueError("voice event type must use the voice.* namespace")
        self.events.append(
            VoiceEvent(
                type=event_type,
                seq=len(self.events) + 1,
                ts=time.time(),
                payload=dict(payload),
            )
        )


class NullEventSink:
    def emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if not event_type.startswith("voice."):
            raise ValueError("voice event type must use the voice.* namespace")
