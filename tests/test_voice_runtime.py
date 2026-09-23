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
from voice.contracts import RouteDecision
from voice.events import MemoryEventSink
from voice.runtime import VoiceRuntimeResources, build_capture, build_runtime
from voice.safety import EchoAwareSpeaker
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


    def test_stop_during_local_fallback_playback_is_not_blocked_by_epoch_lock(self):
        local_entered = threading.Event()
        release_local = threading.Event()

        class Cloud:
            def speak(self, text: str) -> None:
                raise RuntimeError("cloud offline")

            def cancel(self) -> None:
                return None

        class Local:
            def __init__(self) -> None:
                self.stop_calls = 0

            def speak(self, text: str, *, cancellation=None, operation_id=None) -> None:
                local_entered.set()
                release_local.wait(timeout=2.0)

            def stop(self, *, operation_id=None) -> None:
                self.stop_calls += 1
                release_local.set()

        local = Local()
        speaker = FallbackSpeaker(Cloud(), local)
        worker = threading.Thread(target=lambda: speaker.speak("完成"))
        worker.start()
        self.assertTrue(local_entered.wait(timeout=1.0))

        stop_returned = threading.Event()
        stopper = threading.Thread(target=lambda: (speaker.stop(), stop_returned.set()))
        stopper.start()
        self.assertTrue(stop_returned.wait(timeout=1.0))

        release_local.set()
        worker.join(timeout=1.0)
        stopper.join(timeout=1.0)
        self.assertEqual(local.stop_calls, 1)


