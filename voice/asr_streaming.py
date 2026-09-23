"""Streaming Zipformer captions backed by ``sherpa-onnx``.

This adapter is deliberately presentation-only.  It returns structured caption
updates and never sends text to an event sink or an action/router interface.
Command execution must continue to use the final transcript produced by the
command ASR, not the streaming hypotheses returned here.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any, Iterable, Iterator, Literal

import numpy as np


@dataclass(frozen=True, slots=True)
class StreamingTranscriptUpdate:
    """One display-only streaming ASR update.

    ``raw_text`` preserves the recognizer output. ``text`` is whitespace-
    normalized for display, and ``stable_text`` is the longest common prefix
    shared with the preceding *different* partial hypothesis. Consecutive
    duplicate partials are suppressed by the adapter.
    """

    kind: Literal["partial", "final"]
    text: str
    raw_text: str
    stable_text: str
    actionable: bool = False

    @property
    def is_final(self) -> bool:
        return self.kind == "final"

    def as_payload(self) -> dict[str, object]:
        """Return data suitable for a ``voice.partial``-style event payload."""

        return {
            "text": self.text,
            "raw_text": self.raw_text,
            "stable_text": self.stable_text,
            "final": self.is_final,
            "actionable": False,
        }


class PartialTextStabilizer:
    """Stabilize caption display without treating partial text as committed."""

    def __init__(self) -> None:
        self._previous = ""

    @staticmethod
    def normalize(text: str) -> str:
        return " ".join(text.strip().split())

    @staticmethod
    def longest_common_prefix(left: str, right: str) -> str:
        limit = min(len(left), len(right))
        index = 0
        while index < limit and left[index] == right[index]:
            index += 1
        return right[:index]

    def update(self, raw_text: str) -> tuple[str, str] | None:
        """Return ``(display_text, stable_prefix)`` or suppress a duplicate."""

        current = self.normalize(raw_text)
        if not current:
            return None
        if current == self._previous:
            return None
        stable = self.longest_common_prefix(self._previous, current)
        self._previous = current
        return current, stable

    def reset(self) -> None:
        self._previous = ""


class StreamingZipformerRecognizer:
    """Lifecycle wrapper for sherpa-onnx ``OnlineRecognizer``.

    Model assets are supplied explicitly and are checked only when the first
    stream is created. A recognizer may be injected for tests, so unit tests do
    not import sherpa-onnx or require model files.
    """

    SAMPLE_RATE = 16_000
    CHANNELS = 1
    SAMPLE_WIDTH = 2

    def __init__(
        self,
        tokens: str | Path,
        encoder: str | Path,
        decoder: str | Path,
        joiner: str | Path,
        *,
        recognizer: Any | None = None,
        num_threads: int = 2,
        decoding_method: str = "greedy_search",
        max_active_paths: int = 4,
        provider: str = "cpu",
        enable_endpoint_detection: bool = True,
        tail_padding_seconds: float = 0.5,
    ) -> None:
        if tail_padding_seconds < 0:
            raise ValueError("tail_padding_seconds must be non-negative")

        self.tokens = Path(tokens)
        self.encoder = Path(encoder)
        self.decoder = Path(decoder)
        self.joiner = Path(joiner)
        self.num_threads = num_threads
        self.decoding_method = decoding_method
        self.max_active_paths = max_active_paths
        self.provider = provider
        self.enable_endpoint_detection = enable_endpoint_detection
        self.tail_padding_seconds = tail_padding_seconds

        self._recognizer = recognizer
        self._stream: Any | None = None
        self._stabilizer = PartialTextStabilizer()
        self._last_raw_text = ""
        self._finished = False
        self._closed = False
        self._lock = threading.RLock()

    @property
    def recognizer_loaded(self) -> bool:
        return self._recognizer is not None

    @property
    def closed(self) -> bool:
        return self._closed

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("streaming recognizer is closed")

    def _get_recognizer(self) -> Any:
        if self._recognizer is not None:
            return self._recognizer

        assets = (self.tokens, self.encoder, self.decoder, self.joiner)
        missing = [str(path) for path in assets if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                "missing streaming Zipformer assets: " + ", ".join(missing)
            )

        # Importing sherpa-onnx can load native libraries, so keep it out of the
        # module import path and defer it until audio is actually accepted.
        import sherpa_onnx

        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(self.tokens),
            encoder=str(self.encoder),
            decoder=str(self.decoder),
            joiner=str(self.joiner),
            num_threads=self.num_threads,
            sample_rate=self.SAMPLE_RATE,
            decoding_method=self.decoding_method,
            max_active_paths=self.max_active_paths,
            provider=self.provider,
            enable_endpoint_detection=self.enable_endpoint_detection,
        )
        return self._recognizer

    def warmup(self, *, silence_samples: int = 320) -> None:
        """Load native assets and decode a short isolated silent stream.

        Warm-up always decodes at least once so an incompatible backend fails
        here — before the microphone opens — rather than on the first live frame.
        """

        if silence_samples <= 0:
            raise ValueError("silence_samples must be positive")
        with self._lock:
            self._ensure_open()
            recognizer = self._get_recognizer()
            stream = recognizer.create_stream()
            silence = np.zeros(silence_samples, dtype=np.float32)
            stream.accept_waveform(self.SAMPLE_RATE, silence)
            decoded = 0
            while recognizer.is_ready(stream):
                recognizer.decode_stream(stream)
                decoded += 1
            if decoded == 0:
                # The backend reported nothing ready for the probe; decode once
                # anyway to surface a broken recognizer before capture starts.
                recognizer.decode_stream(stream)
            reset_stream = getattr(recognizer, "reset", None)
            if callable(reset_stream):
                reset_stream(stream)

    def _get_stream(self) -> tuple[Any, Any]:
        recognizer = self._get_recognizer()
        if self._stream is None:
            self._stream = recognizer.create_stream()
        return recognizer, self._stream

    @staticmethod
    def _extract_text(recognizer: Any, stream: Any) -> str:
        get_result = getattr(recognizer, "get_result", None)
        if callable(get_result):
            result = get_result(stream)
        else:
            get_result_all = getattr(recognizer, "get_result_all", None)
            if callable(get_result_all):
                result = get_result_all(stream)
            else:
                result = getattr(stream, "result", "")
        if isinstance(result, str):
            return result
        return str(getattr(result, "text", "") or "")

    def _partial_update(self, raw_text: str) -> StreamingTranscriptUpdate | None:
        self._last_raw_text = raw_text
        stabilized = self._stabilizer.update(raw_text)
        if stabilized is None:
            return None
        text, stable_text = stabilized
        return StreamingTranscriptUpdate(
            kind="partial",
            text=text,
            raw_text=raw_text,
            stable_text=stable_text,
        )

    def _final_update(self, raw_text: str) -> StreamingTranscriptUpdate:
        self._last_raw_text = raw_text
        text = self._stabilizer.normalize(raw_text)
        return StreamingTranscriptUpdate(
            kind="final",
            text=text,
            raw_text=raw_text,
            stable_text=text,
        )

    def _drain(self, recognizer: Any, stream: Any) -> list[StreamingTranscriptUpdate]:
        updates: list[StreamingTranscriptUpdate] = []
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
            update = self._partial_update(self._extract_text(recognizer, stream))
            if update is not None:
                updates.append(update)
        return updates

    @staticmethod
    def _endpoint_detected(recognizer: Any, stream: Any) -> bool:
        is_endpoint = getattr(recognizer, "is_endpoint", None)
        return bool(is_endpoint(stream)) if callable(is_endpoint) else False

    def accept_waveform(
        self,
        pcm: bytes | bytearray | memoryview,
        *,
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        sample_width: int = SAMPLE_WIDTH,
    ) -> tuple[StreamingTranscriptUpdate, ...]:
        """Accept one 16 kHz int16 mono PCM chunk and return new updates."""

        with self._lock:
            self._ensure_open()
            if self._finished:
                raise RuntimeError("stream is finished; call reset() before adding audio")
            if sample_rate != self.SAMPLE_RATE:
                raise ValueError("streaming Zipformer expects 16000 Hz audio")
            if channels != self.CHANNELS or sample_width != self.SAMPLE_WIDTH:
                raise ValueError("streaming Zipformer expects 16-bit mono PCM")
            if not isinstance(pcm, (bytes, bytearray, memoryview)):
                raise TypeError("pcm must be a bytes-like object")
            if len(pcm) % self.SAMPLE_WIDTH:
                raise ValueError("PCM byte length must align to int16 samples")
            if not pcm:
                return ()

            samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            samples /= 32768.0
            recognizer, stream = self._get_stream()
            stream.accept_waveform(self.SAMPLE_RATE, samples)
            updates = self._drain(recognizer, stream)

            if self._endpoint_detected(recognizer, stream):
                raw_text = self._extract_text(recognizer, stream)
                updates.append(self._final_update(raw_text))
                self._finished = True

            return tuple(updates)

    def finish(self) -> tuple[StreamingTranscriptUpdate, ...]:
        """Flush the active stream and append exactly one final update.

        The final update remains display-only. Its text must not replace the
        separate command-ASR transcript used for routing or execution.
        """

        with self._lock:
            self._ensure_open()
            if self._stream is None:
                raise RuntimeError("no active stream to finish")
            if self._finished:
                return ()

            recognizer = self._get_recognizer()
            stream = self._stream
            if self.tail_padding_seconds:
                tail = np.zeros(
                    int(self.SAMPLE_RATE * self.tail_padding_seconds),
                    dtype=np.float32,
                )
                stream.accept_waveform(self.SAMPLE_RATE, tail)
            stream.input_finished()

            updates = self._drain(recognizer, stream)
            raw_text = self._extract_text(recognizer, stream)
            updates.append(self._final_update(raw_text))
            self._finished = True
            return tuple(updates)

    def iter_updates(
        self,
        chunks: Iterable[bytes | bytearray | memoryview],
    ) -> Iterator[StreamingTranscriptUpdate]:
        """Yield updates for chunks, followed by the flushed final update."""

        for chunk in chunks:
            yield from self.accept_waveform(chunk)
            if self._finished:
                return
        yield from self.finish()

    def reset(self) -> None:
        """Discard the current utterance and prepare for a fresh stream."""

        with self._lock:
            self._ensure_open()
            if self._recognizer is not None and self._stream is not None:
                reset_stream = getattr(self._recognizer, "reset", None)
                if callable(reset_stream):
                    reset_stream(self._stream)
            self._stream = None
            self._stabilizer.reset()
            self._last_raw_text = ""
            self._finished = False

    def close(self) -> None:
        """Release an injected recognizer when supported; safe to call twice."""

        with self._lock:
            if self._closed:
                return
            recognizer = self._recognizer
            stream = self._stream
            if recognizer is not None and stream is not None:
                reset_stream = getattr(recognizer, "reset", None)
                if callable(reset_stream):
                    reset_stream(stream)
            close_recognizer = getattr(recognizer, "close", None)
            if callable(close_recognizer):
                close_recognizer()
            self._stream = None
            self._finished = True
            self._closed = True

    def __enter__(self) -> "StreamingZipformerRecognizer":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


# Short aliases for callers that prefer adapter/update terminology.
StreamingZipformerAdapter = StreamingZipformerRecognizer
StreamingUpdate = StreamingTranscriptUpdate


__all__ = [
    "PartialTextStabilizer",
    "StreamingTranscriptUpdate",
    "StreamingUpdate",
    "StreamingZipformerAdapter",
    "StreamingZipformerRecognizer",
]
