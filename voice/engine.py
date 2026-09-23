"""语音主循环：权限 → 本地 ASR/唤醒 → Jev → 白名单动作 → TTS。"""
from __future__ import annotations

import threading
from typing import Any, Mapping

from .cancellation import CancellationToken
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


class VoiceEngine:
    """可注入依赖的独立语音引擎。

    安全顺序不可调整：权限检查发生在采音之前；Jev 路由发生在唤醒成功或短会话仍有效之后；
    每个动作执行前再次检查权限。未唤醒且会话未激活时，外部调用严格为零。
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
        self._operation_lock = threading.Lock()
        self._current_cancel: CancellationToken | None = None

    def cancel_current(self) -> None:
        with self._operation_lock:
            token = self._current_cancel
        if token is not None:
            token.cancel()
        stop = getattr(self.speaker, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass
        self._emit("voice.state", {"state": "cancelled"})

    def run_once(self, capture: AudioCapture) -> VoiceResult:
        if not self.permission.allowed():
            self._emit("voice.error", {"code": "permission_denied"})
            return VoiceResult(status="permission_denied", detail="voice_control is not granted")
        self._emit("voice.state", {"state": "listening"})
        audio = capture.capture_utterance()
        return self.process_audio(audio, permission_checked=True)

    def process_audio(self, audio: AudioSegment, *, permission_checked: bool = False) -> VoiceResult:
        if not permission_checked and not self.permission.allowed():
            self._emit("voice.error", {"code": "permission_denied"})
            return VoiceResult(status="permission_denied", detail="voice_control is not granted")
        self._emit("voice.state", {"state": "recognizing"})
        transcript = self.recognizer.transcribe(audio)
        return self.process_transcript(transcript, permission_checked=True)

    def process_transcript(
        self,
        transcript: Transcript | str,
        *,
        permission_checked: bool = False,
        cancellation: CancellationToken | None = None,
    ) -> VoiceResult:
        token = cancellation or CancellationToken()
        previous = self._set_current_operation(token)
        if previous is not None and previous is not token:
            previous.cancel()
        try:
            return self._process_transcript(
                transcript,
                permission_checked=permission_checked,
                cancellation=token,
            )
        finally:
            self._clear_current_operation(token)

    def _process_transcript(
        self,
        transcript: Transcript | str,
        *,
        permission_checked: bool,
        cancellation: CancellationToken,
    ) -> VoiceResult:
        if not permission_checked and not self.permission.allowed():
            self._emit("voice.error", {"code": "permission_denied"})
            return VoiceResult(status="permission_denied", detail="voice_control is not granted")
        if cancellation.cancelled:
            return self._cancelled()

        item = transcript if isinstance(transcript, Transcript) else Transcript(text=str(transcript))
        wake = self.wake.detect(item.text)
        if wake is not None:
            command = wake.command
            wake_source = "wake_phrase"
        elif self.session.is_active():
            command = item.text.strip()
            wake_source = "active_session"
        else:
            # 未唤醒语音只在本机内存中完成关键词判断，不向事件流暴露原文。
            self._emit("voice.metric", {"name": "wake_miss", "value": 1})
            self._emit("voice.state", {"state": "sleeping"})
            return VoiceResult(status="wake_miss")

        if not self.permission.allowed():
            self._emit("voice.error", {"code": "permission_revoked"})
            return VoiceResult(status="permission_denied", detail="voice_control was revoked")
        if wake is not None:
            self.session.activate()
        if cancellation.cancelled:
            return self._cancelled(command=command)

        self._emit(
            "voice.final",
            {"text": command, "language": item.language, "source": wake_source},
        )
        if not command:
            self._emit("voice.state", {"state": "wake_detected"})
            return VoiceResult(status="wake_only")

        commands = self.splitter.split(command)
        if len(commands) > 1:
            self._emit("voice.state", {"state": "compound", "steps": len(commands)})
        decisions: list[RouteDecision] = []
        actions: list[ActionResult] = []

        for index, part in enumerate(commands):
            if cancellation.cancelled:
                return self._cancelled(command=command, decisions=decisions, actions=actions)
            if not self.permission.allowed():
                self._emit("voice.error", {"code": "permission_revoked", "step": index})
                return VoiceResult(
                    status="permission_denied",
                    command=command,
                    decisions=tuple(decisions),
                    actions=tuple(actions),
                    detail="voice_control was revoked before routing",
                )

            self._emit("voice.state", {"state": "deciding", "step": index, "steps": len(commands)})
            decision = self.router.route(part)
            decisions.append(decision)
            self._emit(
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
                self._emit("voice.confirmation", {"required": True, "command": part, "step": index})
                return VoiceResult(
                    status="confirmation_required",
                    command=command,
                    decision=decision,
                    decisions=tuple(decisions),
                    actions=tuple(actions),
                )
            if not decision.accepted:
                self._emit("voice.state", {"state": "rejected", "step": index})
                return VoiceResult(
                    status="rejected",
                    command=command,
                    decision=decision,
                    decisions=tuple(decisions),
                    actions=tuple(actions),
                    detail=decision.reason,
                )
            if cancellation.cancelled:
                return self._cancelled(command=command, decisions=decisions, actions=actions)
            if not self.permission.allowed():
                self._emit("voice.error", {"code": "permission_revoked", "step": index})
                return VoiceResult(
                    status="permission_denied",
                    command=command,
                    decision=decision,
                    decisions=tuple(decisions),
                    actions=tuple(actions),
                    detail="voice_control was revoked before action",
                )

            action = self.executor.execute(decision)
            actions.append(action)
            self._emit(
                "voice.action",
                {
                    "step": index,
                    "steps": len(commands),
                    "ok": action.ok,
                    "action": action.action,
                    "detail": action.detail,
                },
            )
            if not action.ok:
                self._emit("voice.error", {"code": "action_failed", "detail": action.detail, "step": index})
                return VoiceResult(
                    status="action_failed",
                    command=command,
                    decision=decision,
                    action=action,
                    decisions=tuple(decisions),
                    actions=tuple(actions),
                    detail=action.detail,
                )

        self.session.touch()
        if cancellation.cancelled:
            return self._cancelled(command=command, decisions=decisions, actions=actions)

        reply = self._completion_reply(actions)
        self._emit("voice.tts", {"state": "started", "text": reply, "backend": "local"})
        self.speaker.speak(reply)
        self._emit("voice.tts", {"state": "finished", "backend": "local"})
        self._emit("voice.state", {"state": "completed", "steps": len(actions)})
        return VoiceResult(
            status="executed",
            command=command,
            decision=decisions[-1] if decisions else None,
            action=actions[-1] if actions else None,
            decisions=tuple(decisions),
            actions=tuple(actions),
        )

    @staticmethod
    def _completion_reply(actions: list[ActionResult]) -> str:
        if len(actions) == 1 and actions[0].action == "open_app:notepad":
            return "好的，记事本打开了。"
        return "好的，已经完成了。"

    def _cancelled(
        self,
        *,
        command: str = "",
        decisions: list[RouteDecision] | None = None,
        actions: list[ActionResult] | None = None,
    ) -> VoiceResult:
        self._emit("voice.state", {"state": "cancelled"})
        return VoiceResult(
            status="cancelled",
            command=command,
            decisions=tuple(decisions or ()),
            actions=tuple(actions or ()),
        )

    def _set_current_operation(self, token: CancellationToken) -> CancellationToken | None:
        with self._operation_lock:
            previous = self._current_cancel
            self._current_cancel = token
            return previous

    def _clear_current_operation(self, token: CancellationToken) -> None:
        with self._operation_lock:
            if self._current_cancel is token:
                self._current_cancel = None

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self.events.emit(event_type, payload)
