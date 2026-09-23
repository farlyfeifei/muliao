"""sherpa-onnx VITS（aishell3）离线神经 TTS 说话人（M5）。

doc 13 / doc 18 §5.4：离线播报降级链是 MiMo 云 → VITS 本地神经 → SAPI 本地系统。
本模块提供中间的 VITS 层。模型文件在本机不存在，因此说话人**懒加载**且通过依赖
注入完全可单测——构造时不碰文件系统、不导入 sherpa_onnx、不开声卡。

设计要点（与既有 voice 模块一致）：

- 公共契约对齐 :class:`voice.tts_local.SapiSpeaker`：``speak(text, *, cancellation,
  operation_id)`` / ``stop(*, operation_id)`` / ``close()``；同时暴露 ``cancel()``
  以便充当 :class:`voice.tts_local.FallbackSpeaker` 的 cloud 层（MiMo→VITS→SAPI 用
  两层 FallbackSpeaker 嵌套实现，VITS 是内层的 cloud，SAPI 是内层的 local）。
- 并发/取消沿用 :class:`voice.tts_mimo.MiMoTtsClient` 的 generation+epoch 模型：
  ``stop()``/``close()``/新的 ``speak()`` 先递增 generation 再打断，合成或播放途中
  被取代的旧 epoch 绝不再发出声音。播放经 :class:`voice.audio_player.CancellableAudioPlayer`。
- 懒加载对齐 :class:`voice.asr_local.SenseVoiceRecognizer._get_recognizer`：首次
  ``speak`` 才检查文件并加载；缺文件先抛 ``FileNotFoundError``（在导入 sherpa_onnx
  之前），让 FallbackSpeaker 优雅降级到 SAPI，绝不 fail closed 整个播报。
- 隐私：绝不持久化或记录播报文本、PCM 音频或模型路径以外的内容；无密钥。
- 默认 ``tts_factory``/``player_factory`` 只在真机执行；测试注入 fake，因此本模块
  在无 sherpa_onnx 高层 API、无模型、无声卡的机器上也能完整单测。
"""
from __future__ import annotations

from pathlib import Path
import threading
from typing import Any, Callable

import numpy as np

from .audio_player import CancellableAudioPlayer, PyAudioPcmSink
from .cancellation import CancellationToken


# A complete sherpa-onnx VITS aishell3 export: acoustic net, token table, pinyin
# lexicon, and the jieba dict directory. All four must be present to load.
REQUIRED_VITS_FILES = ("model.onnx", "tokens.txt", "lexicon.txt", "dict")


