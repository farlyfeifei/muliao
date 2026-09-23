from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.contracts import ActionResult, AudioSegment, RouteDecision, Transcript
from voice.engine import VoiceEngine
from voice.events import MemoryEventSink
from voice.wake import WakeDetector


class Counter:
    def __init__(self) -> None:
        self.calls = 0


class FakePermission(Counter):
    def __init__(self, enabled: bool, sequence: list[bool] | None = None) -> None:
        super().__init__()
        self.enabled = enabled
        self.sequence = list(sequence or [])

    def allowed(self) -> bool:
        self.calls += 1
        if self.sequence:
            return self.sequence.pop(0)
        return self.enabled


class FakeCapture(Counter):
    def capture_utterance(self) -> AudioSegment:
        self.calls += 1
        return AudioSegment(b"\0\0" * 160)


class TimeoutCapture(Counter):
    def capture_utterance(self, *, cancellation=None) -> AudioSegment:
        self.calls += 1
        raise TimeoutError("no speech detected before capture timeout")


class FakeRecognizer(Counter):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def transcribe(self, audio: AudioSegment) -> Transcript:
        self.calls += 1
        return Transcript(self.text)


class FakeRouter(Counter):
    def __init__(self, decision: RouteDecision | None = None) -> None:
        super().__init__()
        self.commands: list[str] = []
        self.decision = decision or RouteDecision(
            accepted=True,
            kind="open_app",
            target="notepad",
            confidence=0.99,
        )

    def route(self, command: str) -> RouteDecision:
        self.calls += 1
        self.commands.append(command)
        return self.decision


class FakeExecutor(Counter):
    def __init__(self, ok: bool = True) -> None:
        super().__init__()
        self.decisions: list[RouteDecision] = []
        self.ok = ok

    def execute(self, decision: RouteDecision) -> ActionResult:
        self.calls += 1
        self.decisions.append(decision)
        return ActionResult(self.ok, "open_app:notepad", "opened" if self.ok else "failed")


class FakeSpeaker(Counter):
    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    def speak(self, text: str) -> None:
        self.calls += 1
        self.texts.append(text)


def make_engine(
    *,
    allowed=True,
    permission_sequence=None,
    transcript="幕僚幕僚，打开记事本",
    decision=None,
    echo_guard=None,
):
    permission = FakePermission(allowed, permission_sequence)
    recognizer = FakeRecognizer(transcript)
    router = FakeRouter(decision)
    executor = FakeExecutor()
    speaker = FakeSpeaker()
    events = MemoryEventSink()
    engine = VoiceEngine(
        permission=permission,
        recognizer=recognizer,
        router=router,
        executor=executor,
        speaker=speaker,
        events=events,
        echo_guard=echo_guard,
    )
    return engine, permission, recognizer, router, executor, speaker, events


class WakeDetectorTests(unittest.TestCase):
    def test_strips_fixed_wake_phrase(self):
        match = WakeDetector().detect("幕僚幕僚，打开记事本")
        self.assertIsNotNone(match)
        self.assertEqual(match.command, "打开记事本")

    def test_accepts_pause_punctuation_between_words(self):
        match = WakeDetector().detect("幕僚……幕僚，打开记事本")
        self.assertIsNotNone(match)
        self.assertEqual(match.command, "打开记事本")

    def test_accepts_verified_homophone_alias_from_real_sensevoice(self):
        match = WakeDetector().detect("木聊木聊打开记事本。")
        self.assertIsNotNone(match)
        self.assertEqual(match.command, "打开记事本。")

    def test_rejects_single_or_mid_sentence_wake_word(self):
        detector = WakeDetector()
        self.assertIsNone(detector.detect("幕僚，打开记事本"))
        self.assertIsNone(detector.detect("请幕僚幕僚打开记事本"))
        self.assertIsNone(detector.detect("打开记事本"))


