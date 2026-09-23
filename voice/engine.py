"""语音主循环：operation generation、权限线性化与唯一终态。"""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Any, Callable, Mapping, TypeVar

from .cancellation import CancellationToken, VoiceCancelled
from .compound import CommandSplitter
from .contracts import (
    ActionExecutor,
    ActionResult,
    AudioCapture,
    AudioSegment,
    CommandRouter,
    EventSink,
    PermissionGate,
    Recognizer,
    RouteDecision,
    Speaker,
    Transcript,
    VoiceResult,
)
from .events import NullEventSink
from .session_state import VoiceSession
from .wake import WakeDetector


T = TypeVar("T")


@dataclass
class _Operation:
    operation_id: int
    token: CancellationToken
    terminal: bool = False
    capture: AudioCapture | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class VoiceEngine:
    """独立语音引擎。

    每次入口从采音前就创建 operation。旧 operation 不能发出新事件、刷新短会话、停止新播报或
    提交副作用。真实权限适配器通过 ``run_if_allowed`` 把撤权与 Jev/动作/TTS 启动线性化。
    """

    def __init__(
        self,
        *,
        permission: PermissionGate,
        recognizer: Recognizer,
        router: CommandRouter,
        executor: ActionExecutor,
        speaker: Speaker,
        wake: WakeDetector | None = None,
        session: VoiceSession | None = None,
        splitter: CommandSplitter | None = None,
        events: EventSink | None = None,
        echo_guard: Any | None = None,
    ) -> None:
        self.permission = permission
        self.recognizer = recognizer
        self.router = router
        self.executor = executor
        self.speaker = speaker
        self.wake = wake or WakeDetector()
        self.session = session or VoiceSession()
        self.splitter = splitter or CommandSplitter()
        self.events = events or NullEventSink()
        self.echo_guard = echo_guard
        self._operation_lock = threading.RLock()
        self._next_operation_id = 0
        self._current: _Operation | None = None

    def request_cancel(self) -> bool:
        """Cancel the active token without waiting for hardware teardown."""

        with self._operation_lock:
            operation = self._current
            if operation is None or operation.terminal or operation.token.cancelled:
                return False
            operation.token.cancel()
            operation_id = operation.operation_id
        self.session.close(operation_id)
        return True

    def cancel_current(self) -> bool:
        """Cancel current work and synchronously interrupt capture and playback."""

        with self._operation_lock:
            operation = self._current
            if operation is None or operation.terminal or operation.token.cancelled:
                return False
            operation.token.cancel()
            capture = operation.capture
            operation_id = operation.operation_id
        self._stop_capture(capture)
        self._stop_speaker(operation_id)
        self.session.close(operation_id)
        return True

    def stop_current_playback(self) -> bool:
        """Stop playback for the currently cancelled operation without touching capture."""

        with self._operation_lock:
            operation = self._current
            if operation is None or operation.terminal or not operation.token.cancelled:
                return False
            operation_id = operation.operation_id
        self._stop_speaker(operation_id)
        return True

    def run_once(self, capture: AudioCapture) -> VoiceResult:
        operation = self._begin_operation()
        with self._operation_lock:
            if self._is_current_locked(operation):
                operation.capture = capture
        try:
            if not self.permission.allowed():
                return self._finish(
                    operation,
                    VoiceResult(status="permission_denied", detail="voice_control is not granted"),
                    "permission_denied",
                    {"code": "permission_denied"},
                    event_type="voice.error",
                )
            self._emit(operation, "voice.state", {"state": "listening"})
            if not self.permission.allowed():
                return self._finish(
                    operation,
                    VoiceResult(status="permission_denied", detail="voice_control is not granted"),
                    "permission_denied",
                    {"code": "permission_denied"},
                    event_type="voice.error",
                )
            audio = self._capture(capture, operation.token)
            operation.token.raise_if_cancelled()
            if not self._is_current(operation):
                return self._finish_stale(operation)
            return self._process_audio(operation, audio)
        except VoiceCancelled:
            return self._cancelled(operation)
        except TimeoutError:
            self._emit(operation, "voice.metric", {"name": "capture_timeout", "value": 1})
            return self._finish(
                operation,
                VoiceResult(status="capture_timeout"),
                "sleeping",
            )
        except Exception as exc:
            if operation.token.cancelled:
                return self._cancelled(operation)
            return self._finish(
                operation,
                VoiceResult(status="capture_error", detail=f"{type(exc).__name__}: {exc}"),
                "error",
                {"code": "capture_error", "detail": f"{type(exc).__name__}: {exc}"},
                event_type="voice.error",
            )
        finally:
            with self._operation_lock:
                if operation.capture is capture:
                    operation.capture = None

    def process_audio(self, audio: AudioSegment, *, permission_checked: bool = False) -> VoiceResult:
        operation = self._begin_operation()
        if not permission_checked and not self.permission.allowed():
            return self._finish(
                operation,
                VoiceResult(status="permission_denied", detail="voice_control is not granted"),
                "permission_denied",
                {"code": "permission_denied"},
                event_type="voice.error",
            )
        return self._process_audio(operation, audio)

    def _process_audio(self, operation: _Operation, audio: AudioSegment) -> VoiceResult:
        try:
            operation.token.raise_if_cancelled()
            self._emit(operation, "voice.state", {"state": "recognizing"})
            transcript = self._timed(
                operation, "asr_ms", lambda: self._transcribe(audio, operation.token)
            )
            operation.token.raise_if_cancelled()
        except VoiceCancelled:
            return self._cancelled(operation)
        except Exception as exc:
            if operation.token.cancelled:
                return self._cancelled(operation)
            return self._finish(
                operation,
                VoiceResult(status="recognition_error", detail=f"{type(exc).__name__}: {exc}"),
                "error",
                {"code": "recognition_error", "detail": f"{type(exc).__name__}: {exc}"},
                event_type="voice.error",
            )
        if not self._is_current(operation):
            return self._finish_stale(operation)
        return self._process_transcript(operation, transcript)

    def process_transcript(
        self,
        transcript: Transcript | str,
        *,
        permission_checked: bool = False,
        cancellation: CancellationToken | None = None,
    ) -> VoiceResult:
        operation = self._begin_operation(cancellation)
        if not permission_checked and not self.permission.allowed():
            return self._finish(
                operation,
                VoiceResult(status="permission_denied", detail="voice_control is not granted"),
                "permission_denied",
                {"code": "permission_denied"},
                event_type="voice.error",
            )
        return self._process_transcript(operation, transcript)

    def _process_transcript(self, operation: _Operation, transcript: Transcript | str) -> VoiceResult:
        if not self._is_current(operation):
            return self._finish_stale(operation)
        if operation.token.cancelled:
            return self._cancelled(operation)

        item = transcript if isinstance(transcript, Transcript) else Transcript(text=str(transcript))
        if self.echo_guard is not None and bool(self.echo_guard.should_drop(item.text)):
            self._emit(operation, "voice.metric", {"name": "echo_drop", "value": 1})
            return self._finish(operation, VoiceResult(status="echo_drop"), "sleeping")
        wake = self.wake.detect(item.text)
        if wake is not None:
            command = wake.command
            wake_source = "wake_phrase"
        elif self.session.is_active():
            command = item.text.strip()
            wake_source = "active_session"
        else:
            self._emit(operation, "voice.metric", {"name": "wake_miss", "value": 1})
            return self._finish(
                operation,
                VoiceResult(status="wake_miss"),
                "sleeping",
            )

        allowed, activated = self._allowed_call(
            lambda: self._commit_session(operation, "activate")
            if wake is not None
            else self._commit_session(operation, "claim")
        )
        if not allowed:
            return self._permission_denied(operation, "before routing")
        if not activated or operation.token.cancelled or not self._is_current(operation):
            return self._cancelled(operation, command=command)

        self._emit(
            operation,
            "voice.final",
            {"text": command, "language": item.language, "source": wake_source},
        )
        if not command:
            return self._finish(operation, VoiceResult(status="wake_only"), "wake_detected")

        try:
            parsed = self.splitter.parse(command)
        except Exception as exc:
            return self._finish(
                operation,
                VoiceResult(status="rejected", command=command, detail=f"malformed_compound:{exc}"),
                "rejected",
                {"reason": "malformed_compound"},
            )
        if parsed.malformed:
            return self._finish(
                operation,
                VoiceResult(status="rejected", command=command, detail=f"malformed_compound:{parsed.reason}"),
                "rejected",
                {"reason": "malformed_compound"},
            )
        commands = list(parsed.parts)
        if len(commands) > 1:
            self._emit(operation, "voice.state", {"state": "compound", "steps": len(commands)})

        decisions: list[RouteDecision] = []
        actions: list[ActionResult] = []
        for index, part in enumerate(commands):
            if operation.token.cancelled or not self._is_current(operation):
                return self._cancelled(operation, command, decisions, actions)

            self._emit(operation, "voice.state", {"state": "deciding", "step": index, "steps": len(commands)})
            allowed, decision = self._allowed_call(
                lambda: self._timed(operation, "jev_ms", lambda: self.router.route(part))
            )
            if not allowed:
                return self._permission_denied(operation, "before routing", command, decisions, actions)
            if operation.token.cancelled or not self._is_current(operation):
                return self._cancelled(operation, command, decisions, actions)
            assert isinstance(decision, RouteDecision)
            decisions.append(decision)
            self._emit(
                operation,
                "voice.decision",
                {
                    "step": index,
                    "steps": len(commands),
                    "command": part,
                    "accepted": decision.accepted,
                    "kind": decision.kind,
                    "target": decision.target,
                    "confidence": decision.confidence,
                    "destructive": decision.destructive,
                },
            )
            if decision.destructive:
                self._emit(operation, "voice.confirmation", {"required": True, "command": part, "step": index})
                return self._finish(
                    operation,
                    VoiceResult(
                        status="confirmation_required",
                        transcript=item.text,
                        command=command,
                        decision=decision,
                        decisions=tuple(decisions),
                        actions=tuple(actions),
                    ),
                    "confirmation_required",
                )
            if not decision.accepted:
                return self._finish(
                    operation,
                    VoiceResult(
                        status="rejected",
                        transcript=item.text,
                        command=command,
                        decision=decision,
                        detail=decision.reason,
                        decisions=tuple(decisions),
                        actions=tuple(actions),
                    ),
                    "rejected",
                    {"step": index},
                )

            if decision.kind == "stop":
                if not self._commit_session(operation, "close"):
                    return self._cancelled(operation, command, decisions, actions)
                if not self._is_current(operation):
                    return self._finish_stale(operation)
                self._stop_current_playback()
                action = ActionResult(True, "stop:current", "voice operation stopped")
                actions.append(action)
                self._emit(operation, "voice.action", {"step": index, "steps": len(commands), "ok": True, "action": action.action, "detail": action.detail})
                return self._finish(
                    operation,
                    VoiceResult(
                        status="executed",
                        transcript=item.text,
                        command=command,
                        decision=decision,
                        action=action,
                        decisions=tuple(decisions),
                        actions=tuple(actions),
                    ),
                    "completed",
                    {"steps": len(actions)},
                )

            allowed, action = self._allowed_call(
                lambda: self._timed(operation, "exec_ms", lambda: self.executor.execute(decision))
            )
            if not allowed:
                return self._permission_denied(operation, "before action", command, decisions, actions)
            assert isinstance(action, ActionResult)
            committed = bool(action.metadata.get("committed", True))
            if not committed and (operation.token.cancelled or not self._is_current(operation)):
                return self._cancelled(operation, command, decisions, actions)
            if not committed:
                action = ActionResult(
                    False,
                    action.action,
                    action.detail or "action did not commit",
                    {**dict(action.metadata), "committed": False},
                )
            actions.append(action)
            self._emit(operation, "voice.action", {"step": index, "steps": len(commands), "ok": action.ok, "action": action.action, "detail": action.detail})
            if not action.ok:
                return self._finish(
                    operation,
                    VoiceResult(
                        status="action_failed",
                        transcript=item.text,
                        command=command,
                        decision=decision,
                        action=action,
                        detail=action.detail,
                        decisions=tuple(decisions),
                        actions=tuple(actions),
                    ),
                    "error",
                    {"code": "action_failed", "detail": action.detail, "step": index},
                    event_type="voice.error",
                )
            if operation.token.cancelled or not self._is_current(operation):
                return self._finish_committed_without_tts(
                    operation, item, command, decisions, actions, "tts_skipped_cancelled"
                )

        # 副作用已提交：撤权/取消后仍返回 executed，避免调用方重试副作用。
        if not self.permission.allowed():
            return self._finish_committed_without_tts(
                operation, item, command, decisions, actions, "tts_skipped_permission_revoked"
            )
        allowed, touched = self._allowed_call(lambda: self._commit_session(operation, "touch"))
        if not allowed:
            return self._finish_committed_without_tts(
                operation, item, command, decisions, actions, "tts_skipped_permission_revoked"
            )
        if not touched or operation.token.cancelled or not self._is_current(operation):
            return self._finish_committed_without_tts(
                operation, item, command, decisions, actions, "tts_skipped_cancelled"
            )
        if not bool(getattr(self.speaker, "enabled", True)):
            return self._finish_committed_without_tts(
                operation, item, command, decisions, actions, "tts_skipped_disabled"
            )

        reply = self._completion_reply(actions)

        def speak() -> None:
            operation.token.raise_if_cancelled()
            if not self._is_current(operation):
                raise VoiceCancelled("stale voice operation")
            self._emit(operation, "voice.tts", {"state": "started", "text": reply, "backend": self._speaker_backend("requested")})
            self._timed(operation, "tts_ms", lambda: self._speak(reply, operation))

        try:
            allowed, _ = self._allowed_call(speak)
        except VoiceCancelled:
            return self._finish_committed_without_tts(
                operation, item, command, decisions, actions, "tts_skipped_cancelled"
            )
        except Exception as exc:
            # 动作已提交：播报失败必须仍是 executed，调用方不得据此重试副作用。
            if operation.token.cancelled:
                return self._finish_committed_without_tts(
                    operation, item, command, decisions, actions, "tts_skipped_cancelled"
                )
            self._emit(
                operation,
                "voice.error",
                {"code": "tts_failed", "detail": f"{type(exc).__name__}: {exc}"},
            )
            return self._finish_committed_without_tts(
                operation, item, command, decisions, actions, f"tts_failed:{type(exc).__name__}"
            )
        if not allowed:
            return self._finish_committed_without_tts(
                operation, item, command, decisions, actions, "tts_skipped_permission_revoked"
            )
        if operation.token.cancelled or not self._is_current(operation):
            return self._finish_committed_without_tts(
                operation, item, command, decisions, actions, "tts_skipped_cancelled"
            )
        self._emit(operation, "voice.tts", {"state": "finished", "backend": self._speaker_backend("actual")})
        return self._finish(
            operation,
            VoiceResult(
                status="executed",
                transcript=item.text,
                command=command,
                decision=decisions[-1] if decisions else None,
                action=actions[-1] if actions else None,
                decisions=tuple(decisions),
                actions=tuple(actions),
            ),
            "completed",
            {"steps": len(actions)},
        )

    def _begin_operation(self, token: CancellationToken | None = None) -> _Operation:
        with self._operation_lock:
            previous = self._current
            if previous is not None and not previous.terminal:
                previous.token.cancel()
                previous.terminal = True
                self.events.emit(
                    "voice.state",
                    {"state": "cancelled", "operation_id": previous.operation_id},
                )
            self._next_operation_id += 1
            operation = _Operation(self._next_operation_id, token or CancellationToken())
            self._current = operation
        if previous is not None and previous.terminal:
            self._stop_capture(previous.capture)
            self._stop_speaker(previous.operation_id)
            self.session.close(previous.operation_id)
        return operation

    @staticmethod
    def _stop_capture(capture: AudioCapture | None) -> None:
        stop = getattr(capture, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass

    def _stop_speaker(self, operation_id: int) -> None:
        stop = getattr(self.speaker, "stop", None)
        if not callable(stop):
            return
        try:
            stop(operation_id=operation_id)
        except TypeError as exc:
            if "unexpected keyword" not in str(exc):
                raise
            try:
                stop()
            except Exception:
                pass
        except Exception:
            pass

    def _stop_current_playback(self) -> None:
        """无条件停止当前任何播报，用于显式"停止播报"语音命令。

        与 :meth:`_stop_speaker` 不同：这里不绑定 operation_id，因为发出停止
        命令的是一个新 operation，而正在播放的音频属于上一个 operation；按 id
        过滤会因不匹配而拒绝停止，违背用户意图。
        """

        stop = getattr(self.speaker, "stop", None)
        if not callable(stop):
            return
        try:
            stop()
        except Exception:
            pass

    def _is_current(self, operation: _Operation) -> bool:
        with self._operation_lock:
            return self._is_current_locked(operation)

    def _is_current_locked(self, operation: _Operation) -> bool:
        return self._current is operation and not operation.terminal and not operation.token.cancelled

    def _commit_session(self, operation: _Operation, method: str) -> bool:
        with self._operation_lock:
            if not self._is_current_locked(operation):
                return False
            fn = getattr(self.session, method)
            return bool(fn(operation.operation_id))

    def _allowed_call(self, callback: Callable[[], T]) -> tuple[bool, T | None]:
        runner = getattr(self.permission, "run_if_allowed", None)
        if callable(runner):
            return runner(callback)
        if not self.permission.allowed():
            return False, None
        return True, callback()

    def _timed(self, operation: _Operation, name: str, work: Callable[[], T]) -> T:
        """Run ``work`` and emit a ``voice.metric`` latency sample on success.

        The sample is a privacy-safe scalar duration in milliseconds (no
        transcript, audio, candidate text, or URL). A raised exception
        propagates untouched and records no sample, so the existing error paths
        stay authoritative. Stale/cancelled operations are dropped by ``_emit``.
        """

        started = time.perf_counter()
        result = work()
        self._emit(
            operation,
            "voice.metric",
            {"name": name, "value": round((time.perf_counter() - started) * 1000, 3)},
        )
        return result

    @staticmethod
    def _capture(capture: AudioCapture, token: CancellationToken) -> AudioSegment:
        try:
            return capture.capture_utterance(cancellation=token)
        except TypeError as exc:
            if "cancellation" not in str(exc):
                raise
            return capture.capture_utterance()

    def _transcribe(self, audio: AudioSegment, token: CancellationToken) -> Transcript:
        try:
            return self.recognizer.transcribe(audio, cancellation=token)
        except TypeError as exc:
            if "cancellation" not in str(exc):
                raise
            return self.recognizer.transcribe(audio)

    def _emit(self, operation: _Operation, event_type: str, payload: Mapping[str, Any]) -> bool:
        with self._operation_lock:
            if self._current is not operation or operation.terminal:
                return False
            data = dict(payload)
            data.setdefault("operation_id", operation.operation_id)
            self.events.emit(event_type, data)
            return True

    def _finish(
        self,
        operation: _Operation,
        result: VoiceResult,
        terminal_state: str,
        payload: Mapping[str, Any] | None = None,
        *,
        event_type: str = "voice.state",
    ) -> VoiceResult:
        with self._operation_lock:
            if operation.terminal:
                return result
            operation.terminal = True
            is_current = self._current is operation
            if is_current:
                data = dict(payload or {})
                if event_type == "voice.state":
                    data.setdefault("state", terminal_state)
                data.setdefault("operation_id", operation.operation_id)
                self.events.emit(event_type, data)
                self._current = None
                operation.capture = None
            return result

    def _finish_stale(self, operation: _Operation) -> VoiceResult:
        operation.token.cancel()
        return self._finish(operation, VoiceResult(status="cancelled"), "cancelled")

    def _cancelled(
        self,
        operation: _Operation,
        command: str = "",
        decisions: list[RouteDecision] | None = None,
        actions: list[ActionResult] | None = None,
    ) -> VoiceResult:
        self.session.close(operation.operation_id)
        return self._finish(
            operation,
            VoiceResult(
                status="cancelled",
                command=command,
                decisions=tuple(decisions or ()),
                actions=tuple(actions or ()),
            ),
            "cancelled",
        )

    def _permission_denied(
        self,
        operation: _Operation,
        stage: str,
        command: str = "",
        decisions: list[RouteDecision] | None = None,
        actions: list[ActionResult] | None = None,
    ) -> VoiceResult:
        self.session.close(operation.operation_id)
        return self._finish(
            operation,
            VoiceResult(
                status="permission_denied",
                command=command,
                decisions=tuple(decisions or ()),
                actions=tuple(actions or ()),
                detail=f"voice_control was revoked {stage}",
            ),
            "permission_denied",
            {"code": "permission_revoked", "stage": stage},
            event_type="voice.error",
        )

    def _finish_committed_without_tts(
        self,
        operation: _Operation,
        item: Transcript,
        command: str,
        decisions: list[RouteDecision],
        actions: list[ActionResult],
        detail: str,
    ) -> VoiceResult:
        self.session.close(operation.operation_id)
        return self._finish(
            operation,
            VoiceResult(
                status="executed",
                transcript=item.text,
                command=command,
                decision=decisions[-1] if decisions else None,
                action=actions[-1] if actions else None,
                detail=detail,
                decisions=tuple(decisions),
                actions=tuple(actions),
            ),
            "completed",
            {"steps": len(actions), "tts": detail.removeprefix("tts_skipped_")},
        )

    def _speak(self, text: str, operation: _Operation) -> None:
        try:
            self.speaker.speak(
                text,
                cancellation=operation.token,
                operation_id=operation.operation_id,
            )
        except TypeError as exc:
            if "unexpected keyword" not in str(exc):
                raise
            self.speaker.speak(text)

    def _speaker_backend(self, mode: str) -> str:
        if mode == "actual":
            return str(getattr(self.speaker, "last_backend", "local") or "local")
        return "mimo_or_local" if hasattr(self.speaker, "last_backend") else "local"

    @staticmethod
    def _completion_reply(actions: list[ActionResult]) -> str:
        if len(actions) == 1 and actions[0].action == "open_app:notepad":
            return "好的，记事本打开了。"
        return "好的，已经完成了。"