class SapiSpeakerThreadingTests(unittest.TestCase):
    @staticmethod
    def wait_for_queue_size(
        speaker: SapiSpeaker,
        expected: int,
        timeout: float = 1.0,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if speaker._commands.qsize() == expected:
                return True
            time.sleep(0.005)
        return speaker._commands.qsize() == expected

    def test_close_purge_error_still_exits_owner_when_playback_finishes_first(self):
        wait_entered = threading.Event()
        playback_done = threading.Event()

        class Voice:
            def Speak(self, text: str, flags: int = 0) -> None:
                if not text and flags == 3:
                    raise RuntimeError("purge failed")

            def WaitUntilDone(self, timeout_ms: int) -> bool:
                wait_entered.set()
                return playback_done.wait(timeout=2.0)

        speaker = SapiSpeaker(dispatch=lambda name: Voice())
        speak_done = threading.Event()
        close_done = threading.Event()
        close_errors: list[BaseException] = []
        speaker_thread = threading.Thread(
            target=lambda: (speaker.speak("播放中"), speak_done.set()),
            daemon=True,
        )
        speaker_thread.start()
        self.assertTrue(wait_entered.wait(timeout=1.0))

        def close() -> None:
            try:
                speaker.close()
            except BaseException as exc:
                close_errors.append(exc)
            finally:
                close_done.set()

        closer = threading.Thread(target=close, daemon=True)
        closer.start()
        self.assertTrue(self.wait_for_queue_size(speaker, 1))
        playback_done.set()

        self.assertTrue(close_done.wait(timeout=1.0))
        self.assertTrue(speak_done.wait(timeout=1.0))
        speaker_thread.join(timeout=1.0)
        closer.join(timeout=1.0)
        self.assertFalse(speaker_thread.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertFalse(speaker._thread.is_alive())
        self.assertEqual([str(exc) for exc in close_errors], ["purge failed"])
        self.assertEqual(speaker._commands.unfinished_tasks, 0)

    def test_deferred_speak_is_completed_when_stop_and_close_are_queued(self):
        first_wait_entered = threading.Event()
        release_first_wait = threading.Event()
        second_wait_entered = threading.Event()
        release_second_wait = threading.Event()
        wait_calls = 0
        wait_lock = threading.Lock()

        class Voice:
            def Speak(self, text: str, flags: int = 0) -> None:
                return None

            def WaitUntilDone(self, timeout_ms: int) -> bool:
                nonlocal wait_calls
                with wait_lock:
                    wait_calls += 1
                    call = wait_calls
                if call == 1:
                    first_wait_entered.set()
                    release_first_wait.wait(timeout=2.0)
                    return False
                second_wait_entered.set()
                return release_second_wait.wait(timeout=2.0)

        speaker = SapiSpeaker(dispatch=lambda name: Voice())
        active_done = threading.Event()
        deferred_done = threading.Event()
        stop_done = threading.Event()
        close_done = threading.Event()
        deferred_errors: list[BaseException] = []
        close_errors: list[BaseException] = []

        active = threading.Thread(
            target=lambda: (speaker.speak("active"), active_done.set()),
            daemon=True,
        )
        active.start()
        self.assertTrue(first_wait_entered.wait(timeout=1.0))

        def deferred_speak() -> None:
            try:
                speaker.speak("deferred")
            except BaseException as exc:
                deferred_errors.append(exc)
            finally:
                deferred_done.set()

        deferred = threading.Thread(target=deferred_speak, daemon=True)
        deferred.start()
        self.assertTrue(self.wait_for_queue_size(speaker, 1))
        release_first_wait.set()
        self.assertTrue(second_wait_entered.wait(timeout=1.0))

        stopper = threading.Thread(
            target=lambda: (speaker.stop(), stop_done.set()),
            daemon=True,
        )
        stopper.start()
        self.assertTrue(self.wait_for_queue_size(speaker, 1))

        def close() -> None:
            try:
                speaker.close()
            except BaseException as exc:
                close_errors.append(exc)
            finally:
                close_done.set()

        closer = threading.Thread(target=close, daemon=True)
        closer.start()
        self.assertTrue(self.wait_for_queue_size(speaker, 2))
        release_second_wait.set()

        for done in (active_done, stop_done, close_done, deferred_done):
            self.assertTrue(done.wait(timeout=1.0))
        for worker in (active, deferred, stopper, closer):
            worker.join(timeout=1.0)
            self.assertFalse(worker.is_alive())
        self.assertFalse(speaker._thread.is_alive())
        self.assertEqual(close_errors, [])
        self.assertEqual(len(deferred_errors), 1)
        self.assertIn("closed", str(deferred_errors[0]))
        self.assertEqual(speaker._commands.unfinished_tasks, 0)

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
    def test_speak_waits_until_real_playback_finishes(self):
        playback_started = threading.Event()
        playback_done = threading.Event()

        class Voice:
            def Speak(self, text: str, flags: int = 0) -> None:
                if text:
                    playback_started.set()

            def WaitUntilDone(self, timeout_ms: int) -> bool:
                return playback_done.wait(timeout_ms / 1000.0)

        speaker = SapiSpeaker(dispatch=lambda name: Voice())
        finished = threading.Event()
        worker = threading.Thread(
            target=lambda: (speaker.speak("仍在播放", operation_id=11), finished.set()),
            daemon=True,
        )
        worker.start()
        try:
            self.assertTrue(playback_started.wait(timeout=1.0))
            self.assertFalse(finished.wait(timeout=0.1))
            playback_done.set()
            self.assertTrue(finished.wait(timeout=1.0))
            worker.join(timeout=1.0)
            self.assertFalse(worker.is_alive())
        finally:
            playback_done.set()
            speaker.close()

    def test_stop_preempts_active_playback_and_unblocks_speak(self):
        playback_started = threading.Event()
        purge_seen = threading.Event()

        class Voice:
            def Speak(self, text: str, flags: int = 0) -> None:
                if text:
                    playback_started.set()
                elif flags == 3:
                    purge_seen.set()

            def WaitUntilDone(self, timeout_ms: int) -> bool:
                time.sleep(timeout_ms / 1000.0)
                return False

        speaker = SapiSpeaker(dispatch=lambda name: Voice())
        worker = threading.Thread(
            target=lambda: speaker.speak("需要急停", operation_id=12),
            daemon=True,
        )
        worker.start()
        try:
            self.assertTrue(playback_started.wait(timeout=1.0))
            self.assertTrue(speaker.stop(operation_id=12))
            self.assertTrue(purge_seen.wait(timeout=1.0))
            worker.join(timeout=1.0)
            self.assertFalse(worker.is_alive())
        finally:
            speaker.close()

    def test_close_preempts_active_playback_and_joins_owner_thread(self):
        playback_started = threading.Event()
        purge_seen = threading.Event()

        class Voice:
            def Speak(self, text: str, flags: int = 0) -> None:
                if text:
                    playback_started.set()
                elif flags == 3:
                    purge_seen.set()

            def WaitUntilDone(self, timeout_ms: int) -> bool:
                time.sleep(timeout_ms / 1000.0)
                return False

        speaker = SapiSpeaker(dispatch=lambda name: Voice())
        worker = threading.Thread(
            target=lambda: speaker.speak("关闭时停止", operation_id=13),
            daemon=True,
        )
        worker.start()
        self.assertTrue(playback_started.wait(timeout=1.0))

        speaker.close()

        self.assertTrue(purge_seen.wait(timeout=1.0))
        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())
        self.assertFalse(speaker._thread.is_alive())


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

    def test_resource_close_attempts_every_backend_and_aggregates_failures(self):
        calls: list[str] = []

        class Resource:
            def __init__(self, name: str, *, fail: bool = False) -> None:
                self.name = name
                self.fail = fail

            def close(self) -> None:
                calls.append(self.name)
                if self.fail:
                    raise RuntimeError(f"{self.name} failed")

        resources = VoiceRuntimeResources(
            router=Resource("router", fail=True),
            speaker=Resource("speaker", fail=True),
            player=Resource("player", fail=True),
            sink=Resource("sink"),
        )

        with self.assertRaises(ExceptionGroup) as raised:
            resources.close()

        self.assertEqual(calls, ["speaker", "router", "player", "sink"])
        self.assertEqual(len(raised.exception.exceptions), 3)

    def test_no_mimo_key_builds_echo_aware_local_speaker(self):
        with mock.patch("voice.runtime.SapiSpeaker") as local_cls:
            local = local_cls.return_value
            engine, resources = build_runtime(self.settings(), speak=True)
            try:
                self.assertIsInstance(engine.speaker, EchoAwareSpeaker)
                self.assertIs(engine.speaker.speaker, local)
                self.assertIs(engine.echo_guard, resources.echo_guard)
                self.assertIs(engine.speaker.guard, resources.echo_guard)
                self.assertIsNone(resources.player)
                self.assertIsNone(resources.sink)
            finally:
                resources.close()

    def test_mimo_key_builds_echo_aware_fallback_without_exposing_key(self):
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
                self.assertIsInstance(engine.speaker, EchoAwareSpeaker)
                self.assertIsInstance(engine.speaker.speaker, FallbackSpeaker)
                self.assertIs(engine.echo_guard, resources.echo_guard)
                self.assertIs(engine.speaker.guard, resources.echo_guard)
                cloud_cls.assert_called_once()
                kwargs = cloud_cls.call_args.kwargs
                self.assertEqual(kwargs["api_key"], "test-only")
                self.assertEqual(kwargs["voice"], "冰糖")
                self.assertIs(resources.sink, sink_cls.return_value)
                self.assertIs(resources.player, player_cls.return_value)
            finally:
                resources.close()

    def test_transcript_only_execution_emits_no_tts_events(self):
        engine, resources = build_runtime(
            self.settings(),
            speak=False,
            enable_asr=False,
        )
        events = MemoryEventSink()
        engine.events = events
        engine.router.route = mock.Mock(
            return_value=RouteDecision(
                accepted=True,
                kind="open_app",
                target="notepad",
                confidence=0.99,
            )
        )
        with mock.patch.object(engine.permission, "allowed", return_value=True), mock.patch.object(
            engine.permission,
            "run_if_allowed",
            side_effect=lambda callback: (True, callback()),
        ):
            try:
                result = engine.process_transcript("幕僚幕僚，打开记事本")
            finally:
                resources.close()

        self.assertEqual(result.status, "executed")
        self.assertEqual(result.detail, "tts_skipped_disabled")
        self.assertFalse(any(event.type == "voice.tts" for event in events.events))

    def test_capture_uses_runtime_echo_guard_gate_and_frame_consumer(self):
        consumer = mock.Mock()
        with mock.patch("voice.runtime.SapiSpeaker"):
            engine, resources = build_runtime(self.settings(), speak=True)
        try:
            capture = build_capture(
                self.settings(),
                echo_guard=resources.echo_guard,
                frame_consumer=consumer,
            )
            self.assertIs(capture.mute_gate, resources.echo_guard.mute_gate)
            self.assertIs(capture.frame_consumer, consumer)
            self.assertIs(engine.echo_guard, resources.echo_guard)
        finally:
            resources.close()

    def test_transcript_only_runtime_skips_asr_echo_and_tts_events(self):
        with (
            mock.patch("voice.runtime.SenseVoiceRecognizer") as recognizer_cls,
            mock.patch("voice.runtime.SapiSpeaker") as sapi_cls,
        ):
            engine, resources = build_runtime(
                self.settings(),
                speak=False,
                enable_asr=False,
            )
            try:
                recognizer_cls.assert_not_called()
                sapi_cls.assert_not_called()
                self.assertIsNone(engine.echo_guard)
                self.assertIsNone(resources.echo_guard)
                self.assertFalse(engine.speaker.enabled)
                with self.assertRaisesRegex(RuntimeError, "disabled"):
                    engine.recognizer.transcribe(None)
            finally:
                resources.close()


class RecognizerFallbackWiringTests(unittest.TestCase):
    @staticmethod
    def settings(**changes):
        base = VoiceSettings(
            sensevoice_dir=Path("C:/models/sensevoice"),
            jev_url="https://example.test/systemone",
            jev_key="jev-test",
            jev_model="jev-latest",
            mimo_base_url="https://api.xiaomimimo.com/v1",
            mimo_api_key="",
        )
        return replace(base, **changes)

    def test_disabled_asr_builds_transcript_only_recognizer(self):
        with mock.patch("voice.runtime.SenseVoiceRecognizer") as sense_cls, \
                mock.patch("voice.runtime.SapiSpeaker"):
            _, resources = build_runtime(self.settings(), speak=False, enable_asr=False)
        try:
            sense_cls.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                resources.recognizer.transcribe(None)
        finally:
            resources.close()

    def test_cloud_asr_off_by_default_builds_plain_local_recognizer(self):
        with mock.patch("voice.runtime.SenseVoiceRecognizer") as sense_cls, \
                mock.patch("voice.runtime.MiMoAsrClient") as cloud_cls, \
                mock.patch("voice.runtime.SapiSpeaker"):
            _, resources = build_runtime(
                # Even with a key present, the explicit opt-in flag is False.
                self.settings(mimo_api_key="test-only", mimo_asr_enabled=False),
                speak=False,
            )
        try:
            cloud_cls.assert_not_called()
            self.assertIs(resources.recognizer, sense_cls.return_value)
        finally:
            resources.close()

    def test_cloud_asr_enabled_without_key_stays_local_only(self):
        with mock.patch("voice.runtime.SenseVoiceRecognizer") as sense_cls, \
                mock.patch("voice.runtime.MiMoAsrClient") as cloud_cls, \
                mock.patch("voice.runtime.SapiSpeaker"):
            _, resources = build_runtime(
                self.settings(mimo_api_key="", mimo_asr_enabled=True),
                speak=False,
            )
        try:
            cloud_cls.assert_not_called()
            self.assertIs(resources.recognizer, sense_cls.return_value)
        finally:
            resources.close()

    def test_cloud_asr_enabled_with_key_wraps_local_in_fallback(self):
        from voice.asr_fallback import FallbackRecognizer

        with mock.patch("voice.runtime.SenseVoiceRecognizer") as sense_cls, \
                mock.patch("voice.runtime.MiMoAsrClient") as cloud_cls, \
                mock.patch("voice.runtime.SapiSpeaker"):
            _, resources = build_runtime(
                self.settings(mimo_api_key="test-only", mimo_asr_enabled=True),
                speak=False,
            )
        try:
            cloud_cls.assert_called_once()
            self.assertEqual(cloud_cls.call_args.kwargs["api_key"], "test-only")
            self.assertIsInstance(resources.recognizer, FallbackRecognizer)
            self.assertIs(resources.recognizer.primary, sense_cls.return_value)
            self.assertIs(resources.recognizer.fallback, cloud_cls.return_value)
        finally:
            resources.close()

    def test_resource_close_closes_the_wrapped_cloud_client(self):
        from voice.asr_fallback import FallbackRecognizer

        closed: list[str] = []

        class Closable:
            def __init__(self, name):
                self.name = name

            def close(self):
                closed.append(self.name)

        local, cloud = Closable("local"), Closable("cloud")
        resources = VoiceRuntimeResources(
            router=None,
            speaker=Closable("speaker"),
            recognizer=FallbackRecognizer(local, cloud, api_key="k"),
        )
        resources.close()
        self.assertEqual(sorted(closed), ["cloud", "local", "speaker"])


class FallbackGetRecognizerTests(unittest.TestCase):
    def test_get_recognizer_delegates_to_primary_local_loader(self):
        from voice.asr_fallback import FallbackRecognizer

        sentinel = object()

        class Primary:
            def _get_recognizer(self):
                return sentinel

        wrapper = FallbackRecognizer(Primary(), None, api_key="")
        self.assertIs(wrapper._get_recognizer(), sentinel)

    def test_get_recognizer_raises_when_primary_lacks_loader(self):
        from voice.asr_fallback import FallbackRecognizer

        class Bare:
            pass

        wrapper = FallbackRecognizer(Bare(), None, api_key="")
        with self.assertRaises(AttributeError):
            wrapper._get_recognizer()


class VitsDegradationChainTests(unittest.TestCase):
    @staticmethod
    def settings(**changes):
        base = VoiceSettings(
            sensevoice_dir=Path("C:/models/sensevoice"),
            jev_url="https://example.test/systemone",
            jev_key="jev-test",
            jev_model="jev-latest",
            mimo_base_url="https://api.xiaomimimo.com/v1",
            mimo_api_key="",
        )
        return replace(base, **changes)

    def test_vits_disabled_by_default_keeps_plain_sapi(self):
        with mock.patch("voice.runtime.SapiSpeaker") as sapi_cls, \
                mock.patch("voice.runtime.VitsSpeaker") as vits_cls:
            _, resources = build_runtime(self.settings(), speak=True)
        try:
            vits_cls.assert_not_called()
            self.assertIs(resources.speaker.speaker, sapi_cls.return_value)
        finally:
            resources.close()

    def test_vits_enabled_wraps_vits_then_sapi(self):
        from voice.tts_local import FallbackSpeaker

        with mock.patch("voice.runtime.SapiSpeaker") as sapi_cls, \
                mock.patch("voice.runtime.VitsSpeaker") as vits_cls:
            _, resources = build_runtime(
                self.settings(vits_tts_enabled=True), speak=True
            )
        try:
            vits_cls.assert_called_once()
            inner = resources.speaker.speaker  # EchoAwareSpeaker -> FallbackSpeaker
            self.assertIsInstance(inner, FallbackSpeaker)
            self.assertIs(inner.cloud, vits_cls.return_value)
            self.assertIs(inner.local, sapi_cls.return_value)
        finally:
            resources.close()

    def test_full_chain_is_mimo_then_vits_then_sapi(self):
        from voice.tts_local import FallbackSpeaker

        with (
            mock.patch("voice.runtime.SapiSpeaker") as sapi_cls,
            mock.patch("voice.runtime.VitsSpeaker") as vits_cls,
            mock.patch("voice.runtime.PyAudioPcmSink"),
            mock.patch("voice.runtime.CancellableAudioPlayer"),
            mock.patch("voice.runtime.MiMoTtsClient") as cloud_cls,
        ):
            _, resources = build_runtime(
                self.settings(
                    mimo_api_key="test-only",
                    mimo_tts_enabled=True,
                    vits_tts_enabled=True,
                ),
                speak=True,
            )
        try:
            outer = resources.speaker.speaker
            self.assertIsInstance(outer, FallbackSpeaker)
            self.assertIs(outer.cloud, cloud_cls.return_value)  # MiMo first
            inner = outer.local
            self.assertIsInstance(inner, FallbackSpeaker)
            self.assertIs(inner.cloud, vits_cls.return_value)  # then VITS
            self.assertIs(inner.local, sapi_cls.return_value)  # then SAPI
        finally:
            resources.close()

    def test_resource_close_reaches_vits_through_the_chain(self):
        from voice.tts_local import FallbackSpeaker

        with mock.patch("voice.runtime.SapiSpeaker"), \
                mock.patch("voice.runtime.VitsSpeaker") as vits_cls:
            _, resources = build_runtime(
                self.settings(vits_tts_enabled=True), speak=True
            )
            resources.close()
        vits_cls.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