class VitsSynthesisError(RuntimeError):
    """结构化合成/播放失败；调用方可据此降级到 SAPI，不在实时路径自动重试。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


def _samples_to_pcm16(samples: Any) -> bytes:
    """Convert float32 samples in [-1, 1] to little-endian signed 16-bit PCM.

    Explicit ``<i2`` keeps the byte order deterministic across platforms (the
    PCM sinks expect int16 LE). Values are clipped, then scaled by 32767 so
    1.0 maps to 0x7fff and -1.0 maps to -32767 without asymmetric overflow.
    """

    array = np.asarray(samples, dtype=np.float32)
    if array.ndim != 1:
        array = array.reshape(-1)
    array = np.clip(array, -1.0, 1.0)
    return (array * 32767.0).astype("<i2").tobytes()


def _default_tts_factory(model_dir: Any, *, num_threads: int = 2, **_: Any) -> Any:
    """Build a real sherpa-onnx VITS OfflineTts. Only runs on a real machine.

    Uses the documented high-level config API. The factory is reached only after
    ``_get_tts`` confirms the four asset paths exist, so a missing model raises
    ``FileNotFoundError`` (graceful SAPI fallback) before sherpa_onnx is imported.
    """

    import sherpa_onnx

    root = Path(model_dir)
    vits = sherpa_onnx.OfflineTtsVitsModelConfig(
        model=str(root / "model.onnx"),
        lexicon=str(root / "lexicon.txt"),
        tokens=str(root / "tokens.txt"),
        dict_dir=str(root / "dict"),
    )
    model_config = sherpa_onnx.OfflineTtsModelConfig(
        vits=vits,
        num_threads=int(num_threads),
        provider="cpu",
    )
    return sherpa_onnx.OfflineTts(config=sherpa_onnx.OfflineTtsConfig(model=model_config))


def _default_player_factory(sample_rate: int) -> CancellableAudioPlayer:
    return CancellableAudioPlayer(PyAudioPcmSink(sample_rate=int(sample_rate)))


class VitsSpeaker:
    """Offline VITS speaker; lazy-loaded, cancellable, injection-testable."""

    enabled = True

    def __init__(
        self,
        model_dir: Any,
        *,
        tts_factory: Callable[..., Any] | None = None,
        player_factory: Callable[[int], Any] | None = None,
        sample_rate: int = 22_050,
        num_threads: int = 2,
        speaker_id: int = 0,
        speed: float = 1.0,
        chunk_bytes: int = 16_384,
        drain_timeout: float = 10.0,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if chunk_bytes < 1024:
            raise ValueError("chunk_bytes must be at least 1024")
        self.model_dir = Path(model_dir)
        self._tts_factory = tts_factory or _default_tts_factory
        self._player_factory = player_factory or _default_player_factory
        self.sample_rate = int(sample_rate)
        self.num_threads = int(num_threads)
        self.speaker_id = int(speaker_id)
        self.speed = float(speed)
        self.chunk_bytes = int(chunk_bytes)
        self.drain_timeout = float(drain_timeout)
        self.last_backend = "vits"
        # Model load is serialized separately from playback state so a slow first
        # load never holds the cancellation lock.
        self._load_lock = threading.RLock()
        self._tts: Any | None = None
        self._transition_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._generation = 0
        self._operation_id: int | str | None = None
        self._closed = False
        self._player: Any | None = None
        self._active_token: CancellationToken | None = None
        self._active_player_generation: int | None = None

    # -- public speaker contract ------------------------------------------

    def speak(
        self,
        text: str,
        *,
        cancellation: CancellationToken | None = None,
        operation_id: int | str | None = None,
    ) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("VITS speaker is closed")
        if not str(text or "").strip():
            return
        # Always own a token so cancel()/stop() can interrupt internal synthesis
        # even when the caller passed none (mirrors MiMoTtsClient).
        token = cancellation or CancellationToken()
        if token.cancelled:
            return

        with self._transition_lock:
            with self._state_lock:
                if self._closed:
                    raise RuntimeError("VITS speaker is closed")
                if self._is_stale_operation(operation_id):
                    return
                if operation_id is not None:
                    self._operation_id = operation_id
                previous_token = self._active_token
                previous_player_generation = self._active_player_generation
                self._generation += 1
                generation = self._generation
                self._active_token = token
                self._active_player_generation = None
            if previous_token is not None:
                previous_token.cancel()
            if previous_player_generation is not None and self._player is not None:
                self._player.stop(generation=previous_player_generation)

        # Load and synthesize OUTSIDE the transition lock. A first VITS load and
        # a neural generate() are both slow, blocking native calls that cannot be
        # interrupted mid-flight; holding the lock over them would stop cancel()
        # from ever claiming it (the bug that lets audio outlive an emergency
        # stop). Instead cancel() takes the lock, bumps the generation and
        # cancels the token, and we re-check supersession the instant generate()
        # returns, discarding the result so no audio is emitted for a dead
        # operation. Mirrors MiMoTtsClient, whose SSE consumption is also outside
        # its startup transition.
        try:
            tts = self._get_tts()
            if self._superseded(generation, token):
                return
            result = tts.generate(text=str(text), sid=self.speaker_id, speed=self.speed)
            if self._superseded(generation, token):
                return
            pcm, rate = self._extract_pcm(result)
        except BaseException:
            # A load/synthesis failure must not leave this generation owning
            # audio state; clear it and re-raise so FallbackSpeaker degrades to
            # SAPI (a missing model is FileNotFoundError, also handled here).
            with self._state_lock:
                if generation == self._generation:
                    self._active_token = None
            raise
        if self._superseded(generation, token):
            return

        # Begin a fresh player generation under the lock, then stream OUTSIDE it.
        with self._transition_lock:
            if self._superseded(generation, token):
                return
            player = self._ensure_player(rate)
            player_generation = player.begin()
            with self._state_lock:
                if self._superseded_locked(generation, token):
                    owns = False
                else:
                    self._active_player_generation = player_generation
                    owns = True
            if not owns:
                player.stop(generation=player_generation)
                return

        try:
            self._play_pcm(player, player_generation, pcm, generation, token)
            if not self._superseded(generation, token):
                self._await_drain(player, player_generation)
        finally:
            if self._superseded(generation, token):
                player.stop(generation=player_generation)
            with self._state_lock:
                if generation == self._generation:
                    self._active_token = None
                    drained = getattr(player, "is_drained", None)
                    if callable(drained) and drained(player_generation):
                        self._active_player_generation = None

    def cancel(self, generation: int | None = None) -> bool:
        """Interrupt current synthesis/playback; the FallbackSpeaker cloud hook."""

        with self._transition_lock:
            with self._state_lock:
                if self._closed:
                    return False
                if generation is not None and generation != self._generation:
                    return False
                token = self._active_token
                player_generation = self._active_player_generation
                player = self._player
                if token is None and player_generation is None:
                    return False
                self._generation += 1
                self._active_token = None
                self._active_player_generation = None
            if token is not None:
                token.cancel()
            if player_generation is not None and player is not None:
                player.stop(generation=player_generation)
            return True

    def stop(self, *, operation_id: int | str | None = None) -> bool:
        """SapiSpeaker-parity stop: cancel playback for the current operation."""

        with self._state_lock:
            if self._closed:
                return False
            if (
                operation_id is not None
                and self._operation_id is not None
                and operation_id != self._operation_id
            ):
                return False
        return self.cancel()

    def close(self) -> None:
        with self._transition_lock:
            with self._state_lock:
                if self._closed:
                    return
                self._closed = True
                self._generation += 1
                token = self._active_token
                player_generation = self._active_player_generation
                player = self._player
                self._active_token = None
                self._active_player_generation = None
            if token is not None:
                token.cancel()
        if player is not None:
            if player_generation is not None:
                stop = getattr(player, "stop", None)
                if callable(stop):
                    try:
                        stop(generation=player_generation)
                    except Exception:
                        pass
            close = getattr(player, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        with self._load_lock:
            self._tts = None

    def __enter__(self) -> "VitsSpeaker":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- internals --------------------------------------------------------

    def _get_tts(self) -> Any:
        with self._load_lock:
            if self._tts is not None:
                return self._tts
            missing = [
                str(self.model_dir / name)
                for name in REQUIRED_VITS_FILES
                if not (self.model_dir / name).exists()
            ]
            if missing:
                # Raised BEFORE importing sherpa_onnx so the caller degrades to
                # SAPI on a machine without the model rather than half-loading.
                raise FileNotFoundError("missing VITS assets: " + ", ".join(missing))
            self._tts = self._tts_factory(self.model_dir, num_threads=self.num_threads)
            return self._tts

    def _ensure_player(self, sample_rate: int) -> Any:
        with self._state_lock:
            if self._player is None:
                self._player = self._player_factory(int(sample_rate))
            return self._player

    def _extract_pcm(self, result: Any) -> tuple[bytes, int]:
        samples = getattr(result, "samples", None)
        if samples is None:
            raise VitsSynthesisError("empty_samples", "VITS produced no audio samples")
        pcm = _samples_to_pcm16(samples)
        if not pcm:
            raise VitsSynthesisError("empty_samples", "VITS produced empty audio")
        try:
            rate = int(getattr(result, "sample_rate", 0) or 0)
        except (TypeError, ValueError):
            rate = 0
        return pcm, rate if rate > 0 else self.sample_rate

    def _play_pcm(
        self,
        player: Any,
        player_generation: int,
        pcm: bytes,
        generation: int,
        token: CancellationToken,
    ) -> None:
        offset = 0
        total = len(pcm)
        while offset < total:
            if self._superseded(generation, token):
                return
            end = min(offset + self.chunk_bytes, total)
            chunk = pcm[offset:end]
            offset = end
            if player.play(chunk, generation=player_generation):
                continue
            # play() returned False: either the bounded queue is full or the
            # generation went stale. Re-check supersession, then drain and retry.
            if self._superseded(generation, token):
                return
            if not player.wait_until_drained(player_generation, timeout=self.drain_timeout):
                raise VitsSynthesisError(
                    "audio_playback_timeout", "VITS playback stalled on a full queue"
                )
            if self._superseded(generation, token):
                return
            if not player.play(chunk, generation=player_generation):
                raise VitsSynthesisError(
                    "audio_queue_full", "VITS audio queue rejected a chunk"
                )

    def _await_drain(self, player: Any, player_generation: int) -> None:
        wait = getattr(player, "wait_until_drained", None)
        if callable(wait) and not wait(player_generation, timeout=self.drain_timeout):
            raise VitsSynthesisError(
                "audio_playback_timeout", "VITS audio did not finish playing"
            )
        failure = getattr(player, "failure", None)
        device_error = failure(player_generation) if callable(failure) else None
        if device_error is not None:
            raise VitsSynthesisError(
                "audio_output_error",
                f"VITS audio output failed: {type(device_error).__name__}",
            )

    def _is_stale_operation(self, operation_id: int | str | None) -> bool:
        return (
            operation_id is not None
            and self._operation_id is not None
            and isinstance(operation_id, int)
            and isinstance(self._operation_id, int)
            and operation_id < self._operation_id
        )

    def _superseded_locked(self, generation: int, token: CancellationToken) -> bool:
        if self._closed:
            return True
        if token.cancelled:
            return True
        return generation != self._generation

    def _superseded(self, generation: int, token: CancellationToken) -> bool:
        with self._state_lock:
            return self._superseded_locked(generation, token)


__all__ = ["REQUIRED_VITS_FILES", "VitsSpeaker", "VitsSynthesisError"]
