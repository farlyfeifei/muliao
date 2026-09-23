"""Privacy-safe bridge from streaming ASR updates to display-only captions.

The bridge is intended to be called by a future capture-frame callback.  It does
not expose recognizer text until a fixed wake phrase has appeared in a partial
hypothesis, and none of its events are actionable command inputs.
"""
from __future__ import annotations

import queue
import threading
from typing import Any, Mapping, Protocol

from .asr_streaming import StreamingTranscriptUpdate
from .wake import WakeDetector


class StreamingRecognizer(Protocol):
    """Minimal recognizer lifecycle consumed by :class:`StreamingCaptionBridge`."""

    def accept_waveform(
        self,
        pcm: bytes | bytearray | memoryview,
        *,
        sample_rate: int = 16_000,
        channels: int = 1,
        sample_width: int = 2,
    ) -> tuple[StreamingTranscriptUpdate, ...]: ...

    def finish(self) -> tuple[StreamingTranscriptUpdate, ...]: ...

    def reset(self) -> None: ...

    def close(self) -> None: ...


class EventSink(Protocol):
    def emit(self, event_type: str, payload: Mapping[str, Any]) -> None: ...


class StreamingCaptionBridge:
    """Wake-gated, display-only event bridge for streaming recognition.

    Before wake detection the bridge emits only ``voice.metric`` events whose
    payloads contain no recognizer text.  Once the injected ``WakeDetector``
    recognizes the fixed phrase in a partial, every caption is stripped to the
    command following that phrase.  A streaming final ends and resets the
    utterance; it is explicitly display-only and never actionable.
    """

    _PARTIAL_EVENT = "voice.partial"
    _FINAL_EVENT = "voice.final"
    _METRIC_EVENT = "voice.metric"

    def __init__(
        self,
        recognizer: StreamingRecognizer,
        wake: WakeDetector,
        events: EventSink,
        echo_guard: Any | None = None,
    ) -> None:
        self.recognizer = recognizer
        self.wake = wake
        self.events = events
        self.echo_guard = echo_guard
        self._lock = threading.RLock()
        self._generation = 0
        self._utterance_id = 0
        self._active = False
        self._awake = False
        self._previous_command = ""
        self._closed = False

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def utterance_id(self) -> int:
        with self._lock:
            return self._utterance_id

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def _ensure_open_locked(self) -> None:
        if self._closed:
            raise RuntimeError("streaming caption bridge is closed")

    def _begin_utterance_locked(self) -> tuple[int, int]:
        if not self._active:
            self._utterance_id += 1
            self._active = True
        return self._generation, self._utterance_id

    def _is_current_locked(self, generation: int, utterance_id: int) -> bool:
        return (
            not self._closed
            and generation == self._generation
            and utterance_id == self._utterance_id
        )

    def _echo_playing(self) -> bool:
        guard = self.echo_guard
        return bool(guard is not None and getattr(guard, "playing", False))

    def _echo_should_drop(self, text: str) -> bool:
        guard = self.echo_guard
        if guard is None:
            return False
        should_drop = getattr(guard, "should_drop", None)
        return bool(callable(should_drop) and should_drop(text))

    def accept_pcm(
        self,
        pcm: bytes | bytearray | memoryview,
        *,
        sample_rate: int = 16_000,
        channels: int = 1,
        sample_width: int = 2,
    ) -> None:
        """Accept a capture frame and synchronously emit safe caption events."""

        with self._lock:
            self._ensure_open_locked()
            generation, utterance_id = self._begin_utterance_locked()
            if self._echo_playing():
                self._reset_locked()
                return

        updates = self.recognizer.accept_waveform(
            pcm,
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
        )
        with self._lock:
            if not self._is_current_locked(generation, utterance_id):
                return
        self._consume_updates(updates, generation, utterance_id)

    def finish(self) -> None:
        """Flush the active utterance and reset after its display-only final."""

        with self._lock:
            self._ensure_open_locked()
            if not self._active:
                return
            generation, utterance_id = self._begin_utterance_locked()
            if self._echo_playing():
                self._reset_locked()
                return

        updates = self.recognizer.finish()
        with self._lock:
            if not self._is_current_locked(generation, utterance_id):
                return
        self._consume_updates(updates, generation, utterance_id)

        with self._lock:
            if self._is_current_locked(generation, utterance_id):
                self._reset_locked()

    def _consume_updates(
        self,
        updates: tuple[StreamingTranscriptUpdate, ...],
        generation: int,
        utterance_id: int,
    ) -> None:
        for update in updates:
            with self._lock:
                if not self._is_current_locked(generation, utterance_id):
                    return
                if self._echo_playing() or self._echo_should_drop(update.raw_text):
                    self._reset_locked()
                    return
                emitted_final = self._consume_update_locked(
                    update, generation, utterance_id
                )
                if emitted_final:
                    self._reset_locked()
                    return

    def _consume_update_locked(
        self,
        update: StreamingTranscriptUpdate,
        generation: int,
        utterance_id: int,
    ) -> bool:
        command: str | None
        if self._awake:
            match = self.wake.detect(update.text)
            if match is not None:
                command = match.command
            else:
                candidate = update.text.strip()
                if self._previous_command and candidate.startswith(self._previous_command):
                    command = candidate
                else:
                    self._awake = False
                    self._previous_command = ""
                    self.events.emit(
                        self._METRIC_EVENT,
                        {
                            "name": "caption_wake_lost",
                            "value": 1,
                            "generation": generation,
                            "utterance_id": utterance_id,
                        },
                    )
                    return update.is_final
        else:
            if update.is_final:
                self.events.emit(
                    self._METRIC_EVENT,
                    {
                        "name": "caption_wake_miss",
                        "value": 1,
                        "generation": generation,
                        "utterance_id": utterance_id,
                    },
                )
                return True
            match = self.wake.detect(update.text)
            if match is None:
                self.events.emit(
                    self._METRIC_EVENT,
                    {
                        "name": "caption_wake_pending",
                        "value": 1,
                        "generation": generation,
                        "utterance_id": utterance_id,
                    },
                )
                return False
            self._awake = True
            command = match.command

        stable_text = command if update.is_final else self._longest_common_prefix(
            self._previous_command, command
        )
        self._previous_command = command
        payload = {
            "text": command,
            "stable_text": stable_text,
            "final": update.is_final,
            "actionable": False,
            "display_only": True,
            "generation": generation,
            "utterance_id": utterance_id,
        }
        self.events.emit(
            self._FINAL_EVENT if update.is_final else self._PARTIAL_EVENT,
            payload,
        )
        return update.is_final

    @staticmethod
    def _longest_common_prefix(left: str, right: str) -> str:
        limit = min(len(left), len(right))
        index = 0
        while index < limit and left[index] == right[index]:
            index += 1
        return right[:index]

    def reset(self) -> None:
        """Invalidate pending updates and discard the current utterance."""

        with self._lock:
            self._ensure_open_locked()
            self._reset_locked()

    def _reset_locked(self) -> None:
        self._generation += 1
        self._active = False
        self._awake = False
        self._previous_command = ""
        self.recognizer.reset()

    def close(self) -> None:
        """Invalidate pending callbacks and close the recognizer once."""

        with self._lock:
            if self._closed:
                return
            self._generation += 1
            self._active = False
            self._awake = False
            self._previous_command = ""
            self._closed = True
            self.recognizer.close()


