"""独立 ``voice.*`` 事件信封。"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import sys
import time
from typing import Any, Mapping, TextIO


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


class JsonLineEventSink:
    """将稳定的 ``voice.*`` 事件逐行输出，供独立开发进程和未来 API 桥接。"""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout
        self.seq = 0

    def emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if not event_type.startswith("voice."):
            raise ValueError("voice event type must use the voice.* namespace")
        self.seq += 1
        event = VoiceEvent(type=event_type, seq=self.seq, ts=time.time(), payload=dict(payload))
        self.stream.write(json.dumps({
            "type": event.type,
            "seq": event.seq,
            "ts": event.ts,
            "payload": event.payload,
        }, ensure_ascii=False) + "\n")
        self.stream.flush()


class NullEventSink:
    def emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if not event_type.startswith("voice."):
            raise ValueError("voice event type must use the voice.* namespace")
