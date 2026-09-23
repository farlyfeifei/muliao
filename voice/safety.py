"""Safety primitives for emergency stop handling and TTS echo suppression.

The classes in this module are deliberately independent from the voice runtime:
callers inject callbacks, key readers, clocks, and downstream frame consumers.  No
raw audio is retained by either echo-protection class.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import numbers
import sys
import threading
import time
import unicodedata
from typing import Any, Callable, Sequence, TypeVar


VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_SPACE = 0x20
DEFAULT_EMERGENCY_KEYS = (VK_CONTROL, VK_SHIFT, VK_SPACE)

T = TypeVar("T")
Generation = int | str


class EmergencyStopUnavailable(RuntimeError):
    """Raised when the default emergency-stop backend is unavailable."""


class EmergencyStopWatcher:
    """Poll a key combination and invoke a callback on each rising edge.

    On Windows the default reader uses ``GetAsyncKeyState`` directly, so this
    class has no dependency on the third-party ``keyboard`` package.  A custom
    ``key_state_reader(vk_code) -> bool`` makes the watcher deterministic in
    tests and usable with another host-provided key source.

    The default combination is Ctrl+Shift+Space.  Holding it produces one
    callback; all keys must be released (or the combination otherwise broken)
    before another callback can be produced.
    """

    def __init__(
        self,
        callback: Callable[[], Any],
        *,
        key_state_reader: Callable[[int], bool] | None = None,
        key_codes: Sequence[int] = DEFAULT_EMERGENCY_KEYS,
        poll_interval: float = 0.03,
        platform: str | None = None,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        if not callable(callback):
            raise TypeError("callback must be callable")
        keys = tuple(int(key) for key in key_codes)
        if not keys:
            raise ValueError("key_codes must not be empty")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")

        self.callback = callback
        self.key_codes = keys
        self.poll_interval = float(poll_interval)
        self.platform = platform or sys.platform
        self._thread_factory = thread_factory
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._combo_down = False
        self._closed = False
        self._last_error: BaseException | None = None
        self._unavailable_reason = ""

        if key_state_reader is not None:
            self._key_state_reader = key_state_reader
        else:
            self._key_state_reader = self._build_default_reader()

    def _build_default_reader(self) -> Callable[[int], bool] | None:
        if self.platform != "win32":
            self._unavailable_reason = (
                "the default emergency-stop watcher is only available on Windows"
            )
            return None
        try:
            import ctypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            get_async_key_state = user32.GetAsyncKeyState
            get_async_key_state.argtypes = [ctypes.c_int]
            get_async_key_state.restype = ctypes.c_short
        except BaseException as exc:
            self._unavailable_reason = (
                f"Windows GetAsyncKeyState is unavailable: {type(exc).__name__}: {exc}"
            )
            return None

        def read(vk_code: int) -> bool:
            # The high bit represents the current physical down state.  The low
            # bit is a historical transition flag and must not trigger the stop.
            return bool(get_async_key_state(int(vk_code)) & 0x8000)

        return read

    @property
    def available(self) -> bool:
        return self._key_state_reader is not None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def last_error(self) -> BaseException | None:
        with self._lock:
            return self._last_error

    @property
    def thread(self) -> threading.Thread | None:
        """Return the current worker for lifecycle inspection."""

        with self._lock:
            return self._thread

    def start(self) -> bool:
        """Start polling; return ``False`` if it was already running."""

        with self._lock:
            if self._closed:
                raise RuntimeError("emergency-stop watcher is closed")
            if self._key_state_reader is None:
                reason = self._unavailable_reason or "emergency-stop watcher is unavailable"
                raise EmergencyStopUnavailable(reason)
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._combo_down = False
            thread = self._thread_factory(
                target=self._run,
                name="voice-emergency-stop",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return True

    def poll_once(self) -> bool:
        """Read one snapshot and invoke the callback on a rising edge.

        Returns ``True`` only when this poll attempted the callback.  Reader and
        callback failures are retained in :attr:`last_error`; the background
        watcher remains alive so a transient failure cannot silently disable the
        emergency-stop path.
        """

        reader = self._key_state_reader
        if reader is None:
            reason = self._unavailable_reason or "emergency-stop watcher is unavailable"
            raise EmergencyStopUnavailable(reason)
        try:
            pressed = all(bool(reader(key)) for key in self.key_codes)
        except BaseException as exc:
            with self._lock:
                self._last_error = exc
            return False

        with self._lock:
            trigger = pressed and not self._combo_down
            self._combo_down = pressed
        if not trigger:
            return False

        try:
            self.callback()
        except BaseException as exc:
            with self._lock:
                self._last_error = exc
        return True

    def stop(self, timeout: float | None = None) -> bool:
        """Request shutdown and wait for the polling thread to exit.

        ``False`` means the optional timeout elapsed.  Calling ``stop`` from the
        callback itself is supported; that call signals shutdown without trying
        to join its own thread.
        """

        with self._lock:
            thread = self._thread
            self._stop_event.set()
        if thread is None:
            return True
        if thread is threading.current_thread():
            return True
        thread.join(timeout=timeout)
        stopped = not thread.is_alive()
        if stopped:
            with self._lock:
                if self._thread is thread:
                    self._thread = None
        return stopped

    def close(self, timeout: float | None = None) -> bool:
        """Permanently stop the watcher.  The operation is idempotent."""

        with self._lock:
            if self._closed:
                thread = self._thread
            else:
                self._closed = True
                self._stop_event.set()
                thread = self._thread
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(timeout=timeout)
        stopped = not thread.is_alive()
        if stopped:
            with self._lock:
                if self._thread is thread:
                    self._thread = None
        return stopped

    def _run(self) -> None:
        current = threading.current_thread()
        try:
            while not self._stop_event.is_set():
                self.poll_once()
                self._stop_event.wait(self.poll_interval)
        finally:
            with self._lock:
                if self._thread is current:
                    self._thread = None

    def __enter__(self) -> EmergencyStopWatcher:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class CaptureMuteGate:
    """Thread-safe generation-owned gate in front of VAD/ASR ingestion.

    The gate never stores frames.  Use :meth:`filter_frame` for a simple
    pass/drop decision, or :meth:`submit` when the check and delivery to a frame
    consumer must be one atomic operation relative to ``tts_started``.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active_generation: Generation | None = None
        self._latest_generation: Generation | None = None
        self._sequence = 0
        self._muted = False

    @staticmethod
    def _older(generation: Generation, latest: Generation | None) -> bool:
        return (
            latest is not None
            and isinstance(generation, numbers.Integral)
            and not isinstance(generation, bool)
            and isinstance(latest, numbers.Integral)
            and not isinstance(latest, bool)
            and int(generation) < int(latest)
        )

    def _next_generation_locked(self) -> int:
        self._sequence += 1
        if isinstance(self._latest_generation, numbers.Integral) and not isinstance(
            self._latest_generation, bool
        ):
            self._sequence = max(self._sequence, int(self._latest_generation) + 1)
        return self._sequence

    @property
    def muted(self) -> bool:
        with self._lock:
            return self._muted

    @property
    def is_muted(self) -> bool:
        return self.muted

    @property
    def capture_allowed(self) -> bool:
        with self._lock:
            return not self._muted

    @property
    def active_generation(self) -> Generation | None:
        with self._lock:
            return self._active_generation

    def allows_capture(self) -> bool:
        return self.capture_allowed

    def allow_frame(self, frame: Any | None = None) -> bool:
        del frame
        return self.capture_allowed

    def tts_started(self, generation: Generation | None = None) -> Generation:
        with self._lock:
            selected: Generation = (
                self._next_generation_locked() if generation is None else generation
            )
            if self._older(selected, self._latest_generation):
                return selected
            self._latest_generation = selected
            if isinstance(selected, numbers.Integral) and not isinstance(selected, bool):
                self._sequence = max(self._sequence, int(selected))
            self._active_generation = selected
            self._muted = True
            return selected

    def tts_finished(self, generation: Generation | None = None) -> bool:
        return self._release(generation)

    def tts_cancelled(self, generation: Generation | None = None) -> bool:
        return self._release(generation)

    def _release(self, generation: Generation | None) -> bool:
        with self._lock:
            if self._active_generation is None:
                return False
            selected = self._active_generation if generation is None else generation
            if selected != self._active_generation:
                return False
            self._active_generation = None
            self._muted = False
            return True

    # Event-style aliases make the object easy to wire to a TTS event sink.
    on_tts_started = tts_started
    on_tts_finished = tts_finished
    on_tts_cancelled = tts_cancelled

    def filter_frame(self, frame: T) -> T | None:
        """Return the frame unchanged when open, otherwise ``None``.

        The object keeps no reference to ``frame`` after this method returns.
        """

        with self._lock:
            if self._muted:
                return None
            return frame

    def submit(self, frame: T, consumer: Callable[[T], Any]) -> bool:
        """Atomically pass a frame to ``consumer`` only while capture is open."""

        if not callable(consumer):
            raise TypeError("consumer must be callable")
        with self._lock:
            if self._muted:
                return False
            consumer(frame)
            return True


