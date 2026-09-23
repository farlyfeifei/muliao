"""本地 SenseVoice 主用、MiMo 云 ASR 备援的降级识别器（M5）。

doc 13 §4.5 的正式降级策略：

- 默认用本地 SenseVoice（快、离线、隐私好）；
- 仅在本地不可用（模型缺失、native 失败、内存不足）时，把**已通过本地唤醒门控**
  的音频交给 MiMo 云 ASR；
- 云端 2 秒超时/错误 → 回到本地失败路径，绝不把空文本送 Jev；
- 云端连续失败会触发熔断，避免每次识别都白等一次网络往返。

安全边界：

- 本识别器**不是**唤醒前的首层门控。唤醒检测永远在本地完成；只有唤醒后的指令
  音频才可能进入云备援（调用方 ``VoiceEngine`` 在 wake gate 之后才 transcribe）。
- 无 ``api_key`` 时云备援直接禁用，永不发网络。
- 不记录音频、Base64 或密钥；失败只保留结构化 code。
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any

from .cancellation import CancellationToken
from .contracts import AudioSegment, Transcript


def _is_missing_model_error(exc: BaseException) -> bool:
    """本地模型不可用（缺失/无法加载）→ 云备援才有意义。"""

    if isinstance(exc, FileNotFoundError):
        return True
    if isinstance(exc, (ImportError, OSError)):
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(
        token in text
        for token in ("missing sensevoice", "no such file", "failed to load", "cannot load")
    )


@dataclass
class FallbackStats:
    """降级统计，用于 metric 与健康观察（不含任何音频/密钥）。"""

    local_ok: int = 0
    local_failed: int = 0
    cloud_ok: int = 0
    cloud_failed: int = 0
    cloud_disabled: int = 0
    breaker_open: int = 0


class FallbackRecognizer:
    """Wrap a primary local recognizer with an optional cloud fallback."""

    def __init__(
        self,
        primary: Any,
        fallback: Any | None = None,
        *,
        api_key: str = "",
        breaker_threshold: int = 3,
        breaker_reset_seconds: float = 60.0,
        clock: Any = time.monotonic,
    ) -> None:
        if primary is None:
            raise ValueError("primary recognizer is required")
        self.primary = primary
        self.fallback = fallback
        self.api_key = api_key
        self.breaker_threshold = max(1, int(breaker_threshold))
        self.breaker_reset_seconds = max(0.0, float(breaker_reset_seconds))
        self._clock = clock
        self._lock = threading.RLock()
        self._consecutive_failures = 0
        self._breaker_opened_at: float | None = None
        self.stats = FallbackStats()

    @property
    def cloud_enabled(self) -> bool:
        return self.fallback is not None and bool(self.api_key)

    @property
    def breaker_open(self) -> bool:
        with self._lock:
            return self._breaker_opened_at is not None

    def _breaker_allows(self) -> bool:
        with self._lock:
            if self._breaker_opened_at is None:
                return True
            if self._clock() - self._breaker_opened_at >= self.breaker_reset_seconds:
                # Half-open: allow one probe; a success will reset the counter.
                self._breaker_opened_at = None
                self._consecutive_failures = 0
                return True
            return False

    def _record_cloud_failure(self) -> None:
        with self._lock:
            self.stats.cloud_failed += 1
            self._consecutive_failures += 1
            if (
                self._consecutive_failures >= self.breaker_threshold
                and self._breaker_opened_at is None
            ):
                self._breaker_opened_at = self._clock()
                self.stats.breaker_open += 1

    def _record_cloud_success(self) -> None:
        with self._lock:
            self.stats.cloud_ok += 1
            self._consecutive_failures = 0
            self._breaker_opened_at = None

    def transcribe(
        self,
        audio: AudioSegment,
        *,
        cancellation: CancellationToken | None = None,
    ) -> Transcript:
        if cancellation is not None:
            cancellation.raise_if_cancelled()

        primary_error: BaseException | None = None
        try:
            try:
                transcript = self.primary.transcribe(audio, cancellation=cancellation)
            except TypeError as exc:
                if "cancellation" not in str(exc):
                    raise
                transcript = self.primary.transcribe(audio)
        except BaseException as exc:  # noqa: BLE001 - decide fallback vs re-raise
            primary_error = exc
        else:
            with self._lock:
                self.stats.local_ok += 1
            return transcript

        with self._lock:
            self.stats.local_failed += 1

        # Only a missing/unloadable local model justifies sending audio to cloud.
        if not _is_missing_model_error(primary_error):
            raise primary_error

        if not self.cloud_enabled:
            with self._lock:
                self.stats.cloud_disabled += 1
            raise primary_error
        if not self._breaker_allows():
            raise primary_error

        if cancellation is not None:
            cancellation.raise_if_cancelled()
        try:
            cloud = self.fallback.transcribe(audio, cancellation=cancellation)
        except Exception:
            self._record_cloud_failure()
            # Never hand an empty/garbage transcript to Jev: surface the local error.
            raise primary_error
        self._record_cloud_success()
        return cloud

    def close(self) -> None:
        for recognizer in (self.primary, self.fallback):
            close = getattr(recognizer, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


__all__ = ["FallbackRecognizer", "FallbackStats"]