class AsyncCaptionPump:
    """Bounded display-only worker that never blocks authoritative capture/ASR."""

    _STOP = object()

    def __init__(
        self,
        bridge: StreamingCaptionBridge,
        *,
        max_items: int = 8,
        close_timeout: float = 0.5,
        thread_factory: Any = threading.Thread,
    ) -> None:
        if max_items < 1:
            raise ValueError("max_items must be positive")
        self.bridge = bridge
        self.events = bridge.events
        self.close_timeout = max(0.0, float(close_timeout))
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max_items)
        self._lock = threading.RLock()
        self._closed = False
        self._thread = thread_factory(
            target=self._run,
            name="voice-caption-worker",
            daemon=True,
        )
        self._thread.start()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def is_running(self) -> bool:
        return self._thread.is_alive()

    def accept_pcm(
        self,
        pcm: bytes | bytearray | memoryview,
        *,
        sample_rate: int = 16_000,
        channels: int = 1,
        sample_width: int = 2,
    ) -> bool:
        item = (
            "pcm",
            bytes(pcm),
            int(sample_rate),
            int(channels),
            int(sample_width),
        )
        return self._offer(item, metric="caption_frame_dropped")

    def finish(self) -> bool:
        return self._offer(("finish",), metric="caption_finish_dropped")

    def reset(self) -> bool:
        return self._offer(("reset",), metric="caption_reset_dropped")

    def _offer(self, item: Any, *, metric: str) -> bool:
        with self._lock:
            if self._closed:
                return False
            try:
                self._queue.put_nowait(item)
                return True
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    pass
                try:
                    self._queue.put_nowait(item)
                    self.events.emit("voice.metric", {"name": metric, "value": 1})
                    return True
                except queue.Full:
                    self.events.emit("voice.metric", {"name": metric, "value": 1})
                    return False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    break
            self._queue.put_nowait(self._STOP)
        self._thread.join(timeout=self.close_timeout)
        if self._thread.is_alive():
            self.events.emit(
                "voice.error",
                {"code": "caption_worker_close_timeout", "detail": "caption worker did not stop"},
            )

    def _run(self) -> None:
        try:
            while True:
                item = self._queue.get()
                try:
                    if item is self._STOP:
                        return
                    kind = item[0]
                    if kind == "pcm":
                        _, pcm, sample_rate, channels, sample_width = item
                        self.bridge.accept_pcm(
                            pcm,
                            sample_rate=sample_rate,
                            channels=channels,
                            sample_width=sample_width,
                        )
                    elif kind == "finish":
                        try:
                            self.bridge.finish()
                        except Exception:
                            self.bridge.reset()
                            raise
                    elif kind == "reset":
                        self.bridge.reset()
                except Exception as exc:
                    self.events.emit(
                        "voice.error",
                        {
                            "code": f"caption_{item[0]}_failed",
                            "detail": f"{type(exc).__name__}: {exc}",
                        },
                    )
                finally:
                    self._queue.task_done()
        finally:
            try:
                self.bridge.close()
            except Exception as exc:
                self.events.emit(
                    "voice.error",
                    {"code": "caption_close_failed", "detail": f"{type(exc).__name__}: {exc}"},
                )


__all__ = ["AsyncCaptionPump", "StreamingCaptionBridge"]
