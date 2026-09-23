"""Windows 本地播报与云端失败降级组合。"""
from __future__ import annotations

from dataclasses import dataclass, field
import queue
import threading
from typing import Any, Callable


class NullSpeaker:
    enabled = False

    def speak(
        self,
        text: str,
        *,
        cancellation: Any | None = None,
        operation_id: int | str | None = None,
    ) -> None:
        return None

    def stop(self) -> None:
        return None


class FallbackSpeaker:
    """优先云端播报，任一失败立即改用本地 Speaker。"""

    def __init__(self, cloud: Any, local: Any) -> None:
        self.cloud = cloud
        self.local = local
        self.last_backend = "none"
        self.last_error = ""
        self._lock = threading.RLock()
        self._generation = 0
        self._operation_id: int | str | None = None
        self._closed = False

    def speak(
        self,
        text: str,
        *,
        cancellation: Any | None = None,
        operation_id: int | str | None = None,
    ) -> None:
        if cancellation is not None and cancellation.cancelled:
            return
        # Each call owns an epoch.  stop()/close() advance it before cancelling,
        # so a cancellation exception from cloud can never resurrect local audio.
        with self._lock:
            if self._closed or (cancellation is not None and cancellation.cancelled):
                return
            if operation_id is not None:
                current_operation = self._operation_id
                if (
                    current_operation is not None
                    and isinstance(operation_id, int)
                    and isinstance(current_operation, int)
                    and operation_id < current_operation
                ):
                    return
                self._operation_id = operation_id
            self._generation += 1
            generation = self._generation
        if cancellation is not None and cancellation.cancelled:
            return
        try:
            if cancellation is None:
                self.cloud.speak(text)
            else:
                try:
                    self.cloud.speak(text, cancellation=cancellation)
                except TypeError as exc:
                    # Preserve older cloud adapters while operation-aware clients
                    # receive the exact token supplied by VoiceEngine.
                    if "cancellation" not in str(exc):
                        raise
                    self.cloud.speak(text)
        except Exception as exc:
            self._fallback(text, generation, exc, cancellation, operation_id)
            return
        with self._lock:
            if (
                not self._closed
                and generation == self._generation
                and (cancellation is None or not cancellation.cancelled)
            ):
                self.last_backend = "mimo"
                self.last_error = ""

    def _fallback(
        self,
        text: str,
        generation: int,
        exc: Exception,
        cancellation: Any | None,
        operation_id: int | str | None,
    ) -> None:
        with self._lock:
            if (
                self._closed
                or generation != self._generation
                or (cancellation is not None and cancellation.cancelled)
            ):
                return
            self.last_backend = "local"
            self.last_error = f"{type(exc).__name__}: {exc}"
        # Speak outside the epoch lock.  The local speaker waits for real
        # playback, so holding the lock here would block stop() from reaching
        # local_stop() and let audio outlive an emergency stop.  The operation
        # token is re-checked by the local speaker at entry and during playback,
        # so a cancellation landing after this point still prevents new audio.
        if cancellation is not None and cancellation.cancelled:
            return
        try:
            self.local.speak(
                text,
                cancellation=cancellation,
                operation_id=operation_id,
            )
        except TypeError as local_exc:
            if (
                "cancellation" not in str(local_exc)
                and "operation_id" not in str(local_exc)
            ):
                raise
            self.local.speak(text)

    def stop(self, *, operation_id: int | str | None = None) -> bool:
        with self._lock:
            if self._closed:
                return False
            if (
                operation_id is not None
                and self._operation_id is not None
                and operation_id != self._operation_id
            ):
                return False
            self._generation += 1
            self._operation_id = None
        cancel = getattr(self.cloud, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                pass
        local_stop = getattr(self.local, "stop", None)
        if callable(local_stop):
            try:
                local_stop(operation_id=operation_id)
            except TypeError as exc:
                if "unexpected keyword" not in str(exc):
                    raise
                local_stop()
        return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
        close = getattr(self.cloud, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        local_close = getattr(self.local, "close", None)
        if callable(local_close):
            local_close()


@dataclass
class _SapiCommand:
    kind: str
    text: str = ""
    cancellation: Any | None = None
    operation_id: int | str | None = None
    done: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None


class SapiSpeaker:
    """Own SAPI and COM on one thread; speak/stop are queued thread-safe commands."""

    _ASYNC = 1
    _PURGE_BEFORE_SPEAK = 2

    def __init__(
        self,
        dispatch: Callable[[str], Any] | None = None,
        *,
        com_initialize: Callable[[], Any] | None = None,
        com_uninitialize: Callable[[], Any] | None = None,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        self._injected_dispatch = dispatch is not None
        if dispatch is None:
            import pythoncom
            import win32com.client

            dispatch = win32com.client.Dispatch
            com_initialize = com_initialize or pythoncom.CoInitialize
            com_uninitialize = com_uninitialize or pythoncom.CoUninitialize
        self._dispatch = dispatch
        self._com_initialize = com_initialize
        self._com_uninitialize = com_uninitialize
        self._commands: queue.Queue[_SapiCommand] = queue.Queue()
        self._state_lock = threading.RLock()
        self._closed = False
        self._operation_id: int | str | None = None
        self._startup = _SapiCommand("startup")
        self._thread = thread_factory(target=self._run, name="voice-sapi-owner", daemon=True)
        self._thread.start()
        self._startup.done.wait()
        if self._startup.error is not None:
            with self._state_lock:
                self._closed = True
            self._thread.join()
            raise self._startup.error

    def speak(
        self,
        text: str,
        *,
        cancellation: Any | None = None,
        operation_id: int | str | None = None,
    ) -> None:
        if cancellation is not None and cancellation.cancelled:
            return
        with self._state_lock:
            if self._closed:
                raise RuntimeError("SAPI speaker is closed")
            if (
                operation_id is not None
                and self._operation_id is not None
                and isinstance(operation_id, int)
                and isinstance(self._operation_id, int)
                and operation_id < self._operation_id
            ):
                return
            if operation_id is not None:
                self._operation_id = operation_id
        self._submit(
            _SapiCommand(
                "speak",
                str(text),
                cancellation=cancellation,
                operation_id=operation_id,
            )
        )

    def stop(self, *, operation_id: int | str | None = None) -> bool:
        with self._state_lock:
            if self._closed:
                return False
            if (
                operation_id is not None
                and self._operation_id is not None
                and operation_id != self._operation_id
            ):
                return False
            self._operation_id = None
        self._submit(_SapiCommand("stop", operation_id=operation_id), ignore_closed=True)
        return True

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            command = _SapiCommand("close")
            self._commands.put(command)
        command.done.wait()
        self._thread.join()
        if command.error is not None:
            raise command.error

    def _submit(self, command: _SapiCommand, *, ignore_closed: bool = False) -> None:
        with self._state_lock:
            if self._closed:
                if ignore_closed:
                    return
                raise RuntimeError("SAPI speaker is closed")
            self._commands.put(command)
        command.done.wait()
        if command.error is not None:
            raise command.error

    def _wait_for_speech(
        self,
        voice: Any,
        current: _SapiCommand,
        wait_until_done: Callable[[int], Any],
    ) -> bool:
        """Wait for playback while pumping stop/close on the COM owner thread.

        Returns True when a close command was consumed and the owner loop must exit.
        Deferred speak commands are requeued after the current playback ends or is purged.
        """

        deferred: list[_SapiCommand] = []
        close_requested = False
        cancel_deferred = False
        try:
            while True:
                if current.cancellation is not None and current.cancellation.cancelled:
                    cancel_deferred = True
                    voice.Speak("", self._ASYNC | self._PURGE_BEFORE_SPEAK)
                    break
                if bool(wait_until_done(50)):
                    break
                try:
                    control = self._commands.get_nowait()
                except queue.Empty:
                    continue
                if control.kind == "speak":
                    deferred.append(control)
                    continue
                try:
                    if control.kind == "stop":
                        cancel_deferred = True
                        voice.Speak("", self._ASYNC | self._PURGE_BEFORE_SPEAK)
                        break
                    if control.kind == "close":
                        close_requested = True
                        cancel_deferred = True
                        voice.Speak("", self._ASYNC | self._PURGE_BEFORE_SPEAK)
                        break
                except BaseException as exc:
                    control.error = exc
                    cancel_deferred = True
                    if control.kind == "close":
                        close_requested = True
                    break
                finally:
                    control.done.set()
                    self._commands.task_done()
        finally:
            with self._state_lock:
                closed = self._closed
                if not cancel_deferred and not closed:
                    for command in deferred:
                        self._commands.put(command)
                        self._commands.task_done()
                    deferred.clear()

            if deferred:
                detail = (
                    "SAPI speaker is closed"
                    if close_requested or closed
                    else "SAPI speech was stopped"
                )
                for command in deferred:
                    command.error = RuntimeError(detail)
                    command.done.set()
                    self._commands.task_done()
        return close_requested

    def _run(self) -> None:
        voice = None
        initialized = False
        try:
            if self._com_initialize is not None:
                self._com_initialize()
                initialized = True
            voice = self._dispatch("SAPI.SpVoice")
            self._startup.done.set()
            while True:
                command = self._commands.get()
                try:
                    if command.kind == "speak":
                        if (
                            command.cancellation is None
                            or not command.cancellation.cancelled
                        ):
                            if self._injected_dispatch:
                                try:
                                    voice.Speak(command.text, self._ASYNC)
                                except TypeError:
                                    # Historical injected test doubles often expose
                                    # Speak(text) only; real SAPI always receives flags.
                                    voice.Speak(command.text)
                                wait_until_done = getattr(voice, "WaitUntilDone", None)
                                if callable(wait_until_done) and self._wait_for_speech(
                                    voice,
                                    command,
                                    wait_until_done,
                                ):
                                    return
                            else:
                                voice.Speak(command.text, self._ASYNC)
                                if self._wait_for_speech(
                                    voice,
                                    command,
                                    voice.WaitUntilDone,
                                ):
                                    return
                    elif command.kind == "stop":
                        voice.Speak("", self._ASYNC | self._PURGE_BEFORE_SPEAK)
                    elif command.kind == "close":
                        voice.Speak("", self._ASYNC | self._PURGE_BEFORE_SPEAK)
                except BaseException as exc:
                    command.error = exc
                finally:
                    command.done.set()
                    self._commands.task_done()
                if command.kind == "close":
                    return
        except BaseException as exc:
            # Construction failure is delivered before __init__ returns.  A later
            # owner-thread failure is delivered to every queued caller.
            if not self._startup.done.is_set():
                self._startup.error = exc
                self._startup.done.set()
            while True:
                try:
                    command = self._commands.get_nowait()
                except queue.Empty:
                    break
                command.error = exc
                command.done.set()
                self._commands.task_done()
        finally:
            voice = None
            if initialized and self._com_uninitialize is not None:
                self._com_uninitialize()
