from __future__ import annotations

from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.contracts import ActionResult, AudioSegment, RouteDecision, Transcript
from voice.engine import VoiceEngine
from voice.events import MemoryEventSink
from voice.session_state import VoiceSession


AUDIO = AudioSegment(b"\0\0" * 160)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Permission:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def allowed(self) -> bool:
        return self.enabled


class Router:
    def __init__(self, decision: RouteDecision | None = None, delay: float = 0.0) -> None:
        self.decision = decision or RouteDecision(True, "open_app", "notepad", 0.99)
        self.delay = delay
        self.calls = 0

    def route(self, command: str) -> RouteDecision:
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return self.decision


class Executor:
    def __init__(self, result: ActionResult | None = None, delay: float = 0.0) -> None:
        self.result = result
        self.delay = delay
        self.calls = 0

    def execute(self, decision: RouteDecision) -> ActionResult:
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return self.result or ActionResult(True, f"{decision.kind}:{decision.target}", "ok")


class Speaker:
    enabled = True

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.texts: list[str] = []
        self.last_backend = "local"

    def speak(self, text: str, *, cancellation=None, operation_id=None) -> None:
        if self.delay:
            time.sleep(self.delay)
        self.texts.append(text)

    def stop(self, *, operation_id=None) -> bool:
        return True


class Recognizer:
    def __init__(self, text: str = "打开记事本", delay: float = 0.0) -> None:
        self.text = text
        self.delay = delay
        self.calls = 0

    def transcribe(self, audio, *, cancellation=None):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return Transcript(self.text)


def build(*, router=None, executor=None, speaker=None, recognizer=None):
    events = MemoryEventSink()
    engine = VoiceEngine(
        permission=Permission(),
        recognizer=recognizer or Recognizer(),
        router=router or Router(),
        executor=executor or Executor(),
        speaker=speaker if speaker is not None else Speaker(),
        events=events,
        session=VoiceSession(window_seconds=8.0, clock=Clock()),
    )
    return engine, events


def metrics(events: MemoryEventSink) -> dict[str, float]:
    out: dict[str, float] = {}
    for event in events.events:
        if event.type == "voice.metric" and isinstance(event.payload.get("value"), (int, float)):
            name = event.payload.get("name")
            if isinstance(name, str) and name.endswith("_ms"):
                out.setdefault(name, 0.0)
                out[name] = max(out[name], float(event.payload["value"]))
    return out


class SegmentTimingTests(unittest.TestCase):
    def test_full_flow_emits_jev_exec_and_tts_segments(self):
        engine, events = build(
            router=Router(delay=0.01), executor=Executor(delay=0.01), speaker=Speaker(delay=0.01)
        )
        result = engine.process_transcript("幕僚幕僚，打开记事本")
        self.assertEqual(result.status, "executed")
        sample = metrics(events)
        for name in ("jev_ms", "exec_ms", "tts_ms"):
            self.assertIn(name, sample, f"{name} latency sample missing")
            self.assertGreaterEqual(sample[name], 5.0, f"{name} should reflect the injected delay")

    def test_asr_segment_is_timed_on_process_audio(self):
        engine, events = build(recognizer=Recognizer("幕僚幕僚，打开记事本", delay=0.01))
        result = engine.process_audio(AUDIO, permission_checked=True)
        self.assertEqual(result.status, "executed")
        sample = metrics(events)
        self.assertIn("asr_ms", sample)
        self.assertGreaterEqual(sample["asr_ms"], 5.0)
        # A transcript path also runs jev/exec/tts, so those appear too.
        self.assertIn("jev_ms", sample)

    def test_transcript_only_path_has_no_asr_segment(self):
        engine, events = build()
        engine.process_transcript("幕僚幕僚，打开记事本")
        self.assertNotIn("asr_ms", metrics(events))

    def test_metric_payload_carries_operation_id_and_no_text(self):
        engine, events = build()
        engine.process_transcript("幕僚幕僚，打开记事本")
        for event in events.events:
            if event.type == "voice.metric" and str(event.payload.get("name", "")).endswith("_ms"):
                self.assertIn("operation_id", event.payload)
                self.assertNotIn("打开记事本", str(event.payload))
                self.assertIsInstance(event.payload["value"], float)

    def test_rejected_route_emits_jev_but_no_exec_or_tts(self):
        engine, events = build(
            router=Router(RouteDecision(False, "none", "none", 0.0, reason="unsupported"))
        )
        result = engine.process_transcript("幕僚幕僚，今天天气不错")
        self.assertEqual(result.status, "rejected")
        sample = metrics(events)
        self.assertIn("jev_ms", sample)
        self.assertNotIn("exec_ms", sample)
        self.assertNotIn("tts_ms", sample)

    def test_failed_action_emits_exec_but_no_tts(self):
        engine, events = build(executor=Executor(ActionResult(False, "open_app:notepad", "boom")))
        result = engine.process_transcript("幕僚幕僚，打开记事本")
        self.assertEqual(result.status, "action_failed")
        sample = metrics(events)
        self.assertIn("exec_ms", sample)
        self.assertNotIn("tts_ms", sample)

    def test_router_exception_propagates_and_records_no_jev_sample(self):
        # _timed must not emit a phantom latency sample when the inner callable
        # raises; the exception propagates so the caller's error path stays
        # authoritative. (The production FAST router catches its own transport
        # errors and returns a rejected decision, so this guards the helper.)
        class BoomRouter:
            def route(self, command):
                raise RuntimeError("jev down")

        engine, events = build(router=BoomRouter())
        with self.assertRaises(RuntimeError):
            engine.process_transcript("幕僚幕僚，打开记事本")
        self.assertNotIn("jev_ms", metrics(events))

    def test_tts_failure_still_executed_but_no_tts_sample(self):
        class BoomSpeaker(Speaker):
            def speak(self, text, *, cancellation=None, operation_id=None):
                raise RuntimeError("sapi unavailable")

        engine, events = build(speaker=BoomSpeaker())
        result = engine.process_transcript("幕僚幕僚，打开记事本")
        # Side effects committed -> executed even though TTS failed.
        self.assertEqual(result.status, "executed")
        self.assertTrue(str(result.detail).startswith("tts_failed"))
        self.assertNotIn("tts_ms", metrics(events))

    def test_disabled_speaker_skips_tts_segment(self):
        class MutedSpeaker(Speaker):
            enabled = False

        engine, events = build(speaker=MutedSpeaker())
        result = engine.process_transcript("幕僚幕僚，打开记事本")
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.detail, "tts_skipped_disabled")
        self.assertNotIn("tts_ms", metrics(events))


if __name__ == "__main__":
    unittest.main()
