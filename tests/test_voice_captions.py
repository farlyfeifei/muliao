from __future__ import annotations

from pathlib import Path
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.asr_streaming import StreamingTranscriptUpdate
from voice.captions import StreamingCaptionBridge
from voice.events import MemoryEventSink
from voice.wake import WakeDetector


PARTIAL = "partial"
FINAL = "final"


def update(kind: str, text: str) -> StreamingTranscriptUpdate:
    return StreamingTranscriptUpdate(
        kind=kind,
        text=text,
        raw_text=text,
        stable_text=text if kind == FINAL else "",
    )


class FakeStreamingRecognizer:
    def __init__(
        self,
        accepts: list[tuple[StreamingTranscriptUpdate, ...]] | None = None,
        finishes: list[tuple[StreamingTranscriptUpdate, ...]] | None = None,
    ) -> None:
        self.accepts = list(accepts or [])
        self.finishes = list(finishes or [])
        self.accepted: list[tuple[bytes, int, int, int]] = []
        self.reset_calls = 0
        self.close_calls = 0

    def accept_waveform(
        self,
        pcm,
        *,
        sample_rate=16_000,
        channels=1,
        sample_width=2,
    ):
        self.accepted.append(
            (bytes(pcm), sample_rate, channels, sample_width)
        )
        return self.accepts.pop(0) if self.accepts else ()

    def finish(self):
        return self.finishes.pop(0) if self.finishes else ()

    def reset(self) -> None:
        self.reset_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class BlockingRecognizer(FakeStreamingRecognizer):
    def __init__(self, returned_update: StreamingTranscriptUpdate) -> None:
        super().__init__()
        self.returned_update = returned_update
        self.entered = threading.Event()
        self.release = threading.Event()

    def accept_waveform(self, pcm, *, sample_rate=16_000, channels=1, sample_width=2):
        self.entered.set()
        if not self.release.wait(2.0):
            raise TimeoutError("test recognizer was not released")
        return (self.returned_update,)


class FakeEchoGuard:
    def __init__(self, *, playing: bool = False, drops: set[str] | None = None) -> None:
        self.playing = playing
        self.drops = set(drops or ())
        self.checked: list[str] = []

    def should_drop(self, text: str) -> bool:
        self.checked.append(text)
        return text in self.drops


