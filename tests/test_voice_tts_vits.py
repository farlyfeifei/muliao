from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.cancellation import CancellationToken
from voice.tts_vits import REQUIRED_VITS_FILES, VitsSpeaker, _samples_to_pcm16


def make_model_dir(test: unittest.TestCase) -> Path:
    directory = Path(tempfile.mkdtemp())
    (directory / "model.onnx").write_bytes(b"\0")
    (directory / "tokens.txt").write_text("a", encoding="utf-8")
    (directory / "lexicon.txt").write_text("b", encoding="utf-8")
    (directory / "dict").mkdir()
    test.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
    return directory


class FakeResult:
    def __init__(self, samples, sample_rate=22050) -> None:
        self.samples = samples
        self.sample_rate = sample_rate


class FakeTts:
    def __init__(self, samples=None, *, block=None, sample_rate=22050) -> None:
        self.samples = samples if samples is not None else [0.0, 0.5, -0.5, 1.0, -1.0]
        self.sample_rate = sample_rate
        self.calls = 0
        self._block = block

    def generate(self, *, text, sid, speed):
        self.calls += 1
        if self._block is not None:
            self._block.set()
            self._block.clear()
        return FakeResult(self.samples, self.sample_rate)


class FakePlayer:
    """Mimics CancellableAudioPlayer's interface without a real sound card."""

    def __init__(self, *, drain_ok=True, failure=None) -> None:
        self.chunks: list[bytes] = []
        self.begins = 0
        self.stops: list[int | None] = []
        self.closes = 0
        self._generation = 0
        self.drain_ok = drain_ok
        self.failure_error = failure
        self._block_play = None

    def begin(self) -> int:
        self.begins += 1
        self._generation += 1
        return self._generation

    def play(self, pcm: bytes, *, generation: int) -> bool:
        if generation != self._generation:
            return False
        if self._block_play is not None:
            entered, release = self._block_play
            entered.set()
            release.wait(timeout=2.0)
        self.chunks.append(bytes(pcm))
        return True

    def stop(self, *, generation=None) -> bool:
        self.stops.append(generation)
        self._generation += 1
        return True

    def wait_until_drained(self, generation, timeout=None) -> bool:
        return self.drain_ok

    def failure(self, generation):
        return self.failure_error

    def is_drained(self, generation) -> bool:
        return True

    def close(self) -> None:
        self.closes += 1


def speaker(test, *, samples=None, player=None, tts=None, **kwargs):
    factory_calls = {"tts": 0, "player": 0}
    fake_tts = tts if tts is not None else FakeTts(samples)
    fake_player = player if player is not None else FakePlayer()

    def tts_factory(model_dir, **_):
        factory_calls["tts"] += 1
        return fake_tts

    def player_factory(sample_rate):
        factory_calls["player"] += 1
        return fake_player

    sp = VitsSpeaker(
        make_model_dir(test),
        tts_factory=tts_factory,
        player_factory=player_factory,
        **kwargs,
    )
    return sp, fake_tts, fake_player, factory_calls


class ConversionTests(unittest.TestCase):
    def test_int16_byte_length_and_endpoints(self):
        pcm = _samples_to_pcm16([0.0, 0.5, -0.5, 1.0, -1.0])
        self.assertEqual(len(pcm), 10, "5 samples * 2 bytes")
        back = np.frombuffer(pcm, dtype="<i2")
        self.assertEqual(int(back[0]), 0)
        self.assertEqual(int(back[3]), 32767)  # 1.0 -> 0x7fff
        self.assertEqual(int(back[4]), -32767)  # symmetric, no -32768 overflow
        self.assertEqual(int(back[1]), 16383)  # 0.5 * 32767

    def test_clips_out_of_range_samples(self):
        back = np.frombuffer(_samples_to_pcm16([2.0, -3.0]), dtype="<i2")
        self.assertEqual(int(back[0]), 32767)
        self.assertEqual(int(back[1]), -32767)

    def test_accepts_numpy_float32_input(self):
        pcm = _samples_to_pcm16(np.array([0.0, 1.0], dtype=np.float32))
        self.assertEqual(len(pcm), 4)

    def test_flattens_multidimensional_samples(self):
        pcm = _samples_to_pcm16(np.zeros((2, 3), dtype=np.float32))
        self.assertEqual(len(pcm), 12, "6 samples flattened * 2 bytes")


class ConstructionTests(unittest.TestCase):
    def test_construction_is_lazy_and_touches_nothing(self):
        sp, _, _, factories = speaker(self)
        try:
            self.assertEqual(factories["tts"], 0)
            self.assertEqual(factories["player"], 0)
            self.assertTrue(sp.enabled)
            self.assertEqual(sp.last_backend, "vits")
        finally:
            sp.close()

    def test_construction_rejects_invalid_parameters(self):
        with self.assertRaisesRegex(ValueError, "sample_rate"):
            VitsSpeaker(make_model_dir(self), sample_rate=0)
        with self.assertRaisesRegex(ValueError, "chunk_bytes"):
            VitsSpeaker(make_model_dir(self), chunk_bytes=8)


