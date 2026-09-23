from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.audio_player import PyAudioPcmSink
from voice.cancellation import CancellationToken
from voice.config import VoiceSettings
from voice.runtime import build_runtime
from voice.tts_local import FallbackSpeaker, SapiSpeaker


class FakeCloud:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.texts: list[str] = []
        self.cancel_calls = 0
        self.close_calls = 0

    def speak(self, text: str) -> None:
        self.texts.append(text)
        if self.error is not None:
            raise self.error

    def cancel(self) -> None:
        self.cancel_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class FakeLocal:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.stop_calls = 0
        self.close_calls = 0

    def speak(self, text: str) -> None:
        self.texts.append(text)

    def stop(self) -> None:
        self.stop_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class FallbackSpeakerTests(unittest.TestCase):
    def test_cloud_success_does_not_call_local(self):
        cloud, local = FakeCloud(), FakeLocal()
        speaker = FallbackSpeaker(cloud, local)
        speaker.speak("完成")
        self.assertEqual(cloud.texts, ["完成"])
        self.assertEqual(local.texts, [])
        self.assertEqual(speaker.last_backend, "mimo")

    def test_cloud_failure_immediately_calls_local(self):
        cloud, local = FakeCloud(RuntimeError("offline")), FakeLocal()
        speaker = FallbackSpeaker(cloud, local)
        speaker.speak("完成")
        self.assertEqual(local.texts, ["完成"])
        self.assertEqual(speaker.last_backend, "local")
        self.assertIn("offline", speaker.last_error)

    def test_cancelled_operation_never_calls_cloud_or_local(self):
        cloud, local = FakeCloud(RuntimeError("must not run")), FakeLocal()
        speaker = FallbackSpeaker(cloud, local)
        token = CancellationToken()
        token.cancel()
        speaker.speak("stale", cancellation=token, operation_id=9)
        self.assertEqual(cloud.texts, [])
        self.assertEqual(local.texts, [])

    def test_stop_during_cloud_cancellation_error_does_not_start_local(self):
        cloud_entered = threading.Event()
        cancelled = threading.Event()

        class CancelRaisesCloud(FakeCloud):
            def speak(self, text: str) -> None:
                self.texts.append(text)
                cloud_entered.set()
                cancelled.wait(timeout=2.0)
                raise RuntimeError("cancelled")

            def cancel(self) -> None:
                self.cancel_calls += 1
                cancelled.set()

        cloud, local = CancelRaisesCloud(), FakeLocal()
        speaker = FallbackSpeaker(cloud, local)
        thread = threading.Thread(target=lambda: speaker.speak("完成"))
        thread.start()
        self.assertTrue(cloud_entered.wait(timeout=1.0))
        speaker.stop()
        thread.join(timeout=1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(local.texts, [])
        self.assertEqual(local.stop_calls, 1)

    def test_new_speak_epoch_suppresses_old_cloud_fallback(self):
        old_entered = threading.Event()
        allow_old_failure = threading.Event()

        class Cloud(FakeCloud):
            def speak(self, text: str) -> None:
                self.texts.append(text)
                if text == "old":
                    old_entered.set()
                    allow_old_failure.wait(timeout=2.0)
                    raise RuntimeError("old failed")

        cloud, local = Cloud(), FakeLocal()
        speaker = FallbackSpeaker(cloud, local)
        old = threading.Thread(target=lambda: speaker.speak("old"))
        old.start()
        self.assertTrue(old_entered.wait(timeout=1.0))
        speaker.speak("new")
        allow_old_failure.set()
        old.join(timeout=1.0)
        self.assertEqual(local.texts, [])
        self.assertEqual(speaker.last_backend, "mimo")

    def test_stop_and_close_reach_both_backends(self):
        cloud, local = FakeCloud(), FakeLocal()
        speaker = FallbackSpeaker(cloud, local)
        speaker.stop()
        speaker.close()
        self.assertEqual(cloud.cancel_calls, 1)
        self.assertEqual(cloud.close_calls, 1)
        self.assertEqual(local.stop_calls, 1)
        self.assertEqual(local.close_calls, 1)


class SapiSpeakerThreadingTests(unittest.TestCase):
    def test_com_voice_speak_purge_and_uninitialize_stay_on_owner_thread(self):
        calls: list[tuple[str, int, str | int | None]] = []
        owner: list[int] = []

        class ThreadAffineVoice:
            def __init__(self) -> None:
                self.thread_id = threading.get_ident()

            def Speak(self, text: str, flags: int = 0) -> None:
                self._assert_owner()
                calls.append(("speak", threading.get_ident(), f"{text}|{flags}"))

            def _assert_owner(self) -> None:
                if threading.get_ident() != self.thread_id:
                    raise AssertionError("SAPI object used from non-owner thread")

        def initialize() -> None:
            owner.append(threading.get_ident())
            calls.append(("init", threading.get_ident(), None))

        def dispatch(name: str):
            self.assertEqual(name, "SAPI.SpVoice")
            calls.append(("dispatch", threading.get_ident(), name))
            return ThreadAffineVoice()

        def uninitialize() -> None:
            calls.append(("uninit", threading.get_ident(), None))

        speaker = SapiSpeaker(
            dispatch=dispatch,
            com_initialize=initialize,
            com_uninitialize=uninitialize,
        )
        speaker.speak("hello")
        speaker.stop()
        speaker.close()
        self.assertTrue(owner)
        self.assertTrue(all(call[1] == owner[0] for call in calls))
        spoken = [call[2] for call in calls if call[0] == "speak"]
        self.assertEqual(spoken, ["hello|1", "|3", "|3"])

    def test_cancelled_token_is_checked_before_sapi_submission(self):
        spoken: list[str] = []

        class Voice:
            def Speak(self, text: str, flags: int = 0) -> None:
                spoken.append(text)

        token = CancellationToken()
        token.cancel()
        speaker = SapiSpeaker(dispatch=lambda name: Voice())
        try:
            speaker.speak("stale", cancellation=token, operation_id=7)
            self.assertEqual(spoken, [])
        finally:
            speaker.close()

    def test_owner_rechecks_token_after_command_was_queued(self):
        block_entered = threading.Event()
        release_block = threading.Event()
        spoken: list[str] = []

        class Voice:
            def Speak(self, text: str, flags: int = 0) -> None:
                if text == "block":
                    block_entered.set()
                    release_block.wait(timeout=2.0)
                spoken.append(text)

        speaker = SapiSpeaker(dispatch=lambda name: Voice())
        blocker = threading.Thread(target=lambda: speaker.speak("block"))
        blocker.start()
        self.assertTrue(block_entered.wait(timeout=1.0))
        token = CancellationToken()
        stale_done = threading.Event()
        stale = threading.Thread(
            target=lambda: (
                speaker.speak("stale", cancellation=token, operation_id=8),
                stale_done.set(),
            )
        )
        stale.start()
        token.cancel()
        release_block.set()
        blocker.join(timeout=1.0)
        self.assertTrue(stale_done.wait(timeout=1.0))
        stale.join(timeout=1.0)
        speaker.close()
        self.assertNotIn("stale", spoken)


class FakeOutputStream:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.stopped = False
        self.start_calls = 0
        self.stop_calls = 0
        self.close_calls = 0

    def write(self, pcm: bytes) -> None:
        self.writes.append(bytes(pcm))

    def is_stopped(self) -> bool:
        return self.stopped

    def start_stream(self) -> None:
        self.stopped = False
        self.start_calls += 1

    def stop_stream(self) -> None:
        self.stopped = True
        self.stop_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class FakePyAudio:
    def __init__(self) -> None:
        self.stream = FakeOutputStream()
        self.open_kwargs = None
        self.terminate_calls = 0

    def open(self, **kwargs):
        self.open_kwargs = kwargs
        return self.stream

    def terminate(self) -> None:
        self.terminate_calls += 1


class PyAudioSinkTests(unittest.TestCase):
    def test_stopped_stream_restarts_on_next_write_and_close_releases(self):
        audio = FakePyAudio()
        sink = PyAudioPcmSink(pyaudio_factory=lambda: audio)
        sink.write(b"a")
        sink.stop()
        sink.write(b"b")
        sink.close()
        self.assertEqual(audio.stream.writes, [b"a", b"b"])
        self.assertEqual(audio.stream.start_calls, 1)
        self.assertGreaterEqual(audio.stream.stop_calls, 2)
        self.assertEqual(audio.stream.close_calls, 1)
        self.assertEqual(audio.terminate_calls, 1)
        self.assertEqual(audio.open_kwargs["rate"], 24_000)
        self.assertEqual(audio.open_kwargs["channels"], 1)


class RuntimeBuildTests(unittest.TestCase):
    @staticmethod
    def settings(**changes):
        base = VoiceSettings(
            sensevoice_dir=Path("C:/models/sensevoice"),
            jev_url="https://example.test/systemone",
            jev_key="jev-test",
            jev_model="jev-latest",
            mimo_base_url="https://api.xiaomimimo.com/v1",
            mimo_api_key="",
            mimo_tts_model="mimo-v2.5-tts",
            mimo_tts_voice="冰糖",
        )
        return replace(base, **changes)

    def test_no_mimo_key_builds_local_only_speaker(self):
        with mock.patch("voice.runtime.SapiSpeaker") as local_cls:
            local = local_cls.return_value
            engine, resources = build_runtime(self.settings(), speak=True)
            try:
                self.assertIs(engine.speaker, local)
                self.assertIsNone(resources.player)
                self.assertIsNone(resources.sink)
            finally:
                resources.close()

    def test_mimo_key_builds_fallback_without_exposing_key(self):
        with (
            mock.patch("voice.runtime.SapiSpeaker") as local_cls,
            mock.patch("voice.runtime.PyAudioPcmSink") as sink_cls,
            mock.patch("voice.runtime.CancellableAudioPlayer") as player_cls,
            mock.patch("voice.runtime.MiMoTtsClient") as cloud_cls,
        ):
            engine, resources = build_runtime(
                self.settings(mimo_api_key="test-only", mimo_tts_enabled=True),
                speak=True,
            )
            try:
                self.assertIsInstance(engine.speaker, FallbackSpeaker)
                cloud_cls.assert_called_once()
                kwargs = cloud_cls.call_args.kwargs
                self.assertEqual(kwargs["api_key"], "test-only")
                self.assertEqual(kwargs["voice"], "冰糖")
                self.assertIs(resources.sink, sink_cls.return_value)
                self.assertIs(resources.player, player_cls.return_value)
            finally:
                resources.close()


if __name__ == "__main__":
    unittest.main()