class StreamingCaptionBridgeTests(unittest.TestCase):
    def make_bridge(self, recognizer, *, echo_guard=None):
        events = MemoryEventSink()
        bridge = StreamingCaptionBridge(
            recognizer,
            WakeDetector(),
            events,
            echo_guard,
        )
        return bridge, events

    def test_before_wake_emits_metric_without_any_recognizer_text(self):
        secret = "这是私密原文不要泄露"
        recognizer = FakeStreamingRecognizer(
            accepts=[(update(PARTIAL, secret),)]
        )
        bridge, events = self.make_bridge(recognizer)

        bridge.accept_pcm(b"\0\0")

        self.assertEqual([event.type for event in events.events], ["voice.metric"])
        payload = events.events[0].payload
        self.assertEqual(payload["name"], "caption_wake_pending")
        self.assertNotIn("text", payload)
        self.assertNotIn("raw_text", payload)
        self.assertNotIn(secret, repr(payload))
        self.assertFalse(any(event.type in {"voice.partial", "voice.final"} for event in events.events))

    def test_partial_wake_is_stripped_and_display_only(self):
        recognizer = FakeStreamingRecognizer(
            accepts=[(update(PARTIAL, "幕僚幕僚，打开"),)]
        )
        bridge, events = self.make_bridge(recognizer)

        bridge.accept_pcm(b"\0\0")

        event = events.events[0]
        self.assertEqual(event.type, "voice.partial")
        self.assertEqual(event.payload["text"], "打开")
        self.assertEqual(event.payload["stable_text"], "")
        self.assertFalse(event.payload["final"])
        self.assertFalse(event.payload["actionable"])
        self.assertTrue(event.payload["display_only"])
        self.assertNotIn("raw_text", event.payload)
        self.assertNotIn("幕僚", repr(event.payload))

    def test_verified_homophone_alias_opens_the_same_gate(self):
        recognizer = FakeStreamingRecognizer(
            accepts=[(update(PARTIAL, "木聊木聊打开记事本"),)]
        )
        bridge, events = self.make_bridge(recognizer)

        bridge.accept_pcm(b"\0\0")

        self.assertEqual(events.events[0].type, "voice.partial")
        self.assertEqual(events.events[0].payload["text"], "打开记事本")
        self.assertFalse(events.events[0].payload["actionable"])

    def test_later_partial_without_repeated_wake_stays_command_only(self):
        recognizer = FakeStreamingRecognizer(
            accepts=[
                (update(PARTIAL, "幕僚幕僚，打开"),),
                (update(PARTIAL, "打开记事本"),),
            ]
        )
        bridge, events = self.make_bridge(recognizer)

        bridge.accept_pcm(b"\0\0")
        bridge.accept_pcm(b"\0\0")

        partials = [event for event in events.events if event.type == "voice.partial"]
        self.assertEqual([event.payload["text"] for event in partials], ["打开", "打开记事本"])
        self.assertEqual(partials[1].payload["stable_text"], "打开")
        self.assertTrue(all(not event.payload["actionable"] for event in partials))

    def test_rewritten_unprefixed_partial_closes_privacy_gate_without_text(self):
        secret = "会议密码是123456"
        recognizer = FakeStreamingRecognizer(
            accepts=[
                (update(PARTIAL, "幕僚幕僚打开"),),
                (update(PARTIAL, secret),),
            ]
        )
        bridge, events = self.make_bridge(recognizer)

        bridge.accept_pcm(b"\0\0")
        bridge.accept_pcm(b"\0\0")

        self.assertEqual(
            [event.type for event in events.events],
            ["voice.partial", "voice.metric"],
        )
        self.assertEqual(events.events[-1].payload["name"], "caption_wake_lost")
        self.assertNotIn(secret, repr([event.payload for event in events.events]))

    def test_final_is_command_only_non_actionable_then_resets_utterance(self):
        recognizer = FakeStreamingRecognizer(
            accepts=[(update(PARTIAL, "幕僚幕僚打开"),)],
            finishes=[(update(FINAL, "幕僚幕僚打开记事本"),)],
        )
        bridge, events = self.make_bridge(recognizer)
        bridge.accept_pcm(b"\0\0")
        first_utterance = bridge.utterance_id

        bridge.finish()

        final = events.events[-1]
        self.assertEqual(final.type, "voice.final")
        self.assertEqual(final.payload["text"], "打开记事本")
        self.assertEqual(final.payload["stable_text"], "打开记事本")
        self.assertTrue(final.payload["final"])
        self.assertFalse(final.payload["actionable"])
        self.assertTrue(final.payload["display_only"])
        self.assertEqual(final.payload["utterance_id"], first_utterance)
        self.assertEqual(recognizer.reset_calls, 1)
        self.assertEqual(bridge.utterance_id, first_utterance)

        recognizer.accepts.append((update(PARTIAL, "幕僚幕僚新命令"),))
        bridge.accept_pcm(b"\0\0")
        self.assertGreater(bridge.utterance_id, first_utterance)

    def test_unwoken_final_never_leaks_and_resets(self):
        secret = "未唤醒的最终私密原文"
        recognizer = FakeStreamingRecognizer(
            accepts=[(update(PARTIAL, "普通对话"),)],
            finishes=[(update(FINAL, secret),)],
        )
        bridge, events = self.make_bridge(recognizer)

        bridge.accept_pcm(b"\0\0")
        bridge.finish()

        self.assertFalse(any(event.type == "voice.final" for event in events.events))
        self.assertEqual(
            [event.type for event in events.events],
            ["voice.metric", "voice.metric"],
        )
        self.assertNotIn(secret, repr([event.payload for event in events.events]))
        self.assertEqual(recognizer.reset_calls, 1)

    def test_final_cannot_open_gate_without_a_partial_wake(self):
        recognizer = FakeStreamingRecognizer(
            accepts=[(update(PARTIAL, "普通对话"),)],
            finishes=[(update(FINAL, "幕僚幕僚不应由final开门"),)],
        )
        bridge, events = self.make_bridge(recognizer)

        bridge.accept_pcm(b"\0\0")
        bridge.finish()

        self.assertEqual(
            [event.type for event in events.events],
            ["voice.metric", "voice.metric"],
        )
        self.assertEqual(events.events[-1].payload["name"], "caption_wake_miss")
        self.assertNotIn(
            "不应由final开门", repr([event.payload for event in events.events])
        )
        self.assertEqual(recognizer.reset_calls, 1)

    def test_finish_without_active_audio_is_a_noop(self):
        recognizer = FakeStreamingRecognizer()
        bridge, events = self.make_bridge(recognizer)

        bridge.finish()

        self.assertEqual(events.events, [])
        self.assertEqual(recognizer.reset_calls, 0)
        self.assertEqual(bridge.utterance_id, 0)

    def test_echo_playing_emits_zero_events_and_resets_before_asr(self):
        recognizer = FakeStreamingRecognizer(
            accepts=[(update(PARTIAL, "幕僚幕僚不应显示"),)]
        )
        guard = FakeEchoGuard(playing=True)
        bridge, events = self.make_bridge(recognizer, echo_guard=guard)

        bridge.accept_pcm(b"\0\0")

        self.assertEqual(events.events, [])
        self.assertEqual(recognizer.accepted, [])
        self.assertEqual(recognizer.reset_calls, 1)

    def test_echo_should_drop_emits_zero_events_and_resets_utterance(self):
        echoed = "幕僚幕僚这是播报回声"
        recognizer = FakeStreamingRecognizer(
            accepts=[(update(PARTIAL, echoed),)]
        )
        guard = FakeEchoGuard(drops={echoed})
        bridge, events = self.make_bridge(recognizer, echo_guard=guard)

        bridge.accept_pcm(b"\0\0")

        self.assertEqual(events.events, [])
        self.assertEqual(guard.checked, [echoed])
        self.assertEqual(recognizer.reset_calls, 1)

    def test_reset_invalidates_late_recognizer_update(self):
        recognizer = BlockingRecognizer(update(PARTIAL, "幕僚幕僚旧命令"))
        bridge, events = self.make_bridge(recognizer)
        errors: list[BaseException] = []

        def accept() -> None:
            try:
                bridge.accept_pcm(b"\0\0")
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=accept, daemon=True)
        worker.start()
        self.assertTrue(recognizer.entered.wait(2.0))
        old_generation = bridge.generation
        old_utterance = bridge.utterance_id

        bridge.reset()
        recognizer.release.set()
        worker.join(2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(events.events, [])
        self.assertGreater(bridge.generation, old_generation)
        self.assertEqual(bridge.utterance_id, old_utterance)
        self.assertEqual(recognizer.reset_calls, 1)

    def test_all_emitted_events_use_voice_namespace(self):
        recognizer = FakeStreamingRecognizer(
            accepts=[
                (update(PARTIAL, "还没唤醒"),),
                (update(PARTIAL, "幕僚幕僚打开"),),
            ],
            finishes=[(update(FINAL, "幕僚幕僚打开记事本"),)],
        )
        bridge, events = self.make_bridge(recognizer)

        bridge.accept_pcm(b"\0\0")
        bridge.accept_pcm(b"\0\0")
        bridge.finish()

        self.assertEqual(
            [event.type for event in events.events],
            ["voice.metric", "voice.partial", "voice.final"],
        )
        self.assertTrue(all(event.type.startswith("voice.") for event in events.events))

    def test_close_is_idempotent_and_blocks_later_audio(self):
        recognizer = FakeStreamingRecognizer()
        bridge, _ = self.make_bridge(recognizer)

        bridge.close()
        bridge.close()

        self.assertTrue(bridge.closed)
        self.assertEqual(recognizer.close_calls, 1)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            bridge.accept_pcm(b"\0\0")


if __name__ == "__main__":
    unittest.main()
