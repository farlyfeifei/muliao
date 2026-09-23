from __future__ import annotations

from pathlib import Path
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.asr_streaming import StreamingZipformerRecognizer


class FakeResult:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeOnlineStream:
    def __init__(self, recognizer: "FakeOnlineRecognizer") -> None:
        self.recognizer = recognizer
        self.accepted: list[tuple[int, np.ndarray]] = []
        self.input_finished_calls = 0
        self.pending: list[str] = []
        self.current_text = ""

    def accept_waveform(self, sample_rate: int, samples: np.ndarray) -> None:
        self.accepted.append((sample_rate, samples.copy()))
        if self.recognizer.hypotheses_per_accept:
            self.pending.extend(self.recognizer.hypotheses_per_accept.pop(0))

    def input_finished(self) -> None:
        self.input_finished_calls += 1
        if self.recognizer.hypotheses_per_finish:
            self.pending.extend(self.recognizer.hypotheses_per_finish.pop(0))


class FakeOnlineRecognizer:
    def __init__(
        self,
        hypotheses_per_accept: list[list[str]],
        hypotheses_per_finish: list[list[str]] | None = None,
    ) -> None:
        self.hypotheses_per_accept = [list(items) for items in hypotheses_per_accept]
        self.hypotheses_per_finish = [
            list(items) for items in (hypotheses_per_finish or [])
        ]
        self.streams: list[FakeOnlineStream] = []
        self.reset_calls = 0
        self.close_calls = 0

    def create_stream(self) -> FakeOnlineStream:
        stream = FakeOnlineStream(self)
        self.streams.append(stream)
        return stream

    def is_ready(self, stream: FakeOnlineStream) -> bool:
        return bool(stream.pending)

    def decode_stream(self, stream: FakeOnlineStream) -> None:
        stream.current_text = stream.pending.pop(0)

    def get_result_all(self, stream: FakeOnlineStream) -> FakeResult:
        return FakeResult(stream.current_text)

    def is_endpoint(self, stream: FakeOnlineStream) -> bool:
        return False

    def reset(self, stream: FakeOnlineStream) -> None:
        self.reset_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class StreamingZipformerTests(unittest.TestCase):
    @staticmethod
    def make_adapter(fake: FakeOnlineRecognizer, **kwargs) -> StreamingZipformerRecognizer:
        return StreamingZipformerRecognizer(
            "tokens.txt",
            "encoder.onnx",
            "decoder.onnx",
            "joiner.onnx",
            recognizer=fake,
            tail_padding_seconds=0,
            **kwargs,
        )

    def test_first_partial_is_structured_and_non_actionable(self):
        fake = FakeOnlineRecognizer([["幕僚"]])
        adapter = self.make_adapter(fake)

        updates = adapter.accept_waveform(b"\0\0\1\0")

        self.assertEqual(len(updates), 1)
        update = updates[0]
        self.assertEqual(update.kind, "partial")
        self.assertEqual(update.text, "幕僚")
        self.assertEqual(update.raw_text, "幕僚")
        self.assertEqual(update.stable_text, "")
        self.assertFalse(update.actionable)
        self.assertEqual(
            update.as_payload(),
            {
                "text": "幕僚",
                "raw_text": "幕僚",
                "stable_text": "",
                "final": False,
                "actionable": False,
            },
        )
        sample_rate, samples = fake.streams[0].accepted[0]
        self.assertEqual(sample_rate, 16_000)
        np.testing.assert_allclose(samples, [0.0, 1.0 / 32768.0])

    def test_updated_partial_exposes_longest_common_prefix(self):
        fake = FakeOnlineRecognizer([["幕僚"], ["幕僚打开"]])
        adapter = self.make_adapter(fake)

        adapter.accept_waveform(b"\0\0")
        updates = adapter.accept_waveform(b"\0\0")

        self.assertEqual([item.text for item in updates], ["幕僚打开"])
        self.assertEqual(updates[0].stable_text, "幕僚")

    def test_consecutive_duplicate_partial_is_suppressed(self):
        fake = FakeOnlineRecognizer([["幕僚", "幕僚", "幕僚打开"]])
        adapter = self.make_adapter(fake)

        updates = adapter.accept_waveform(b"\0\0")

        self.assertEqual([item.text for item in updates], ["幕僚", "幕僚打开"])
        self.assertTrue(all(not item.actionable for item in updates))

    def test_finish_flushes_and_returns_final_raw_text(self):
        fake = FakeOnlineRecognizer(
            [["  幕僚   打开  "]],
            hypotheses_per_finish=[["幕僚打开记事本。"]],
        )
        adapter = self.make_adapter(fake)
        adapter.accept_waveform(b"\0\0")

        updates = adapter.finish()

        self.assertEqual([item.kind for item in updates], ["partial", "final"])
        final = updates[-1]
        self.assertEqual(final.raw_text, "幕僚打开记事本。")
        self.assertEqual(final.text, "幕僚打开记事本。")
        self.assertEqual(final.stable_text, "幕僚打开记事本。")
        self.assertFalse(final.actionable)
        self.assertEqual(fake.streams[0].input_finished_calls, 1)
        self.assertEqual(adapter.finish(), ())

    def test_reset_starts_a_new_stream_and_clears_stability(self):
        fake = FakeOnlineRecognizer([["幕僚"], ["打开"]])
        adapter = self.make_adapter(fake)
        adapter.accept_waveform(b"\0\0")

        adapter.reset()
        updates = adapter.accept_waveform(b"\0\0")

        self.assertEqual(len(fake.streams), 2)
        self.assertEqual(fake.reset_calls, 1)
        self.assertEqual(updates[0].text, "打开")
        self.assertEqual(updates[0].stable_text, "")

    def test_rejects_wrong_audio_format_before_loading_recognizer(self):
        adapter = StreamingZipformerRecognizer(
            "missing-tokens.txt",
            "missing-encoder.onnx",
            "missing-decoder.onnx",
            "missing-joiner.onnx",
        )

        with self.assertRaisesRegex(ValueError, "16000 Hz"):
            adapter.accept_waveform(b"\0\0", sample_rate=8_000)
        with self.assertRaisesRegex(ValueError, "16-bit mono"):
            adapter.accept_waveform(b"\0\0", channels=2)
        with self.assertRaisesRegex(ValueError, "16-bit mono"):
            adapter.accept_waveform(b"\0\0", sample_width=1)
        with self.assertRaisesRegex(ValueError, "align to int16"):
            adapter.accept_waveform(b"\0")
        self.assertFalse(adapter.recognizer_loaded)

    def test_recognizer_and_model_files_are_lazy(self):
        adapter = StreamingZipformerRecognizer(
            "missing-tokens.txt",
            "missing-encoder.onnx",
            "missing-decoder.onnx",
            "missing-joiner.onnx",
        )
        self.assertFalse(adapter.recognizer_loaded)

        with self.assertRaisesRegex(FileNotFoundError, "missing streaming Zipformer assets"):
            adapter.accept_waveform(b"\0\0")

    def test_close_is_idempotent_and_blocks_more_audio(self):
        fake = FakeOnlineRecognizer([["幕僚"]])
        adapter = self.make_adapter(fake)
        adapter.accept_waveform(b"\0\0")

        adapter.close()
        adapter.close()

        self.assertEqual(fake.reset_calls, 1)
        self.assertEqual(fake.close_calls, 1)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            adapter.accept_waveform(b"\0\0")


if __name__ == "__main__":
    unittest.main()
