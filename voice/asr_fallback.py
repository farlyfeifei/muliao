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

from .cancellation import CancellationToken, VoiceCancelled
from .contracts import AudioSegment, Transcript


def _is_missing_model_error(exc: BaseException) -> bool:
    """本地模型不可用（缺失/无法加载）→ 云备援才有意义。

    只对**确定**的"模型资产缺失/无法加载"判定为可上云，绝不把真实解码错误
    （native 失败、PCM 对齐、流创建失败等）误判上去——那会把已通过唤醒门控的
    音频错误地发给云端，违背"仅模型缺失才上云"的隐私承诺。因此：

    - ``FileNotFoundError``：SenseVoiceRecognizer 在资产文件不存在时抛它，明确可上云。
    - ``ImportError``：sherpa_onnx 未安装，本地无法识别，可上云。
    - 其余一律不上云。不再无条件放行所有 ``OSError``（native 解码失败常以 OSError
      形态出现），也不用 "failed to load"/"cannot load" 之类宽泛子串匹配真实运行期
      错误。
    """

    return isinstance(exc, (FileNotFoundError, ImportError))


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

    def _get_recognizer(self) -> Any:
        """Delegate to the primary local recognizer's lazy loader.

        Warm-up and model validation (voice/service.py, voice/models.py) force the
        local SenseVoice model to load via ``recognizer._get_recognizer()``. When
        the runtime wraps the local recognizer in this fallback, that accessor must
        still reach the *local* model — the cloud client is never pre-loaded, so a
        missing local asset fails warm-up exactly as before.
        """

        loader = getattr(self.primary, "_get_recognizer", None)
        if not callable(loader):
            raise AttributeError("primary recognizer has no _get_recognizer()")
        return loader()

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
        except VoiceCancelled:
            # A barge-in during the cloud round-trip is a user cancellation, not
            # a cloud failure. Re-raise untouched so it never increments the
            # consecutive-failure counter or trips the breaker; the caller's
            # cancelled path stays authoritative.
            raise
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
