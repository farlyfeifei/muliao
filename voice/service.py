"""Thread-safe lifecycle and event bridge for the standalone voice API.

The service deliberately owns no chat state.  Heavy voice dependencies are created
lazily, only after the ``voice_control`` permission gate succeeds.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field, is_dataclass
import inspect
import threading
import time
from typing import Any, AsyncIterator, Callable, Mapping

from .permission_gate import ExistingVoicePermission


class VoicePermissionDenied(PermissionError):
    """Raised before any microphone, router, or action dependency is started."""


class _InterruptibleCapture:
    """Add cooperative ``stop()`` to the existing single-utterance capture.

    ``PyAudioVADCapture`` intentionally owns its stream inside
    ``capture_utterance``.  The API service needs to interrupt that blocking read
    when a stop request arrives, so this adapter mirrors the small open/close shell
    while reusing the capture's tested VAD segment reader.
    """

    def __init__(self, capture: Any) -> None:
        self._capture = capture
        self._lock = threading.Lock()
        self._stream: Any | None = None
        self._audio: Any | None = None
        self._closed = False

    def capture_utterance(self) -> Any:
        import pyaudio
        import webrtcvad

        with self._lock:
            if self._closed:
                raise RuntimeError("voice capture is stopped")

        audio_factory = getattr(self._capture, "_pyaudio_factory", None) or pyaudio.PyAudio
        vad_factory = getattr(self._capture, "_vad_factory", None) or webrtcvad.Vad
        audio = audio_factory()
        vad = vad_factory(self._capture.vad_mode)
        stream = None
        try:
            stream = audio.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self._capture.sample_rate,
                input=True,
                input_device_index=self._capture.input_device_index,
                frames_per_buffer=self._capture.samples_per_frame,
            )
            with self._lock:
                stopped = self._closed
                if not stopped:
                    self._stream = stream
                    self._audio = audio
            if stopped:
                raise RuntimeError("voice capture is stopped")
            return self._capture._read_segment(stream, vad)
        finally:
            with self._lock:
                if self._stream is stream:
                    self._stream = None
                if self._audio is audio:
                    self._audio = None
            self._shutdown(stream, audio)

    def stop(self) -> None:
        with self._lock:
            self._closed = True
            stream = self._stream
            audio = self._audio
        self._shutdown(stream, audio)

    def close(self) -> None:
        self.stop()

    @staticmethod
    def _shutdown(stream: Any | None, audio: Any | None) -> None:
        if stream is not None:
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        if audio is not None:
            try:
                audio.terminate()
            except Exception:
                pass


@dataclass
class VoiceRuntime:
    """Resources owned by one service run.

    ``resources`` is the runtime-owned bundle returned beside ``VoiceEngine``.  The
    service treats it as opaque and calls its single ``close()`` method on release.
    Additional ``closeables`` remain available for injected test/runtime adapters.
    """

    engine: Any
    capture: Any | None = None
    test_engine: Any | None = None
    resources: Any | None = None
    closeables: tuple[Any, ...] = ()
    _release_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _released: bool = field(default=False, init=False, repr=False)
    _preclosed: set[int] = field(default_factory=set, init=False, repr=False)


class VoiceEventHub:
    """A thread-to-async fan-out bus that emits only ``voice.*`` envelopes."""

    def __init__(self, *, queue_size: int = 128) -> None:
        self._queue_size = max(1, int(queue_size))
        self._lock = threading.Lock()
        self._seq = 0
        self._next_subscriber = 0
        self._subscribers: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]] = {}

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        event = self.envelope(event_type, payload)
        with self._lock:
            subscribers = list(self._subscribers.items())
        stale: list[int] = []
        for subscriber_id, (loop, queue) in subscribers:
            try:
                loop.call_soon_threadsafe(self._offer, queue, event)
            except RuntimeError:
                stale.append(subscriber_id)
        for subscriber_id in stale:
            self.unsubscribe(subscriber_id)

    def envelope(self, event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not event_type.startswith("voice."):
            raise ValueError("voice event type must use the voice.* namespace")
        with self._lock:
            self._seq += 1
            seq = self._seq
        return {
            "type": event_type,
            "seq": seq,
            "ts": time.time(),
            "payload": _jsonable(dict(payload)),
        }

    def subscribe(self) -> tuple[int, asyncio.Queue[dict[str, Any]]]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        with self._lock:
            self._next_subscriber += 1
            subscriber_id = self._next_subscriber
            self._subscribers[subscriber_id] = (loop, queue)
        return subscriber_id, queue

    def unsubscribe(self, subscriber_id: int) -> None:
        with self._lock:
            self._subscribers.pop(subscriber_id, None)

    @staticmethod
    def _offer(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


class VoiceService:
    """Own the microphone worker, dry-run commands, and SSE subscribers.

    Tests and future integrations can inject a concrete engine/capture, individual
    factories, or a runtime factory.  A runtime factory may return ``VoiceRuntime``,
    ``(engine, capture)``, a mapping with those keys, or an object exposing matching
    attributes.  Factories can optionally accept ``events`` and ``dry_run`` keyword
    arguments.
    """

    def __init__(
        self,
        *,
        permission: Any | None = None,
        engine: Any | None = None,
        capture: Any | None = None,
        engine_factory: Callable[..., Any] | None = None,
        capture_factory: Callable[..., Any] | None = None,
        runtime_factory: Callable[..., Any] | None = None,
        test_engine_factory: Callable[..., Any] | None = None,
        event_hub: VoiceEventHub | None = None,
        stop_timeout: float = 2.0,
    ) -> None:
        self.permission = permission or ExistingVoicePermission()
        self.events = event_hub or VoiceEventHub()
        self._injected_engine = engine
        self._injected_capture = capture
        self._engine_factory = engine_factory
        self._capture_factory = capture_factory
        self._runtime_factory = runtime_factory
        self._test_engine_factory = test_engine_factory
        self._stop_timeout = max(0.0, float(stop_timeout))

        self._lock = threading.RLock()
        self._test_lock = threading.Lock()
        self._state = "stopped"
        self._generation = 0
        self._runtime: VoiceRuntime | None = None
        self._worker: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._started_at: float | None = None
        self._last_error: dict[str, Any] | None = None

    def status(self) -> dict[str, Any]:
        authorized = self._allowed()
        with self._lock:
            return self._status_locked(authorized=authorized)

    def start(self) -> dict[str, Any]:
        """Start one microphone loop; concurrent calls construct it at most once."""

        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                result = self._status_locked(authorized=self._allowed())
                result.update({"changed": False, "idempotent": True})
                return result
            if self._state in {"starting", "running", "stopping"}:
                result = self._status_locked(authorized=self._allowed())
                result.update({"changed": False, "idempotent": True})
                return result
            if not self._allowed():
                self._state = "denied"
                self._last_error = {
                    "code": "permission_denied",
                    "detail": "voice_control is not granted",
                }
                self.events.emit("voice.error", self._last_error)
                raise VoicePermissionDenied("voice_control is not granted")

            self._state = "starting"
            self._last_error = None
            self._generation += 1
            generation = self._generation
            stop_event = threading.Event()
            self._stop_event = stop_event
            self.events.emit("voice.state", {"state": "starting", "generation": generation})

            try:
                runtime = self._create_runtime(dry_run=False)
                if runtime.capture is None:
                    raise RuntimeError("voice runtime did not provide an audio capture")
                self._runtime = runtime
                worker = threading.Thread(
                    target=self._worker_main,
                    args=(generation, runtime, stop_event),
                    name=f"muliao-voice-{generation}",
                    daemon=True,
                )
                self._worker = worker
                self._started_at = time.time()
                worker.start()
            except Exception as exc:
                runtime = self._runtime
                self._runtime = None
                self._worker = None
                self._stop_event = None
                self._state = "error"
                self._set_last_error("start_failed", exc)
                if runtime is not None:
                    self._release_runtime(runtime)
                self.events.emit("voice.error", dict(self._last_error or {}))
                raise

            result = self._status_locked(authorized=True)
            result.update({"changed": True, "idempotent": False})
            return result

    def stop(self) -> dict[str, Any]:
        """Cancel current work, interrupt capture when supported, then release it."""

        with self._lock:
            worker = self._worker
            runtime = self._runtime
            stop_event = self._stop_event
            active = bool(worker is not None and worker.is_alive())
            if not active and runtime is None:
                self._state = "stopped"
                result = self._status_locked(authorized=self._allowed())
                result.update({"changed": False, "idempotent": True})
                return result
            self._state = "stopping"
            if stop_event is not None:
                stop_event.set()
            generation = self._generation

        self.events.emit("voice.state", {"state": "stopping", "generation": generation})
        if runtime is not None:
            self._cancel_engine(runtime.engine)
            self._interrupt_capture(runtime)

        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=self._stop_timeout)

        with self._lock:
            still_running = bool(self._worker is not None and self._worker.is_alive())
            if not still_running and self._runtime is None:
                self._state = "stopped"
            result = self._status_locked(authorized=self._allowed())
            result.update({"changed": True, "idempotent": False})
            return result

    def test_command(self, text: str) -> dict[str, Any]:
        """Route a text command through a dry-run engine without opening the mic."""

        command = str(text).strip()
        if not command:
            raise ValueError("command text must not be empty")
        if not self._allowed():
            error = {"code": "permission_denied", "detail": "voice_control is not granted"}
            with self._lock:
                self._last_error = error
            self.events.emit("voice.error", error)
            raise VoicePermissionDenied(error["detail"])

        transient: VoiceRuntime | None = None
        with self._test_lock:
            with self._lock:
                active_runtime = self._runtime
            if active_runtime is not None and active_runtime.test_engine is not None:
                engine = active_runtime.test_engine
            elif self._test_engine_factory is not None:
                value = _call_factory(
                    self._test_engine_factory,
                    events=self.events,
                    dry_run=True,
                )
                transient = _normalize_runtime(value)
                engine = transient.test_engine or transient.engine
            elif self._injected_engine is not None and self._runtime_factory is None:
                # Injected engines are test doubles or caller-owned dry-run engines.
                engine = self._injected_engine
                self._attach_events(engine)
            else:
                transient = self._create_runtime(dry_run=True)
                engine = transient.test_engine or transient.engine

            try:
                process = getattr(engine, "process_transcript", None)
                if not callable(process):
                    raise TypeError("voice engine must implement process_transcript(text)")
                result = process(command)
                result_data = _jsonable(result)
                status = result_data.get("status") if isinstance(result_data, dict) else None
                self.events.emit(
                    "voice.result",
                    {"source": "test", "dry_run": True, "status": status, "result": result_data},
                )
                return {"ok": True, "dry_run": True, "result": result_data}
            except VoicePermissionDenied:
                raise
            except Exception as exc:
                self._set_last_error("test_failed", exc)
                self.events.emit("voice.error", dict(self._last_error or {}))
                raise
            finally:
                if transient is not None:
                    self._release_runtime(transient)

    async def stream_events(
        self,
        is_disconnected: Callable[[], Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield an initial status and future events; cleanup is in ``finally``."""

        subscriber_id, queue = self.events.subscribe()
        try:
            yield self.events.envelope("voice.status", self.status())
            while True:
                if is_disconnected is not None:
                    disconnected = is_disconnected()
                    if inspect.isawaitable(disconnected):
                        disconnected = await disconnected
                    if disconnected:
                        break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.25)
                except TimeoutError:
                    continue
                if not str(event.get("type", "")).startswith("voice."):
                    continue
                yield event
        finally:
            self.events.unsubscribe(subscriber_id)

    def _worker_main(
        self,
        generation: int,
        runtime: VoiceRuntime,
        stop_event: threading.Event,
    ) -> None:
        with self._lock:
            if generation != self._generation or stop_event.is_set():
                should_run = False
            else:
                self._state = "running"
                should_run = True
        if should_run:
            self.events.emit("voice.state", {"state": "running", "generation": generation})

        try:
            while should_run and not stop_event.is_set():
                if not self._allowed():
                    self._set_last_error(
                        "permission_revoked",
                        RuntimeError("voice_control was revoked"),
                    )
                    self.events.emit("voice.error", dict(self._last_error or {}))
                    break
                try:
                    result = self._run_once(runtime, stop_event)
                except TimeoutError:
                    if stop_event.is_set():
                        break
                    self.events.emit("voice.metric", {"name": "capture_timeout", "value": 1})
                    continue
                except Exception as exc:
                    if stop_event.is_set():
                        break
                    self._set_last_error("runtime_failed", exc)
                    self.events.emit("voice.error", dict(self._last_error or {}))
                    break

                if result is None or stop_event.is_set():
                    continue
                result_data = _jsonable(result)
                result_status = result_data.get("status") if isinstance(result_data, dict) else None
                self.events.emit(
                    "voice.result",
                    {"source": "microphone", "status": result_status, "result": result_data},
                )
                if result_status == "permission_denied":
                    break
        finally:
            self._release_runtime(runtime)
            with self._lock:
                if self._runtime is runtime:
                    self._runtime = None
                if self._worker is threading.current_thread():
                    self._worker = None
                if self._stop_event is stop_event:
                    self._stop_event = None
                self._started_at = None
                self._state = "stopped"
            self.events.emit("voice.state", {"state": "stopped", "generation": generation})

    def _run_once(self, runtime: VoiceRuntime, stop_event: threading.Event) -> Any | None:
        engine = runtime.engine
        capture = runtime.capture
        process_audio = getattr(engine, "process_audio", None)
        capture_once = getattr(capture, "capture_utterance", None)
        if callable(process_audio) and callable(capture_once):
            self.events.emit("voice.state", {"state": "listening"})
            audio = capture_once()
            if stop_event.is_set():
                return None
            return process_audio(audio)
        run_once = getattr(engine, "run_once", None)
        if callable(run_once):
            return run_once(capture)
        raise TypeError("voice engine must implement process_audio(audio) or run_once(capture)")

    def _create_runtime(self, *, dry_run: bool) -> VoiceRuntime:
        if self._runtime_factory is not None:
            value = _call_factory(
                self._runtime_factory,
                events=self.events,
                dry_run=dry_run,
            )
            runtime = _normalize_runtime(value)
        elif self._injected_engine is not None or self._engine_factory is not None:
            runtime_resources = None
            closeables: list[Any] = []
            if self._injected_engine is not None:
                engine = self._injected_engine
            else:
                value = _call_factory(
                    self._engine_factory,
                    events=self.events,
                    dry_run=dry_run,
                )
                if isinstance(value, tuple) and value:
                    engine = value[0]
                    runtime_resources = value[1] if len(value) > 1 else None
                    closeables.extend(value[2:])
                else:
                    engine = value
            if dry_run:
                capture = None
            elif self._injected_capture is not None:
                capture = self._injected_capture
            elif self._capture_factory is not None:
                capture = _call_factory(
                    self._capture_factory,
                    events=self.events,
                    dry_run=False,
                )
            else:
                capture = None
            runtime = VoiceRuntime(
                engine=engine,
                capture=capture,
                resources=runtime_resources,
                closeables=tuple(closeables),
            )
        else:
            runtime = _default_runtime_factory(events=self.events, dry_run=dry_run)

        self._attach_events(runtime.engine)
        if runtime.test_engine is not None:
            self._attach_events(runtime.test_engine)
        return runtime

    def _attach_events(self, engine: Any) -> None:
        if engine is None:
            return
        try:
            setattr(engine, "events", self.events)
        except (AttributeError, TypeError):
            pass

    def _interrupt_capture(self, runtime: VoiceRuntime) -> None:
        capture = runtime.capture
        if capture is None:
            return
        stop = getattr(capture, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception as exc:
                self.events.emit(
                    "voice.error",
                    {"code": "capture_stop_failed", "detail": f"{type(exc).__name__}: {exc}"},
                )
            return
        close = getattr(capture, "close", None)
        if callable(close):
            try:
                close()
                runtime._preclosed.add(id(capture))
            except Exception as exc:
                self.events.emit(
                    "voice.error",
                    {"code": "capture_stop_failed", "detail": f"{type(exc).__name__}: {exc}"},
                )

    @staticmethod
    def _cancel_engine(engine: Any) -> None:
        cancel = getattr(engine, "cancel_current", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                pass

    def _release_runtime(self, runtime: VoiceRuntime) -> None:
        with runtime._release_lock:
            if runtime._released:
                return
            runtime._released = True
        resources = [
            runtime.test_engine,
            runtime.capture,
            runtime.engine,
            runtime.resources,
            *runtime.closeables,
        ]
        seen: set[int] = set()
        for resource in resources:
            if resource is None or id(resource) in seen:
                continue
            seen.add(id(resource))
            if id(resource) in runtime._preclosed:
                continue
            close = getattr(resource, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception as exc:
                self.events.emit(
                    "voice.error",
                    {"code": "release_failed", "detail": f"{type(exc).__name__}: {exc}"},
                )

    def _allowed(self) -> bool:
        try:
            return bool(self.permission.allowed())
        except Exception as exc:
            self._set_last_error("permission_check_failed", exc)
            return False

    def _set_last_error(self, code: str, exc: Exception) -> None:
        error = {"code": code, "detail": f"{type(exc).__name__}: {exc}"}
        with self._lock:
            self._last_error = error

    def _status_locked(self, *, authorized: bool) -> dict[str, Any]:
        worker_alive = bool(self._worker is not None and self._worker.is_alive())
        return {
            "state": self._state,
            "running": worker_alive and self._state in {"starting", "running", "stopping"},
            "authorized": authorized,
            "generation": self._generation,
            "started_at": self._started_at,
            "last_error": _jsonable(self._last_error),
            "subscribers": self.events.subscriber_count,
        }


def _call_factory(factory: Callable[..., Any] | None, *, events: VoiceEventHub, dry_run: bool) -> Any:
    if factory is None:
        raise RuntimeError("voice dependency factory is not configured")
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return factory()

    parameters = signature.parameters
    kwargs: dict[str, Any] = {}
    accepts_kwargs = any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values())
    if "events" in parameters or accepts_kwargs:
        kwargs["events"] = events
    if "dry_run" in parameters or accepts_kwargs:
        kwargs["dry_run"] = dry_run
    if kwargs:
        return factory(**kwargs)

    required_positional = [
        item
        for item in parameters.values()
        if item.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        and item.default is inspect.Parameter.empty
    ]
    if len(required_positional) == 1:
        return factory(events)
    return factory()


def _normalize_runtime(value: Any) -> VoiceRuntime:
    if isinstance(value, VoiceRuntime):
        return value
    if isinstance(value, Mapping):
        return VoiceRuntime(
            engine=value.get("engine"),
            capture=value.get("capture"),
            test_engine=value.get("test_engine"),
            resources=value.get("resources"),
            closeables=tuple(value.get("closeables") or ()),
        )
    if isinstance(value, tuple):
        if not value:
            raise TypeError("runtime factory returned an empty tuple")
        return VoiceRuntime(
            engine=value[0],
            capture=value[1] if len(value) > 1 else None,
            closeables=tuple(value[2:]),
        )
    if hasattr(value, "engine"):
        return VoiceRuntime(
            engine=getattr(value, "engine"),
            capture=getattr(value, "capture", None),
            test_engine=getattr(value, "test_engine", None),
            resources=getattr(value, "resources", None),
            closeables=tuple(getattr(value, "closeables", ()) or ()),
        )
    return VoiceRuntime(engine=value)


def _default_runtime_factory(*, events: VoiceEventHub, dry_run: bool = False) -> VoiceRuntime:
    """Import and construct heavy dependencies only after permission succeeds."""

    from .config import VoiceSettings
    from .runtime import build_capture, build_runtime

    settings = VoiceSettings.load()
    engine, resources = build_runtime(
        settings,
        mode="fast",
        act=not dry_run,
        speak=not dry_run,
    )
    engine.events = events
    capture = None if dry_run else _InterruptibleCapture(build_capture(settings))
    return VoiceRuntime(engine=engine, capture=capture, resources=resources)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return str(value)