class SpeakTests(unittest.TestCase):
    def test_first_speak_lazy_loads_then_second_reuses_model(self):
        sp, tts, player, factories = speaker(self)
        try:
            sp.speak("好的")
            sp.speak("再来一次")
            self.assertEqual(factories["tts"], 1, "model loaded once and cached")
            self.assertEqual(tts.calls, 2)
            self.assertTrue(player.chunks)
        finally:
            sp.close()

    def test_speak_plays_pcm_through_the_player(self):
        sp, _, player, _ = speaker(self, samples=[0.0, 0.5, -0.5, 1.0, -1.0])
        try:
            sp.speak("好的")
            self.assertEqual(player.begins, 1)
            self.assertEqual(len(b"".join(player.chunks)), 10)
        finally:
            sp.close()

    def test_empty_text_is_a_noop(self):
        sp, tts, player, factories = speaker(self)
        try:
            sp.speak("   ")
            self.assertEqual(tts.calls, 0)
            self.assertEqual(player.begins, 0)
            self.assertEqual(factories["tts"], 0)
        finally:
            sp.close()

    def test_already_cancelled_token_does_nothing(self):
        sp, tts, player, _ = speaker(self)
        token = CancellationToken()
        token.cancel()
        try:
            sp.speak("好的", cancellation=token)
            self.assertEqual(tts.calls, 0)
            self.assertEqual(player.begins, 0)
        finally:
            sp.close()

    def test_large_audio_is_chunked_into_multiple_plays(self):
        samples = [0.1] * 2000  # 4000 bytes, chunk_bytes=1024 -> 4 plays
        sp, _, player, _ = speaker(self, samples=samples, chunk_bytes=1024)
        try:
            sp.speak("长文本")
            self.assertEqual(len(player.chunks), 4)
            self.assertEqual(sum(len(c) for c in player.chunks), 4000)
        finally:
            sp.close()


class OperationIdTests(unittest.TestCase):
    def test_stale_lower_operation_id_is_ignored(self):
        sp, _, player, _ = speaker(self)
        try:
            sp.speak("新的", operation_id=5)
            player.chunks.clear()
            sp.speak("旧的", operation_id=4)  # lower -> ignored
            self.assertEqual(player.chunks, [])
        finally:
            sp.close()

    def test_higher_operation_id_plays(self):
        sp, _, player, _ = speaker(self)
        try:
            sp.speak("一", operation_id=1)
            player.chunks.clear()
            sp.speak("二", operation_id=2)
            self.assertTrue(player.chunks)
        finally:
            sp.close()


class MissingAssetTests(unittest.TestCase):
    def test_missing_model_raises_file_not_found_on_first_speak_only(self):
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(empty, ignore_errors=True))
        calls = {"tts": 0}

        def tts_factory(model_dir, **_):
            calls["tts"] += 1
            return FakeTts()

        # Construction must NOT raise or touch the filesystem.
        sp = VitsSpeaker(empty, tts_factory=tts_factory, player_factory=lambda r: FakePlayer())
        try:
            with self.assertRaises(FileNotFoundError) as ctx:
                sp.speak("好的")
            self.assertIn("missing VITS assets", str(ctx.exception))
            self.assertEqual(calls["tts"], 0, "factory must not run when files are missing")
            # The error names the missing paths so the caller can degrade to SAPI.
            for name in REQUIRED_VITS_FILES:
                self.assertIn(name, str(ctx.exception))
        finally:
            sp.close()


class CancellationTests(unittest.TestCase):
    def test_stop_during_synthesis_suppresses_audio(self):
        block = threading.Event()
        tts = FakeTts(block=block)
        sp, _, player, _ = speaker(self, tts=tts)
        entered = threading.Event()
        # Make generate() block so we can stop mid-synthesis.

        class BlockingTts(FakeTts):
            def generate(self, *, text, sid, speed):
                self.calls += 1
                entered.set()
                block.wait(timeout=2.0)
                return FakeResult(self.samples, self.sample_rate)

        sp._tts_factory = lambda model_dir, **_: BlockingTts()
        worker = threading.Thread(target=lambda: sp.speak("好的"))
        worker.start()
        self.assertTrue(entered.wait(timeout=1.0))
        self.assertTrue(sp.stop())
        block.set()
        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())
        # Synthesis was superseded before begin()/play(), so no audio was emitted.
        self.assertEqual(player.begins, 0)
        self.assertEqual(player.chunks, [])
        sp.close()

    def test_cancel_with_no_active_speech_returns_false(self):
        sp, _, _, _ = speaker(self)
        try:
            self.assertFalse(sp.cancel())
            self.assertFalse(sp.stop())
        finally:
            sp.close()


class CloseTests(unittest.TestCase):
    def test_close_is_idempotent_and_closes_player_once(self):
        sp, _, player, factories = speaker(self)
        sp.speak("好的")  # ensures the player exists
        self.assertEqual(factories["player"], 1)
        sp.close()
        sp.close()
        self.assertEqual(player.closes, 1)

    def test_speak_after_close_raises(self):
        sp, _, _, _ = speaker(self)
        sp.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            sp.speak("好的")

    def test_stop_after_close_returns_false(self):
        sp, _, _, _ = speaker(self)
        sp.close()
        self.assertFalse(sp.stop())


class FallbackSpeakerIntegrationTests(unittest.TestCase):
    """VITS must be usable as the cloud tier of FallbackSpeaker(VITS, SAPI)."""

    def test_vits_failure_falls_through_to_local_sapi(self):
        from voice.tts_local import FallbackSpeaker

        empty = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(empty, ignore_errors=True))
        vits = VitsSpeaker(
            empty,
            tts_factory=lambda d, **_: FakeTts(),
            player_factory=lambda r: FakePlayer(),
        )
        spoken: list[str] = []

        class Sapi:
            def speak(self, text, *, cancellation=None, operation_id=None):
                spoken.append(text)

            def stop(self, *, operation_id=None):
                return True

        chain = FallbackSpeaker(vits, Sapi())
        try:
            # VITS model is missing -> raises -> FallbackSpeaker uses SAPI.
            chain.speak("好的，记事本打开了。")
            self.assertEqual(spoken, ["好的，记事本打开了。"])
            self.assertEqual(chain.last_backend, "local")
        finally:
            chain.close()


if __name__ == "__main__":
    unittest.main()