class VoiceM0Tests(unittest.TestCase):
    def test_denied_permission_does_not_open_microphone_or_call_dependencies(self):
        engine, _, recognizer, router, executor, speaker, _ = make_engine(allowed=False)
        capture = FakeCapture()
        result = engine.run_once(capture)
        self.assertEqual(result.status, "permission_denied")
        self.assertEqual(capture.calls, 0)
        self.assertEqual(recognizer.calls, 0)
        self.assertEqual(router.calls, 0)
        self.assertEqual(executor.calls, 0)
        self.assertEqual(speaker.calls, 0)

    def test_capture_timeout_is_metric_not_error_and_calls_no_dependencies(self):
        engine, _, recognizer, router, executor, speaker, events = make_engine()
        capture = TimeoutCapture()

        result = engine.run_once(capture)

        self.assertEqual(result.status, "capture_timeout")
        self.assertEqual(capture.calls, 1)
        self.assertEqual(recognizer.calls, 0)
        self.assertEqual(router.calls, 0)
        self.assertEqual(executor.calls, 0)
        self.assertEqual(speaker.calls, 0)
        self.assertFalse(any(event.type == "voice.error" for event in events.events))
        self.assertEqual(
            [event.payload.get("name") for event in events.events if event.type == "voice.metric"],
            ["capture_timeout"],
        )

    def test_wake_miss_has_zero_jev_and_action_calls(self):
        engine, _, recognizer, router, executor, speaker, events = make_engine(
            transcript="打开记事本"
        )
        result = engine.process_audio(AudioSegment(b"\0\0" * 160))
        self.assertEqual(result.status, "wake_miss")
        self.assertEqual(result.transcript, "")
        self.assertEqual(recognizer.calls, 1)
        self.assertEqual(router.calls, 0)
        self.assertEqual(executor.calls, 0)
        self.assertEqual(speaker.calls, 0)
        self.assertTrue(any(e.type == "voice.metric" for e in events.events))
        self.assertFalse(any(e.type == "voice.final" for e in events.events))

    def test_echo_drop_has_zero_jev_action_tts_and_leaks_no_transcript(self):
        echoed = "幕僚幕僚这是刚才的播报回声"

        class EchoGuard:
            def __init__(self) -> None:
                self.checked: list[str] = []

            def should_drop(self, text: str) -> bool:
                self.checked.append(text)
                return text == echoed

        guard = EchoGuard()
        engine, _, _, router, executor, speaker, events = make_engine(
            transcript=echoed,
            echo_guard=guard,
        )

        result = engine.process_transcript(echoed)

        self.assertEqual(result.status, "echo_drop")
        self.assertEqual(result.transcript, "")
        self.assertEqual(guard.checked, [echoed])
        self.assertEqual(router.calls, 0)
        self.assertEqual(executor.calls, 0)
        self.assertEqual(speaker.calls, 0)
        self.assertEqual(
            [event.payload.get("name") for event in events.events if event.type == "voice.metric"],
            ["echo_drop"],
        )
        self.assertFalse(any(event.type == "voice.final" for event in events.events))
        self.assertNotIn(echoed, repr([event.payload for event in events.events]))

    def test_permission_revoked_after_recognition_stops_before_jev(self):
        engine, _, _, router, executor, speaker, events = make_engine(
            permission_sequence=[True, False]
        )
        result = engine.process_transcript("幕僚幕僚，打开记事本")
        self.assertEqual(result.status, "permission_denied")
        self.assertEqual(router.calls, 0)
        self.assertEqual(executor.calls, 0)
        self.assertEqual(speaker.calls, 0)
        self.assertTrue(any(e.payload.get("code") == "permission_revoked" for e in events.events))

    def test_permission_revoked_after_jev_stops_before_action(self):
        engine, _, _, router, executor, speaker, events = make_engine(
            permission_sequence=[True, True, True, False]
        )
        result = engine.process_transcript("幕僚幕僚，打开记事本")
        self.assertEqual(result.status, "permission_denied")
        self.assertEqual(router.calls, 1)
        self.assertEqual(executor.calls, 0)
        self.assertEqual(speaker.calls, 0)
        self.assertTrue(any(e.payload.get("code") == "permission_revoked" for e in events.events))

    def test_wake_phrase_is_stripped_before_router_and_notepad_executes(self):
        engine, _, _, router, executor, speaker, events = make_engine()
        result = engine.process_transcript("幕僚幕僚，打开记事本")
        self.assertEqual(result.status, "executed")
        self.assertEqual(router.commands, ["打开记事本"])
        self.assertEqual(executor.calls, 1)
        self.assertEqual(speaker.texts, ["好的，记事本打开了。"])
        self.assertTrue(all(e.type.startswith("voice.") for e in events.events))

    def test_rejected_route_does_not_execute_or_speak(self):
        decision = RouteDecision(accepted=False, kind="none", reason="not whitelisted")
        engine, _, _, router, executor, speaker, _ = make_engine(decision=decision)
        result = engine.process_transcript("幕僚幕僚，删除全部文件")
        self.assertEqual(result.status, "rejected")
        self.assertEqual(router.calls, 1)
        self.assertEqual(executor.calls, 0)
        self.assertEqual(speaker.calls, 0)

    def test_destructive_route_requires_confirmation(self):
        decision = RouteDecision(
            accepted=False,
            kind="open_app",
            target="notepad",
            confidence=0.9,
            destructive=True,
        )
        engine, _, _, _, executor, speaker, events = make_engine(decision=decision)
        result = engine.process_transcript("幕僚幕僚，执行危险动作")
        self.assertEqual(result.status, "confirmation_required")
        self.assertEqual(executor.calls, 0)
        self.assertEqual(speaker.calls, 0)
        self.assertTrue(any(e.type == "voice.confirmation" for e in events.events))


    def test_tts_failure_after_commit_stays_executed_not_retryable(self):
        engine, _, _, _, executor, _, events = make_engine()

        class FailingSpeaker(Counter):
            def speak(self, text: str, **kwargs) -> None:
                self.calls += 1
                raise RuntimeError("audio device busy")

        engine.speaker = FailingSpeaker()

        result = engine.process_transcript("幕僚幕僚，打开记事本")

        self.assertEqual(result.status, "executed")
        self.assertEqual(executor.calls, 1)
        self.assertTrue(result.detail.startswith("tts_failed:"))
        self.assertTrue(
            any(e.payload.get("code") == "tts_failed" for e in events.events),
        )


if __name__ == "__main__":
    unittest.main()
