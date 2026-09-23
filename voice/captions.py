"""Privacy-safe bridge from streaming ASR updates to display-only captions.

The bridge is intended to be called by a future capture-frame callback.  It does
not expose recognizer text until a fixed wake phrase has appeared in a partial
hypothesis, and none of its events are actionable command inputs.
"""
from __future__ import annotations

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
            command = match.command if match is not None else update.text.strip()
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


__all__ = ["StreamingCaptionBridge"]
