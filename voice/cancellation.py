"""可在线程间共享的语音操作取消令牌。"""
from __future__ import annotations

import threading


class VoiceCancelled(RuntimeError):
    """阻塞的采音/识别阶段观察到取消。"""


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise VoiceCancelled("voice operation cancelled")
