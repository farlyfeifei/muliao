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


class MissingModelClassificationTests(unittest.TestCase):
    """Regression guard for the over-broad _is_missing_model_error (HIGH)."""

    def test_real_decode_oserror_does_not_leak_audio_to_cloud(self):
        # A native decode failure surfaces as OSError, but it is NOT a missing
        # model: sending this audio to the cloud would violate the privacy
        # contract. The local error must surface and the cloud must stay idle.
        cloud = Cloud()
        recognizer = FallbackRecognizer(
            Primary(error=OSError("native decode failure")),
            cloud,
            api_key="k",
        )
        with self.assertRaisesRegex(OSError, "native decode failure"):
            recognizer.transcribe(AUDIO)
        self.assertEqual(cloud.calls, 0)

    def test_runtime_error_with_load_like_text_is_not_treated_as_missing(self):
        cloud = Cloud()
        recognizer = FallbackRecognizer(
            Primary(error=RuntimeError("cannot load stream for decode")),
            cloud,
            api_key="k",
        )
        with self.assertRaisesRegex(RuntimeError, "cannot load stream"):
            recognizer.transcribe(AUDIO)
        self.assertEqual(cloud.calls, 0)

    def test_file_not_found_still_falls_back_to_cloud(self):
        cloud = Cloud()
        recognizer = FallbackRecognizer(
            Primary(error=FileNotFoundError("missing SenseVoice assets")),
            cloud,
            api_key="k",
        )
        result = recognizer.transcribe(AUDIO)
        self.assertEqual(result.metadata["provider"], "mimo")
        self.assertEqual(cloud.calls, 1)

    def test_import_error_still_falls_back_to_cloud(self):
        cloud = Cloud()
        recognizer = FallbackRecognizer(
            Primary(error=ImportError("sherpa_onnx not installed")),
            cloud,
            api_key="k",
        )
        result = recognizer.transcribe(AUDIO)
        self.assertEqual(result.metadata["provider"], "mimo")
        self.assertEqual(cloud.calls, 1)


class CloudCancellationBreakerTests(unittest.TestCase):
    """A barge-in during the cloud round-trip must not trip the breaker (MEDIUM)."""

    def test_voice_cancelled_during_cloud_does_not_count_as_failure(self):
        from voice.cancellation import VoiceCancelled

        token = CancellationToken()

        class FailLocal(Primary):
            def transcribe(self, audio, *, cancellation=None):
                raise FileNotFoundError("missing")

        class CancellingCloud(Cloud):
            def transcribe(self, audio, *, cancellation=None):
                self.calls += 1
                raise VoiceCancelled("barge-in")

        recognizer = FallbackRecognizer(
            FailLocal(),
            CancellingCloud(),
            api_key="k",
            breaker_threshold=1,
        )
        with self.assertRaises(VoiceCancelled):
            recognizer.transcribe(AUDIO, cancellation=token)
        # VoiceCancelled propagates; it is NOT recorded as a cloud failure and
        # does NOT open the breaker.
        self.assertEqual(recognizer.stats.cloud_failed, 0)
        self.assertFalse(recognizer.breaker_open)


if __name__ == "__main__":
    unittest.main()
