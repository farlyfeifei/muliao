"""唤醒后的短会话窗口。"""
from __future__ import annotations

import threading
import time
from typing import Callable


class VoiceSession:
    """线程安全的短会话状态。

    唤醒或成功动作后，允许在 ``window_seconds`` 内直接说下一条命令。
    """

    def __init__(self, window_seconds: float = 8.0, clock: Callable[[], float] = time.monotonic) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.window_seconds = float(window_seconds)
        self._clock = clock
        self._active_until = 0.0
        self._lock = threading.Lock()

    def activate(self) -> None:
        with self._lock:
            self._active_until = self._clock() + self.window_seconds

    def touch(self) -> None:
        self.activate()

    def close(self) -> None:
        with self._lock:
            self._active_until = 0.0

    def is_active(self) -> bool:
        with self._lock:
            return self._active_until > self._clock()

    def remaining_seconds(self) -> float:
        with self._lock:
            return max(0.0, self._active_until - self._clock())
