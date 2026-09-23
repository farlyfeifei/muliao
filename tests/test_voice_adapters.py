from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.actions import DryRunExecutor, WindowsNotepadExecutor
from voice.asr_local import SenseVoiceRecognizer
from voice.capture import PyAudioVADCapture
from voice.contracts import AudioSegment, RouteDecision
from voice.events import JsonLineEventSink
from voice.jev_router import JevM0Router
from voice.tts_local import SapiSpeaker


class FakeStream:
    def __init__(self, frames: list[bytes]) -> None:
        self.frames = list(frames)
        self.reads = 0

    def read(self, size: int, exception_on_overflow: bool = False) -> bytes:
        self.reads += 1
        if not self.frames:
            return b"\0\0" * size
        return self.frames.pop(0)


class SequenceVad:
    def __init__(self, sequence: list[bool]) -> None:
        self.sequence = list(sequence)

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        return self.sequence.pop(0) if self.sequence else False


class CaptureAdapterTests(unittest.TestCase):
    def test_vad_capture_includes_speech_and_trailing_silence(self):
        frame = b"\1\0" * 480
        stream = FakeStream([frame] * 7)
        vad = SequenceVad([False, False, True, True, True, False, False])
        capture = PyAudioVADCapture(
            frame_ms=30,
            pre_roll_ms=30,
            silence_ms=60,
            min_speech_ms=60,
            max_utterance_ms=1_000,
            listen_timeout_ms=1_000,
        )
        segment = capture._read_segment(stream, vad)
        self.assertEqual(segment.sample_rate, 16_000)
        self.assertEqual(len(segment.pcm), 5 * len(frame))

    def test_vad_capture_times_out_when_no_speech_arrives(self):
        frame = b"\0\0" * 480
        stream = FakeStream([frame] * 3)
        vad = SequenceVad([False, False, False])
        capture = PyAudioVADCapture(
            frame_ms=30,
            listen_timeout_ms=90,
            max_utterance_ms=1_000,
        )
        with self.assertRaisesRegex(TimeoutError, "no speech"):
            capture._read_segment(stream, vad)


class FakeSenseResult:
    text = "幕僚幕僚，打开记事本。"
    emotion = "<|NEUTRAL|>"
    event = "<|Speech|>"
    lang = "<|zh|>"
    timestamps = [0.1, 0.2]


class FakeSenseStream:
    def __init__(self) -> None:
        self.result = FakeSenseResult()
        self.accepted = None

    def accept_waveform(self, sample_rate, samples):
        self.accepted = (sample_rate, samples)


class FakeSenseRecognizer:
    def __init__(self) -> None:
        self.stream = FakeSenseStream()

    def create_stream(self):
        return self.stream

    def decode_stream(self, stream):
        return None


class SenseVoiceAdapterTests(unittest.TestCase):
    def test_transcribes_int16_pcm_and_preserves_metadata(self):
        adapter = SenseVoiceRecognizer("unused")
        fake = FakeSenseRecognizer()
        adapter._recognizer = fake
        result = adapter.transcribe(AudioSegment(b"\0\0\1\0", sample_rate=16_000))
        self.assertEqual(result.text, "幕僚幕僚，打开记事本。")
        self.assertEqual(result.language, "<|zh|>")
        self.assertEqual(result.metadata["event"], "<|Speech|>")
        self.assertEqual(fake.stream.accepted[0], 16_000)
        self.assertEqual(len(fake.stream.accepted[1]), 2)

    def test_rejects_non_int16_mono_audio(self):
        adapter = SenseVoiceRecognizer("unused")
        with self.assertRaisesRegex(ValueError, "16-bit mono"):
            adapter.transcribe(AudioSegment(b"\0\0", channels=2))


class JevRouterTests(unittest.TestCase):
    @staticmethod
    def _response(*, destructive=0.01, kind="open_app", app="notepad"):
        return {
            "model": "jev-test",
            "answers": {
                "addressed": {"noul": 0.99},
                "complete": {"noul": 0.98},
                "destructive": {"noul": destructive},
                "kind": {"choice": kind, "confidence": 0.97},
                "app": {"choice": app, "confidence": 0.96},
            },
        }

    def test_accepts_only_confident_notepad_launch(self):
        seen = []

        def handler(request: httpx.Request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=self._response())

        client = httpx.Client(transport=httpx.MockTransport(handler))
        router = JevM0Router(url="https://example.test/systemone", api_key="test-only", client=client)
        decision = router.route("打开记事本")
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.kind, "open_app")
        self.assertEqual(decision.target, "notepad")
        self.assertEqual(len(seen), 1)
        self.assertIn("打开记事本", seen[0]["state"])
        self.assertNotIn("幕僚幕僚", seen[0]["state"])

    def test_blocks_other_apps_and_destructive_routes(self):
        payloads = [
            self._response(app="none"),
            self._response(destructive=0.9),
        ]

        def handler(request: httpx.Request):
            return httpx.Response(200, json=payloads.pop(0))

        client = httpx.Client(transport=httpx.MockTransport(handler))
        router = JevM0Router(url="https://example.test", api_key="test-only", client=client)
        self.assertFalse(router.route("打开计算器").accepted)
        destructive = router.route("删除全部文件")
        self.assertFalse(destructive.accepted)
        self.assertTrue(destructive.destructive)

    def test_missing_key_does_not_touch_network(self):
        calls = []

        def handler(request: httpx.Request):
            calls.append(request)
            raise AssertionError("network must not be called")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        router = JevM0Router(url="https://example.test", api_key="", client=client)
        decision = router.route("打开记事本")
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "jev_missing_api_key")
        self.assertEqual(calls, [])


class ActionAndTtsAdapterTests(unittest.TestCase):
    def test_notepad_executor_has_a_strict_allowlist(self):
        launched = []
        executor = WindowsNotepadExecutor(launcher=launched.append)
        ok = executor.execute(RouteDecision(True, "open_app", "notepad", 0.99))
        self.assertTrue(ok.ok)
        self.assertEqual(launched, ["notepad.exe"])
        blocked = executor.execute(RouteDecision(True, "open_app", "calculator", 0.99))
        self.assertFalse(blocked.ok)
        self.assertEqual(launched, ["notepad.exe"])

    def test_dry_run_reports_without_side_effects(self):
        result = DryRunExecutor().execute(RouteDecision(True, "open_app", "notepad", 0.99))
        self.assertTrue(result.ok)
        self.assertIn("dry-run", result.detail)

    def test_sapi_speaker_uses_injected_dispatch(self):
        spoken = []

        class Voice:
            def Speak(self, text):
                spoken.append(text)

        speaker = SapiSpeaker(dispatch=lambda name: Voice())
        speaker.speak("好的，记事本打开了。")
        self.assertEqual(spoken, ["好的，记事本打开了。"])


class EventAdapterTests(unittest.TestCase):
    def test_json_line_events_use_voice_namespace_and_sequence(self):
        stream = io.StringIO()
        sink = JsonLineEventSink(stream)
        sink.emit("voice.state", {"state": "listening"})
        sink.emit("voice.metric", {"name": "latency", "value": 1})
        rows = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([row["seq"] for row in rows], [1, 2])
        self.assertEqual(rows[0]["type"], "voice.state")
        with self.assertRaises(ValueError):
            sink.emit("done", {})


if __name__ == "__main__":
    unittest.main()
