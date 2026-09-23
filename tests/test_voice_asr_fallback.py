from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.asr_fallback import FallbackRecognizer
from voice.cancellation import CancellationToken, VoiceCancelled
from voice.contracts import AudioSegment, Transcript


AUDIO = AudioSegment(b"\0\0" * 160)


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Primary:
    def __init__(self, *, result=None, error=None):
        self.result = result or Transcript("打开记事本", metadata={"provider": "local"})
        self.error = error
        self.calls = 0

    def transcribe(self, audio, *, cancellation=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class LegacyPrimary:
    """Old recognizer without a cancellation keyword."""

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        return self.result


class Cloud:
    def __init__(self, *, result=None, error=None):
        self.result = result or Transcript("打开记事本", metadata={"provider": "mimo"})
        self.error = error
        self.calls = 0

    def transcribe(self, audio, *, cancellation=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class LocalPrimaryTests(unittest.TestCase):
    def test_local_success_returns_without_touching_cloud(self):
        primary, cloud = Primary(), Cloud()
        recognizer = FallbackRecognizer(primary, cloud, api_key="k")
        result = recognizer.transcribe(AUDIO)
        self.assertEqual(result.metadata["provider"], "local")
        self.assertEqual(cloud.calls, 0)
        self.assertEqual(recognizer.stats.local_ok, 1)

    def test_legacy_primary_without_cancellation_keyword_works(self):
        primary = LegacyPrimary(Transcript("旧接口"))
        recognizer = FallbackRecognizer(primary, None, api_key="")
        self.assertEqual(recognizer.transcribe(AUDIO).text, "旧接口")

    def test_cancelled_before_transcribe_raises(self):
        token = CancellationToken()
        token.cancel()
        recognizer = FallbackRecognizer(Primary(), None, api_key="")
        with self.assertRaises(VoiceCancelled):
            recognizer.transcribe(AUDIO, cancellation=token)


class CloudFallbackTests(unittest.TestCase):
    def test_missing_local_model_falls_back_to_cloud(self):
        cloud = Cloud()
        recognizer = FallbackRecognizer(
            Primary(error=FileNotFoundError("missing SenseVoice assets")),
            cloud,
            api_key="k",
        )
        result = recognizer.transcribe(AUDIO)
        self.assertEqual(result.metadata["provider"], "mimo")
        self.assertEqual(cloud.calls, 1)
        self.assertEqual(recognizer.stats.cloud_ok, 1)

    def test_non_missing_local_error_is_not_sent_to_cloud(self):
        # A genuine decode error (not a missing model) must surface, not leak
        # audio to the cloud.
        cloud = Cloud()
        recognizer = FallbackRecognizer(
            Primary(error=ValueError("PCM byte length must align")),
            cloud,
            api_key="k",
        )
        with self.assertRaisesRegex(ValueError, "align"):
            recognizer.transcribe(AUDIO)
        self.assertEqual(cloud.calls, 0)

    def test_no_api_key_disables_cloud_and_raises_local_error(self):
        cloud = Cloud()
        recognizer = FallbackRecognizer(
            Primary(error=FileNotFoundError("missing")),
            cloud,
            api_key="",
        )
        with self.assertRaises(FileNotFoundError):
            recognizer.transcribe(AUDIO)
        self.assertEqual(cloud.calls, 0)
        self.assertEqual(recognizer.stats.cloud_disabled, 1)

    def test_no_fallback_configured_raises_local_error(self):
        recognizer = FallbackRecognizer(
            Primary(error=FileNotFoundError("missing")), None, api_key="k"
        )
        with self.assertRaises(FileNotFoundError):
            recognizer.transcribe(AUDIO)
        self.assertEqual(recognizer.stats.cloud_disabled, 1)

    def test_cloud_failure_raises_local_error_never_empty_transcript(self):
        cloud = Cloud(error=RuntimeError("429 rate limited"))
        recognizer = FallbackRecognizer(
            Primary(error=FileNotFoundError("missing")),
            cloud,
            api_key="k",
        )
        # The original local error surfaces; Jev never receives an empty string.
        with self.assertRaises(FileNotFoundError):
            recognizer.transcribe(AUDIO)
        self.assertEqual(recognizer.stats.cloud_failed, 1)

    def test_cancelled_between_local_and_cloud_raises(self):
        token = CancellationToken()
        cloud = Cloud()

        class CancelOnFailPrimary(Primary):
            def transcribe(self, audio, *, cancellation=None):
                token.cancel()
                raise FileNotFoundError("missing")

        recognizer = FallbackRecognizer(CancelOnFailPrimary(), cloud, api_key="k")
        with self.assertRaises(VoiceCancelled):
            recognizer.transcribe(AUDIO, cancellation=token)
        self.assertEqual(cloud.calls, 0)


class BreakerTests(unittest.TestCase):
    def test_breaker_opens_after_threshold_and_skips_cloud(self):
        clock = Clock()
        cloud = Cloud(error=RuntimeError("down"))
        recognizer = FallbackRecognizer(
            Primary(error=FileNotFoundError("missing")),
            cloud,
            api_key="k",
            breaker_threshold=2,
            breaker_reset_seconds=30.0,
            clock=clock,
        )
        for _ in range(2):
            with self.assertRaises(FileNotFoundError):
                recognizer.transcribe(AUDIO)
        self.assertTrue(recognizer.breaker_open)
        self.assertEqual(cloud.calls, 2)

        # While open, cloud is not probed again.
        with self.assertRaises(FileNotFoundError):
            recognizer.transcribe(AUDIO)
        self.assertEqual(cloud.calls, 2)
        self.assertEqual(recognizer.stats.breaker_open, 1)

    def test_breaker_half_opens_after_reset_window(self):
        clock = Clock()
        cloud = Cloud(error=RuntimeError("down"))
        recognizer = FallbackRecognizer(
            Primary(error=FileNotFoundError("missing")),
            cloud,
            api_key="k",
            breaker_threshold=1,
            breaker_reset_seconds=30.0,
            clock=clock,
        )
        with self.assertRaises(FileNotFoundError):
            recognizer.transcribe(AUDIO)
        self.assertTrue(recognizer.breaker_open)
        self.assertEqual(cloud.calls, 1)

        clock.advance(31.0)
        with self.assertRaises(FileNotFoundError):
            recognizer.transcribe(AUDIO)
        self.assertEqual(cloud.calls, 2, "half-open probe should reach cloud once")

    def test_cloud_success_resets_breaker(self):
        clock = Clock()
        cloud = Cloud(error=RuntimeError("down"))
        recognizer = FallbackRecognizer(
            Primary(error=FileNotFoundError("missing")),
            cloud,
            api_key="k",
            breaker_threshold=1,
            breaker_reset_seconds=30.0,
            clock=clock,
        )
        with self.assertRaises(FileNotFoundError):
            recognizer.transcribe(AUDIO)
        self.assertTrue(recognizer.breaker_open)
        clock.advance(31.0)
        cloud.error = None  # probe now succeeds
        result = recognizer.transcribe(AUDIO)
        self.assertEqual(result.metadata["provider"], "mimo")
        self.assertFalse(recognizer.breaker_open)


class CloseTests(unittest.TestCase):
    def test_close_reaches_both_recognizers_once(self):
        closed = []

        class Closable:
            def __init__(self, name):
                self.name = name

            def transcribe(self, audio, *, cancellation=None):
                return Transcript("x")

            def close(self):
                closed.append(self.name)

        primary, cloud = Closable("primary"), Closable("cloud")
        recognizer = FallbackRecognizer(primary, cloud, api_key="k")
        recognizer.close()
        self.assertEqual(sorted(closed), ["cloud", "primary"])

    def test_close_swallows_errors(self):
        class Boom:
            def transcribe(self, audio, *, cancellation=None):
                return Transcript("x")

            def close(self):
                raise RuntimeError("close failed")

        recognizer = FallbackRecognizer(Boom(), None, api_key="")
        recognizer.close()  # must not raise


if __name__ == "__main__":
    unittest.main()
