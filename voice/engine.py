"""M0 语音主循环：权限 → 本地 ASR → 唤醒 → Jev → 白名单动作 → 本地 TTS。"""
from __future__ import annotations

from dataclasses import asdict
from typing import Mapping, Any

from .contracts import (
    ActionExecutor,
    AudioCapture,
    AudioSegment,
    CommandRouter,
    EventSink,
    PermissionGate,
    Recognizer,
    Speaker,
    Transcript,
    VoiceResult,
)
from .events import NullEventSink
from .wake import WakeDetector


class VoiceEngine:
    """可注入依赖的 M0 引擎。

    安全顺序不可调整：权限检查发生在采音之前；Jev 路由发生在唤醒成功之后；动作执行发生在
    路由白名单通过之后。这样可由自动测试证明未授权/未唤醒时外部调用严格为零。
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
        events: EventSink | None = None,
    ) -> None:
        self.permission = permission
        self.recognizer = recognizer
        self.router = router
        self.executor = executor
        self.speaker = speaker
        self.wake = wake or WakeDetector()
        self.events = events or NullEventSink()

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
    ) -> VoiceResult:
        if not permission_checked and not self.permission.allowed():
            self._emit("voice.error", {"code": "permission_denied"})
            return VoiceResult(status="permission_denied", detail="voice_control is not granted")

        item = transcript if isinstance(transcript, Transcript) else Transcript(text=str(transcript))
        self._emit("voice.final", {"text": item.text, "language": item.language})
        wake = self.wake.detect(item.text)
        if wake is None:
            self._emit("voice.metric", {"name": "wake_miss", "value": 1})
            self._emit("voice.state", {"state": "sleeping"})
            return VoiceResult(status="wake_miss", transcript=item.text)
        if not wake.command:
            self._emit("voice.state", {"state": "wake_detected"})
            return VoiceResult(status="wake_only", transcript=item.text)

        self._emit("voice.state", {"state": "deciding"})
        decision = self.router.route(wake.command)
        self._emit(
            "voice.decision",
            {
                "command": wake.command,
                "accepted": decision.accepted,
                "kind": decision.kind,
                "target": decision.target,
                "confidence": decision.confidence,
                "destructive": decision.destructive,
            },
        )
        if decision.destructive:
            self._emit("voice.confirmation", {"required": True, "command": wake.command})
            return VoiceResult(
                status="confirmation_required",
                transcript=item.text,
                command=wake.command,
                decision=decision,
            )
        if not decision.accepted:
            self._emit("voice.state", {"state": "rejected"})
            return VoiceResult(
                status="rejected",
                transcript=item.text,
                command=wake.command,
                decision=decision,
                detail=decision.reason,
            )

        action = self.executor.execute(decision)
        self._emit("voice.action", {"ok": action.ok, "action": action.action, "detail": action.detail})
        if not action.ok:
            self._emit("voice.error", {"code": "action_failed", "detail": action.detail})
            return VoiceResult(
                status="action_failed",
                transcript=item.text,
                command=wake.command,
                decision=decision,
                action=action,
                detail=action.detail,
            )

        reply = "好的，记事本打开了。"
        self._emit("voice.tts", {"state": "started", "text": reply, "backend": "local"})
        self.speaker.speak(reply)
        self._emit("voice.tts", {"state": "finished", "backend": "local"})
        self._emit("voice.state", {"state": "completed"})
        return VoiceResult(
            status="executed",
            transcript=item.text,
            command=wake.command,
            decision=decision,
            action=action,
        )

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self.events.emit(event_type, payload)
