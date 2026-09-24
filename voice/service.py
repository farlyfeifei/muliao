"""Thread-safe lifecycle and event bridge for the standalone voice API.

The service deliberately owns no chat state.  Heavy voice dependencies are created
lazily, only after the ``voice_control`` permission gate succeeds.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field, is_dataclass
import inspect
import os
from pathlib import Path
import threading
import time
from typing import Any, AsyncIterator, Callable, Mapping

from .permission_gate import ExistingVoicePermission


class VoicePermissionDenied(PermissionError):
    """Raised before any microphone, router, or action dependency is started."""


def _accepts_cancellation(callable_obj: Any) -> bool:
    """Return whether ``callable_obj`` accepts a ``cancellation`` keyword.

    Signature detection replaces error-text probing so a compatible capture
    raising an internal ``TypeError`` is never retried (which would open the
    microphone a second time).
    """

    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False
    parameters = signature.parameters
    if "cancellation" in parameters:
        return True
    return any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values())


def _call_capture(capture_utterance: Callable[..., Any], cancellation: Any | None) -> Any:
    if cancellation is not None and _accepts_cancellation(capture_utterance):
        return capture_utterance(cancellation=cancellation)
    return capture_utterance()


class _CaptionedCapture:
    """Preserve native cancellation while adding display-only caption boundaries."""

    def __init__(self, capture: Any, bridge: Any, events: "VoiceEventHub") -> None:
        self._capture = capture
        self._bridge = bridge
        self._events = events
        self._closed = False
        self._lock = threading.Lock()

    def capture_utterance(self, *, cancellation: Any | None = None) -> Any:
        with self._lock:
            if self._closed:
                raise RuntimeError("voice capture is stopped")
        capture = getattr(self._capture, "capture_utterance", None)
        if not callable(capture):
            raise TypeError("voice capture must implement capture_utterance")
        try:
            audio = _call_capture(capture, cancellation)
        except Exception:
            self._caption("reset")
            raise
        if cancellation is not None and bool(getattr(cancellation, "cancelled", False)):
            self._caption("reset")
            raise_if_cancelled = getattr(cancellation, "raise_if_cancelled", None)
            if callable(raise_if_cancelled):
                raise_if_cancelled()
            raise RuntimeError("voice capture cancelled")
        self._caption("finish")
        return audio

    def stop(self) -> None:
        stop = getattr(self._capture, "stop", None)
        try:
            if callable(stop):
                stop()
        finally:
            self._caption("reset")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        close = getattr(self._capture, "close", None)
        stop = getattr(self._capture, "stop", None)
        try:
            if callable(close):
                close()
            elif callable(stop):
                stop()
        finally:
            self._caption("reset")

    def _caption(self, method: str) -> None:
        callback = getattr(self._bridge, method, None)
        if not callable(callback):
            return
        try:
            callback()
        except Exception as exc:
            self._events.emit(
                "voice.error",
                {
                    "code": f"caption_{method}_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                },
            )


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
    caption_bridge: Any | None = None
    emergency_watcher: Any | None = None
    permission_watcher: Any | None = None
    closeables: tuple[Any, ...] = ()
    _release_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _released: bool = field(default=False, init=False, repr=False)
    _preclosed: set[int] = field(default_factory=set, init=False, repr=False)
    _interrupt_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _interrupt_started: bool = field(default=False, init=False, repr=False)
    _interrupt_threads: tuple[threading.Thread, ...] = field(default=(), init=False, repr=False)


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
        emergency_watcher_factory: Callable[[Callable[[], Any]], Any] | None = None,
        permission_watcher_factory: Callable[[Callable[[], bool], Callable[[], Any]], Any]
        | None = None,
        stop_timeout: float = 2.0,
    ) -> None:
        self.permission = ExistingVoicePermission() if permission is None else permission
        self.events = VoiceEventHub() if event_hub is None else event_hub
        self._injected_engine = engine
        self._injected_capture = capture
        self._engine_factory = engine_factory
        self._capture_factory = capture_factory
        self._runtime_factory = runtime_factory
        self._test_engine_factory = test_engine_factory
        self._emergency_watcher_factory = emergency_watcher_factory
        self._permission_watcher_factory = permission_watcher_factory
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
                allowed, runtime = self._permission_call(
                    lambda: self._create_runtime(dry_run=False)
                )
                if not allowed or runtime is None:
                    raise VoicePermissionDenied(
                        "voice_control was revoked during startup"
                    )
                if runtime.capture is None:
                    self._release_runtime(runtime)
                    raise RuntimeError("voice runtime did not provide an audio capture")
                self._runtime = runtime
                permission_watcher = self._create_permission_watcher()
                runtime.permission_watcher = permission_watcher
                emergency_watcher = self._create_emergency_watcher(runtime)
                runtime.emergency_watcher = emergency_watcher
                worker = threading.Thread(
                    target=self._worker_main,
                    args=(generation, runtime, stop_event),
                    name=f"muliao-voice-{generation}",
                    daemon=True,
                )

                def activate_runtime() -> bool:
                    self._worker = worker
                    self._started_at = time.time()
                    permission_watcher.start()
                    if emergency_watcher is not None:
                        emergency_watcher.start()
                    worker.start()
                    return True

                allowed, activated = self._permission_call(activate_runtime)
                if not allowed or not activated:
                    raise VoicePermissionDenied(
                        "voice_control was revoked before microphone start"
                    )
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

    def stop(self, *, wait: bool = True) -> dict[str, Any]:
        """Cancel current work and optionally wait for the worker to release it."""

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
        interrupt_threads: tuple[threading.Thread, ...] = ()
        if runtime is not None:
            self._request_engine_cancel(runtime.engine)
            interrupt_threads = self._start_runtime_interrupt(runtime)

        if wait:
            deadline = time.monotonic() + self._stop_timeout
            for thread in interrupt_threads:
                if thread is threading.current_thread():
                    continue
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if worker is not None and worker is not threading.current_thread():
                worker.join(timeout=max(0.0, deadline - time.monotonic()))

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
            def run_test() -> tuple[Any, Any]:
                nonlocal transient
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
                elif any(
                    item is not None
                    for item in (
                        self._injected_engine,
                        self._engine_factory,
                        self._capture_factory,
                        self._runtime_factory,
                    )
                ):
                    raise RuntimeError(
                        "test_command requires an explicit test_engine_factory when "
                        "execution/runtime dependencies are injected"
                    )
                else:
                    transient = _default_runtime_factory(events=self.events, dry_run=True)
                    engine = transient.test_engine or transient.engine

                process = getattr(engine, "process_transcript", None)
                if not callable(process):
                    raise TypeError("voice engine must implement process_transcript(text)")
                return process(command), engine

            try:
                allowed, outcome = self._permission_call(run_test)
                if not allowed or outcome is None:
                    raise VoicePermissionDenied(
                        "voice_control was revoked before dry-run execution"
                    )
                result, _engine = outcome
                result_data = _jsonable(result)
                status = result_data.get("status") if isinstance(result_data, dict) else None
                result_data = _sanitize_result(status, result_data)
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
                    self._permission_revoked()
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
                if result_status == "capture_timeout":
                    continue
                result_data = _sanitize_result(result_status, result_data)
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
        run_once = getattr(engine, "run_once", None)
        if callable(run_once):
            result = run_once(capture)
            return None if stop_event.is_set() else result
        process_audio = getattr(engine, "process_audio", None)
        capture_once = getattr(capture, "capture_utterance", None)
        if callable(process_audio) and callable(capture_once):
            self.events.emit("voice.state", {"state": "listening"})
            audio = capture_once()
            if stop_event.is_set():
                return None
            return process_audio(audio)
        raise TypeError("voice engine must implement run_once(capture) or process_audio(audio)")

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

    def _create_permission_watcher(self) -> Any:
        factory = self._permission_watcher_factory
        if factory is None:
            from .safety import PermissionRevocationWatcher

            factory = PermissionRevocationWatcher
        return factory(self._allowed, self._permission_revoked)

    def _permission_revoked(self) -> None:
        with self._lock:
            runtime = self._runtime
            stop_event = self._stop_event
            if runtime is None or stop_event is None or stop_event.is_set():
                return
            self._last_error = {
                "code": "permission_revoked",
                "detail": "voice_control was revoked",
            }
        self.events.emit("voice.error", dict(self._last_error))
        self.stop(wait=False)

    def _create_emergency_watcher(self, runtime: VoiceRuntime) -> Any | None:
        del runtime
        factory = self._emergency_watcher_factory
        if factory is None:
            uses_default_runtime = (
                self._runtime_factory is None
                and self._injected_engine is None
                and self._engine_factory is None
            )
            if not uses_default_runtime:
                return None
            from .safety import EmergencyStopWatcher

            factory = EmergencyStopWatcher
        return factory(lambda: self.stop(wait=False))

    def _start_runtime_interrupt(self, runtime: VoiceRuntime) -> tuple[threading.Thread, ...]:
        with runtime._interrupt_lock:
            if runtime._interrupt_started:
                return runtime._interrupt_threads
            runtime._interrupt_started = True
            capture_thread = threading.Thread(
                target=self._interrupt_capture,
                args=(runtime,),
                name=f"voice-capture-stop-{self._generation}",
                daemon=True,
            )
            engine_thread = threading.Thread(
                target=self._interrupt_engine,
                args=(runtime.engine,),
                name=f"voice-engine-stop-{self._generation}",
                daemon=True,
            )
            threads = (capture_thread, engine_thread)
            started: list[threading.Thread] = []
            for thread in threads:
                try:
                    thread.start()
                    started.append(thread)
                except Exception as exc:
                    self.events.emit(
                        "voice.error",
                        {
                            "code": "interrupt_start_failed",
                            "detail": f"{type(exc).__name__}: {exc}",
                        },
                    )
            runtime._interrupt_threads = tuple(started)
            return runtime._interrupt_threads

    @staticmethod
    def _interrupt_engine(engine: Any) -> None:
        if callable(getattr(engine, "request_cancel", None)):
            stop_playback = getattr(engine, "stop_current_playback", None)
            if callable(stop_playback):
                try:
                    stop_playback()
                except Exception:
                    pass
            return
        cancel = getattr(engine, "cancel_current", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                pass

    def _interrupt_capture(self, runtime: VoiceRuntime) -> None:
        capture = runtime.capture
        if capture is None:
            return
        runtime._preclosed.add(id(capture))
        stop = getattr(capture, "stop", None)
        stop_done = threading.Event()

        def request_stop() -> None:
            try:
                if callable(stop):
                    stop()
            except Exception as exc:
                self.events.emit(
                    "voice.error",
                    {"code": "capture_stop_failed", "detail": f"{type(exc).__name__}: {exc}"},
                )
            finally:
                stop_done.set()

        stop_thread: threading.Thread | None = None
        if callable(stop):
            stop_thread = threading.Thread(
                target=request_stop,
                name=f"voice-capture-cooperative-stop-{self._generation}",
                daemon=True,
            )
            stop_thread.start()
            stop_done.wait(timeout=0.05)

        close = getattr(capture, "close", None)
        if callable(close):
            try:
                close()
                runtime._preclosed.add(id(capture))
            except Exception as exc:
                self.events.emit(
                    "voice.error",
                    {"code": "capture_close_failed", "detail": f"{type(exc).__name__}: {exc}"},
                )
        if stop_thread is not None and stop_thread is not threading.current_thread():
            stop_thread.join(timeout=self._stop_timeout)

    @staticmethod
    def _request_engine_cancel(engine: Any) -> None:
        request = getattr(engine, "request_cancel", None)
        if callable(request):
            try:
                request()
            except Exception:
                pass

    def _permission_call(self, callback: Callable[[], Any]) -> tuple[bool, Any | None]:
        runner = getattr(self.permission, "run_if_allowed", None)
        if callable(runner):
            return runner(callback)
        if not self._allowed():
            return False, None
        return True, callback()

    def _release_runtime(self, runtime: VoiceRuntime) -> None:
        with runtime._release_lock:
            if runtime._released:
                return
            runtime._released = True
        seen: set[int] = set()
        for watcher in (runtime.permission_watcher, runtime.emergency_watcher):
            if watcher is None or id(watcher) in seen:
                continue
            seen.add(id(watcher))
            close = getattr(watcher, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    self.events.emit(
                        "voice.error",
                        {"code": "release_failed", "detail": f"{type(exc).__name__}: {exc}"},
                    )
        for thread in runtime._interrupt_threads:
            if thread is not threading.current_thread():
                thread.join(timeout=self._stop_timeout)
        resources = [
            runtime.capture,
            runtime.caption_bridge,
            runtime.test_engine,
            runtime.engine,
            runtime.resources,
            *runtime.closeables,
        ]
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
            caption_bridge=value.get("caption_bridge"),
            emergency_watcher=value.get("emergency_watcher"),
            permission_watcher=value.get("permission_watcher"),
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
            caption_bridge=getattr(value, "caption_bridge", None),
            emergency_watcher=getattr(value, "emergency_watcher", None),
            permission_watcher=getattr(value, "permission_watcher", None),
            closeables=tuple(getattr(value, "closeables", ()) or ()),
        )
    return VoiceRuntime(engine=value)


def _default_runtime_factory(*, events: VoiceEventHub, dry_run: bool = False) -> VoiceRuntime:
    """Construct transcript-only dry runs or a fully preflighted real runtime."""

    from .config import VoiceSettings
    from .runtime import build_capture, build_runtime

    settings = VoiceSettings.load()
    if dry_run:
        engine, resources = build_runtime(
            settings,
            mode="fast",
            act=False,
            speak=False,
            enable_asr=False,
        )
        engine.events = events
        return VoiceRuntime(engine=engine, resources=resources)

    if settings.sample_rate != 16_000:
        raise ValueError("streaming captions require a 16000 Hz capture sample rate")

    from .asr_streaming import StreamingZipformerRecognizer
    from .captions import AsyncCaptionPump, StreamingCaptionBridge
    from .models import (
        ModelValidationError,
        load_model_inventory,
        validate_model_assets,
        warmup_sensevoice,
    )

    manifest_path = Path(__file__).resolve().parents[1] / "voice-models.json"
    manifest_env = dict(os.environ)
    manifest_env["MULIAO_VOICE_SENSEVOICE_DIR"] = str(settings.sensevoice_dir)
    inventory = load_model_inventory(
        manifest_path,
        repository_root=manifest_path.parent,
        env=manifest_env,
    )
    report = validate_model_assets(
        inventory,
        # sensevoice 是命令识别的唯一依据，必须齐备。
        # streaming_zipformer 只服务「实时字幕」展示（asr_streaming 自述
        # presentation-only，命令执行一律用 sensevoice 的 final transcript），
        # 故它缺失不应阻止开麦——下面按可用性决定是否装字幕组件。
        model_names=("sensevoice",),
    )
    report.raise_for_errors()

    engine = None
    resources = None
    streaming_recognizer = None
    bridge = None
    caption_pump = None
    try:
        engine, resources = build_runtime(
            settings,
            mode="fast",
            act=True,
            speak=True,
            enable_asr=True,
        )
        engine.events = events
        warmup = warmup_sensevoice(
            settings.sensevoice_dir,
            recognizer_factory=lambda _path: engine.recognizer._get_recognizer(),
            sample_rate=settings.sample_rate,
        )
        if not warmup.ok:
            raise ModelValidationError(
                "SenseVoice warm-up failed: " + (warmup.error or "unknown error")
            )
        events.emit(
            "voice.metric",
            {"name": "sensevoice_warmup_ms", "value": round(warmup.total_seconds * 1000, 3)},
        )

        streaming_report = validate_model_assets(
            inventory, model_names=("streaming_zipformer",)
        )
        streaming_available = streaming_report.ok
        if streaming_available:
            streaming = inventory.require("streaming_zipformer")
            files = {asset.name: asset.path for asset in streaming.files}
            streaming_recognizer = StreamingZipformerRecognizer(
                tokens=files["tokens.txt"],
                encoder=files["encoder-epoch-99-avg-1.onnx"],
                decoder=files["decoder-epoch-99-avg-1.onnx"],
                joiner=files["joiner-epoch-99-avg-1.onnx"],
            )
            streaming_started = time.perf_counter()
            streaming_recognizer.warmup()
            events.emit(
                "voice.metric",
                {
                    "name": "streaming_zipformer_warmup_ms",
                    "value": round((time.perf_counter() - streaming_started) * 1000, 3),
                },
            )
            bridge = StreamingCaptionBridge(
                streaming_recognizer,
                engine.wake,
                events,
                resources.echo_guard,
            )
            caption_pump = AsyncCaptionPump(bridge)
        else:
            # 实时字幕是可选的展示能力；缺 streaming 模型时跳过，不影响开麦与命令执行。
            events.emit(
                "voice.metric",
                {
                    "name": "streaming_zipformer_unavailable",
                    "value": 1,
                },
            )

        if caption_pump is not None:
            def accept_caption_frame(frame: bytes) -> None:
                caption_pump.accept_pcm(
                    frame,
                    sample_rate=settings.sample_rate,
                    channels=1,
                    sample_width=2,
                )

            raw_capture = build_capture(
                settings,
                echo_guard=resources.echo_guard,
                frame_consumer=accept_caption_frame,
                frame_resetter=caption_pump.reset,
            )
            capture = _CaptionedCapture(raw_capture, caption_pump, events)
        else:
            capture = build_capture(settings, echo_guard=resources.echo_guard)
        return VoiceRuntime(
            engine=engine,
            capture=capture,
            resources=resources,
            caption_bridge=caption_pump,
        )
    except Exception:
        if caption_pump is not None:
            try:
                caption_pump.close()
            except Exception:
                pass
        elif bridge is not None:
            try:
                bridge.close()
            except Exception:
                pass
        elif streaming_recognizer is not None:
            try:
                streaming_recognizer.close()
            except Exception:
                pass
        if resources is not None:
            try:
                resources.close()
            except Exception:
                pass
        raise


def _sanitize_result(status: Any, result: Any) -> Any:
    if status in {"wake_miss", "echo_drop", "capture_timeout"}:
        return {"status": str(status)}
    return result


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