@dataclass(frozen=True)
class RecentTts:
    """Metadata retained for the latest TTS generation (text only, never audio)."""

    text: str
    normalized_text: str
    generation: Generation
    started_at: float
    finished_at: float | None = None
    cancelled: bool = False


def normalize_for_echo(text: str) -> str:
    """Normalize ASR/TTS text by removing Unicode punctuation and spacing."""

    normalized = unicodedata.normalize("NFKC", str(text)).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and unicodedata.category(character)[0] not in {"P", "Z"}
    )


def _edit_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, 1):
        current = [left_index]
        for right_index, right_character in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1]
                    + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


def edit_similarity(left: str, right: str) -> float:
    """Return normalized Levenshtein similarity for already normalized text."""

    if not left or not right:
        return 1.0 if left == right else 0.0
    return 1.0 - (_edit_distance(left, right) / max(len(left), len(right)))


class EchoGuard:
    """Suppress transcripts caused by the most recent TTS playback.

    While TTS is playing every transcript is rejected and the associated
    :class:`CaptureMuteGate` blocks frames before VAD/ASR.  After playback ends,
    only text similar to the latest TTS is rejected, for 500 ms by default.
    Generation ownership prevents late finish/cancel/start notifications from an
    older integer generation from changing the current playback state.
    """

    def __init__(
        self,
        *,
        post_playback_window_ms: float = 500.0,
        similarity_threshold: float = 0.80,
        clock: Callable[[], float] = time.monotonic,
        mute_gate: CaptureMuteGate | None = None,
    ) -> None:
        if post_playback_window_ms < 0:
            raise ValueError("post_playback_window_ms must not be negative")
        if not 0.0 <= similarity_threshold <= 1.0:
            raise ValueError("similarity_threshold must be between 0 and 1")
        self.post_playback_window_ms = float(post_playback_window_ms)
        self.similarity_threshold = float(similarity_threshold)
        self._clock = clock
        self.mute_gate = mute_gate or CaptureMuteGate()
        self._lock = threading.RLock()
        self._recent: RecentTts | None = None
        self._playing = False
        self._sequence = 0

    @property
    def post_playback_window_seconds(self) -> float:
        return self.post_playback_window_ms / 1000.0

    @property
    def playing(self) -> bool:
        with self._lock:
            return self._playing

    @property
    def generation(self) -> Generation | None:
        with self._lock:
            return None if self._recent is None else self._recent.generation

    @property
    def recent_tts(self) -> RecentTts | None:
        with self._lock:
            return self._recent

    @property
    def is_muted(self) -> bool:
        return self.mute_gate.muted

    def _next_generation_locked(self) -> int:
        self._sequence += 1
        if self._recent is not None and isinstance(
            self._recent.generation, numbers.Integral
        ) and not isinstance(self._recent.generation, bool):
            self._sequence = max(self._sequence, int(self._recent.generation) + 1)
        return self._sequence

    def tts_started(
        self,
        text: str,
        generation: Generation | None = None,
        *,
        now: float | None = None,
    ) -> Generation:
        timestamp = self._clock() if now is None else float(now)
        normalized = normalize_for_echo(text)
        with self._lock:
            selected: Generation = (
                self._next_generation_locked() if generation is None else generation
            )
            current = None if self._recent is None else self._recent.generation
            if CaptureMuteGate._older(selected, current):
                return selected
            if isinstance(selected, numbers.Integral) and not isinstance(selected, bool):
                self._sequence = max(self._sequence, int(selected))
            self._recent = RecentTts(
                text=str(text),
                normalized_text=normalized,
                generation=selected,
                started_at=timestamp,
            )
            self._playing = True
            self.mute_gate.tts_started(selected)
            return selected

    def tts_finished(
        self,
        generation: Generation | None = None,
        *,
        now: float | None = None,
    ) -> bool:
        return self._end_playback(generation, cancelled=False, now=now)

    def tts_cancelled(
        self,
        generation: Generation | None = None,
        *,
        now: float | None = None,
    ) -> bool:
        return self._end_playback(generation, cancelled=True, now=now)

    def _end_playback(
        self,
        generation: Generation | None,
        *,
        cancelled: bool,
        now: float | None,
    ) -> bool:
        timestamp = self._clock() if now is None else float(now)
        with self._lock:
            if self._recent is None or not self._playing:
                return False
            selected = self._recent.generation if generation is None else generation
            if selected != self._recent.generation:
                return False
            self._recent = replace(
                self._recent,
                finished_at=timestamp,
                cancelled=cancelled,
            )
            self._playing = False
            if cancelled:
                self.mute_gate.tts_cancelled(selected)
            else:
                self.mute_gate.tts_finished(selected)
            return True

    on_tts_started = tts_started
    on_tts_finished = tts_finished
    on_tts_cancelled = tts_cancelled

    def clear(self, generation: Generation | None = None) -> bool:
        """Forget retained text, optionally only for the matching generation."""

        with self._lock:
            if self._recent is None:
                return False
            if generation is not None and generation != self._recent.generation:
                return False
            selected = self._recent.generation
            was_playing = self._playing
            self._recent = None
            self._playing = False
            if was_playing:
                self.mute_gate.tts_cancelled(selected)
            return True

    def should_drop(
        self,
        asr_text: str,
        *,
        generation: Generation | None = None,
        now: float | None = None,
    ) -> bool:
        """Return whether an ASR transcript must be discarded as TTS echo."""

        timestamp = self._clock() if now is None else float(now)
        normalized_asr = normalize_for_echo(asr_text)
        with self._lock:
            recent = self._recent
            if self._playing:
                return True
            if recent is None or recent.finished_at is None:
                return False
            if generation is not None and generation != recent.generation:
                return False
            age = timestamp - recent.finished_at
            if age < 0 or age > self.post_playback_window_seconds:
                return False
            normalized_tts = recent.normalized_text

        if not normalized_asr or not normalized_tts:
            return False
        if normalized_asr in normalized_tts or normalized_tts in normalized_asr:
            return True
        return (
            edit_similarity(normalized_asr, normalized_tts)
            >= self.similarity_threshold
        )

    def is_echo(
        self,
        asr_text: str,
        *,
        generation: Generation | None = None,
        now: float | None = None,
    ) -> bool:
        return self.should_drop(asr_text, generation=generation, now=now)

    should_drop_asr = is_echo

    def accept_asr(
        self,
        asr_text: str,
        *,
        generation: Generation | None = None,
        now: float | None = None,
    ) -> bool:
        return not self.should_drop(asr_text, generation=generation, now=now)


__all__ = [
    "CaptureMuteGate",
    "DEFAULT_EMERGENCY_KEYS",
    "EchoGuard",
    "EmergencyStopUnavailable",
    "EmergencyStopWatcher",
    "RecentTts",
    "VK_CONTROL",
    "VK_SHIFT",
    "VK_SPACE",
    "edit_similarity",
    "normalize_for_echo",
]
