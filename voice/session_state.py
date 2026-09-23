"""唤醒后的 owner-generation 短会话窗口。"""
from __future__ import annotations

import threading
import time
from typing import Callable


class VoiceSession:
    """旧 operation 无法刷新或关闭新 operation 拥有的会话。"""

    def __init__(self, window_seconds: float = 8.0, clock: Callable[[], float] = time.monotonic) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.window_seconds = float(window_seconds)
        self._clock = clock
        self._active_until = 0.0
        self._owner: int | None = None
        self._lock = threading.Lock()

    @property
    def owner(self) -> int | None:
        with self._lock:
            return self._owner

    def activate(self, owner: int | None = None) -> bool:
        with self._lock:
            self._owner = owner
            self._active_until = self._clock() + self.window_seconds
            return True

    def touch(self, owner: int | None = None) -> bool:
        with self._lock:
            # 只有 engine 当前 operation 才会调用 touch；成功 follow-up 应接管 owner。
            self._owner = owner if owner is not None else self._owner
            self._active_until = self._clock() + self.window_seconds
            return True

    def claim(self, owner: int) -> bool:
        """把仍有效的会话所有权转给当前 operation。"""
        with self._lock:
            if self._active_until <= self._clock():
                self._active_until = 0.0
                self._owner = None
                return False
            self._owner = owner
            return True

    def close(self, owner: int | None = None) -> bool:
        with self._lock:
            if owner is not None and self._owner is not None and owner != self._owner:
                return False
            self._active_until = 0.0
            self._owner = None
            return True

    def is_active(self) -> bool:
        with self._lock:
            if self._active_until <= self._clock():
                self._active_until = 0.0
                self._owner = None
                return False
            return True

    def remaining_seconds(self) -> float:
        with self._lock:
            remaining = max(0.0, self._active_until - self._clock())
            if remaining == 0.0:
                self._owner = None
            return remaining
