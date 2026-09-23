from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.api import get_voice_service, router
from voice.cancellation import CancellationToken, VoiceCancelled
from voice.captions import AsyncCaptionPump
from voice.contracts import VoiceResult
from voice.service import (
    VoiceEventHub,
    VoicePermissionDenied,
    VoiceRuntime,
    VoiceService,
    _CaptionedCapture,
    _default_runtime_factory,
)


class Permission:
    def __init__(self, allowed: bool) -> None:
        self.enabled = allowed
        self.calls = 0
        self._lock = threading.RLock()

    def allowed(self) -> bool:
        with self._lock:
            self.calls += 1
            return self.enabled

    def run_if_allowed(self, callback):
        with self._lock:
            self.calls += 1
            if not self.enabled:
                return False, None
            return True, callback()

    def revoke(self) -> None:
        with self._lock:
            self.enabled = False


class BlockingCapture:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.released = threading.Event()
        self.calls = 0
        self.stop_calls = 0
        self.close_calls = 0

    def capture_utterance(self):
        self.calls += 1
        self.entered.set()
        if not self.released.wait(timeout=2):
            raise TimeoutError("test capture did not stop")
        return object()

    def stop(self) -> None:
        self.stop_calls += 1
        self.released.set()

    def close(self) -> None:
        self.close_calls += 1
        self.released.set()


class FakeEngine:
    def __init__(self, *, command_status: str = "executed") -> None:
        self.events = None
        self.command_status = command_status
        self.audio_calls = 0
        self.commands: list[str] = []
        self.cancel_calls = 0
        self.close_calls = 0

    def process_audio(self, audio):
        self.audio_calls += 1
        return VoiceResult(status="executed", command="打开记事本")

    def process_transcript(self, text: str):
        self.commands.append(text)
        if self.events is not None:
            self.events.emit(
                "voice.decision",
                {
                    "accepted": True,
                    "kind": "open_app",
                    "target": "notepad",
                    "confidence": 0.99,
                    "command": text,
                },
            )
        return VoiceResult(status=self.command_status, command=text)

    def request_cancel(self) -> None:
        self.cancel_calls += 1

    def cancel_current(self) -> None:
        self.cancel_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class Closable:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class FakeWatcher:
    def __init__(self, callback, *, start_error: Exception | None = None) -> None:
        self.callback = callback
        self.start_error = start_error
        self.start_calls = 0
        self.close_calls = 0
        self.started = threading.Event()

    def start(self) -> bool:
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        self.started.set()
        return True

    def close(self) -> bool:
        self.close_calls += 1
        return True


class BlockingCloseCapture(BlockingCapture):
    def __init__(self) -> None:
        super().__init__()
        self.close_entered = threading.Event()
        self.allow_close = threading.Event()

    def close(self) -> None:
        self.close_calls += 1
        self.close_entered.set()
        if not self.allow_close.wait(timeout=2):
            raise TimeoutError("test close did not unblock")
        self.released.set()


class CloseFailsStopWorksCapture(BlockingCapture):
    def close(self) -> None:
        self.close_calls += 1
        raise RuntimeError("close failed")


class FakeCaptionBridge(Closable):
    def __init__(self, *, finish_error: Exception | None = None) -> None:
        super().__init__()
        self.finish_error = finish_error
        self.finish_calls = 0
        self.reset_calls = 0

    def finish(self) -> None:
        self.finish_calls += 1
        if self.finish_error is not None:
            raise self.finish_error

    def reset(self) -> None:
        self.reset_calls += 1


class VoiceServiceSafetyTests(unittest.TestCase):
    def test_permission_denied_constructs_nothing_and_opens_no_capture(self):
        permission = Permission(False)
        calls = []

        def runtime_factory(**kwargs):
            calls.append(kwargs)
            raise AssertionError("runtime factory must not run without permission")

        service = VoiceService(permission=permission, runtime_factory=runtime_factory)

        with self.assertRaises(VoicePermissionDenied):
            service.start()
        with self.assertRaises(VoicePermissionDenied):
            service.test_command("幕僚幕僚，打开记事本")

        self.assertEqual(calls, [])
        status = service.status()
        self.assertFalse(status["authorized"])
        self.assertEqual(status["state"], "denied")

    def test_runtime_without_capture_is_released_before_start_fails(self):
        engine = FakeEngine()
        resources = Closable()
        service = VoiceService(
            permission=Permission(True),
            runtime_factory=lambda **_: VoiceRuntime(
                engine=engine,
                capture=None,
                resources=resources,
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "did not provide an audio capture"):
            service.start()

        self.assertEqual(engine.close_calls, 1)
        self.assertEqual(resources.close_calls, 1)
        self.assertEqual(service.status()["state"], "error")

    def test_start_stop_cancels_capture_and_releases_runtime_once(self):
        permission = Permission(True)
        engine = FakeEngine()
        capture = BlockingCapture()
        runtime_resources = Closable()
        factory_calls = 0

        def runtime_factory(**kwargs):
            nonlocal factory_calls
            factory_calls += 1
            return VoiceRuntime(
                engine=engine,
                capture=capture,
                resources=runtime_resources,
            )

        service = VoiceService(
            permission=permission,
            runtime_factory=runtime_factory,
            stop_timeout=1,
        )

        started = service.start()
        self.assertTrue(started["changed"])
        self.assertTrue(capture.entered.wait(timeout=1))
        stopped = service.stop()

        self.assertTrue(stopped["changed"])
        self.assertFalse(service.status()["running"])
        self.assertEqual(service.status()["state"], "stopped")
        self.assertEqual(factory_calls, 1)
        self.assertEqual(engine.cancel_calls, 1)
        self.assertEqual(capture.stop_calls, 1)
        self.assertEqual(capture.close_calls, 1)
        self.assertEqual(engine.close_calls, 1)
        self.assertEqual(runtime_resources.close_calls, 1)

        again = service.stop()
        self.assertFalse(again["changed"])
        self.assertTrue(again["idempotent"])
        self.assertEqual(capture.close_calls, 1)

    def test_nonblocking_stop_returns_before_blocking_capture_close(self):
        engine = FakeEngine()
        capture = BlockingCloseCapture()
        service = VoiceService(
            permission=Permission(True),
            runtime_factory=lambda **_: VoiceRuntime(engine=engine, capture=capture),
            stop_timeout=1,
        )
        service.start()
        self.assertTrue(capture.entered.wait(timeout=1))

        started = time.perf_counter()
        result = service.stop(wait=False)
        elapsed = time.perf_counter() - started

        self.assertTrue(result["changed"])
        self.assertLess(elapsed, 0.25)
        self.assertTrue(capture.close_entered.wait(timeout=1))
        capture.allow_close.set()
        deadline = time.time() + 1
        while service.status()["running"] and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(service.status()["running"])

    def test_capture_close_failure_falls_back_to_stop(self):
        engine = FakeEngine()
        capture = CloseFailsStopWorksCapture()
        service = VoiceService(
            permission=Permission(True),
            runtime_factory=lambda **_: VoiceRuntime(engine=engine, capture=capture),
            stop_timeout=1,
        )
        service.start()
        self.assertTrue(capture.entered.wait(timeout=1))

        service.stop()

        self.assertEqual(capture.close_calls, 1)
        self.assertEqual(capture.stop_calls, 1)
        self.assertFalse(service.status()["running"])

    def test_permission_revocation_interrupts_blocking_capture(self):
        permission = Permission(True)
        engine = FakeEngine()
        capture = BlockingCapture()
        service = VoiceService(
            permission=permission,
            runtime_factory=lambda **_: VoiceRuntime(engine=engine, capture=capture),
            stop_timeout=1,
        )
        service.start()
        self.assertTrue(capture.entered.wait(timeout=1))

        permission.revoke()
        deadline = time.time() + 1
        while service.status()["running"] and time.time() < deadline:
            time.sleep(0.01)

        self.assertFalse(service.status()["running"])
        self.assertEqual(capture.close_calls, 1)
        self.assertEqual(engine.cancel_calls, 1)
        self.assertEqual(service.status()["last_error"]["code"], "permission_revoked")

    def test_injected_execution_engine_is_not_assumed_safe_for_dry_run(self):
        engine = FakeEngine()
        service = VoiceService(permission=Permission(True), engine=engine)

        with self.assertRaisesRegex(RuntimeError, "explicit test_engine_factory"):
            service.test_command("幕僚幕僚，打开记事本")

        self.assertEqual(engine.commands, [])

    def test_engine_factory_second_value_is_closed_as_runtime_resources(self):
        permission = Permission(True)
        engine = FakeEngine()
        capture = BlockingCapture()
        runtime_resources = Closable()

        service = VoiceService(
            permission=permission,
            engine_factory=lambda **kwargs: (engine, runtime_resources),
            capture_factory=lambda **kwargs: capture,
            stop_timeout=1,
        )

        service.start()
        self.assertTrue(capture.entered.wait(timeout=1))
        service.stop()

        self.assertEqual(runtime_resources.close_calls, 1)
        self.assertEqual(capture.close_calls, 1)

    def test_concurrent_start_is_idempotent_and_builds_one_runtime(self):
        permission = Permission(True)
        engine = FakeEngine()
        capture = BlockingCapture()
        factory_calls = 0
        factory_lock = threading.Lock()
        barrier = threading.Barrier(6)

        def runtime_factory(**kwargs):
            nonlocal factory_calls
            with factory_lock:
                factory_calls += 1
            time.sleep(0.025)
            return VoiceRuntime(engine=engine, capture=capture)

        service = VoiceService(
            permission=permission,
            runtime_factory=runtime_factory,
            stop_timeout=1,
        )

        def start_together():
            barrier.wait(timeout=1)
            return service.start()

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(start_together) for _ in range(6)]
            results = [future.result(timeout=2) for future in futures]

        self.assertTrue(capture.entered.wait(timeout=1))
        self.assertEqual(factory_calls, 1)
        self.assertEqual(sum(bool(item["changed"]) for item in results), 1)
        self.assertEqual(sum(bool(item["idempotent"]) for item in results), 5)
        service.stop()

    def test_command_test_is_dry_run_and_never_constructs_capture(self):
        permission = Permission(True)
        dry_engine = FakeEngine(command_status="executed")
        modes: list[bool] = []

        def test_engine_factory(*, events, dry_run):
            modes.append(dry_run)
            self.assertTrue(dry_run)
            return VoiceRuntime(engine=dry_engine, capture=None)

        service = VoiceService(
            permission=permission,
            test_engine_factory=test_engine_factory,
        )
        result = service.test_command("幕僚幕僚，打开记事本")

        self.assertEqual(modes, [True])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["result"]["status"], "executed")
        self.assertEqual(dry_engine.commands, ["幕僚幕僚，打开记事本"])

    def test_release_stops_capture_before_closing_caption_bridge(self):
        order: list[str] = []

        class Capture:
            def close(self) -> None:
                order.append("capture.close")

        class Bridge:
            def reset(self) -> None:
                order.append("bridge.reset")

            def close(self) -> None:
                order.append("bridge.close")

        runtime = VoiceRuntime(
            engine=object(),
            capture=_CaptionedCapture(Capture(), Bridge(), VoiceEventHub()),
            caption_bridge=Bridge(),
        )
        runtime.capture._bridge = runtime.caption_bridge
        service = VoiceService(permission=Permission(True))

        service._release_runtime(runtime)

        self.assertEqual(order, ["capture.close", "bridge.reset", "bridge.close"])

    def test_watcher_start_failure_opens_no_capture_and_releases_runtime(self):
        permission = Permission(True)
        engine = FakeEngine()
        capture = BlockingCapture()
        resources = Closable()
        bridge = FakeCaptionBridge()
        watcher = FakeWatcher(lambda: None, start_error=RuntimeError("hotkey unavailable"))
        service = VoiceService(
            permission=permission,
            runtime_factory=lambda **_: VoiceRuntime(
                engine=engine,
                capture=capture,
                resources=resources,
                caption_bridge=bridge,
            ),
            emergency_watcher_factory=lambda callback: (
                setattr(watcher, "callback", callback) or watcher
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "hotkey unavailable"):
            service.start()

        self.assertEqual(capture.calls, 0)
        self.assertEqual(capture.close_calls, 1)
        self.assertEqual(engine.close_calls, 1)
        self.assertEqual(resources.close_calls, 1)
        self.assertEqual(bridge.close_calls, 1)
        self.assertEqual(watcher.start_calls, 1)
        self.assertEqual(watcher.close_calls, 1)
        self.assertEqual(service.status()["state"], "error")

    def test_emergency_callback_requests_nonblocking_stop_and_worker_releases_all(self):
        permission = Permission(True)
        engine = FakeEngine()
        capture = BlockingCapture()
        resources = Closable()
        bridge = FakeCaptionBridge()
        watchers: list[FakeWatcher] = []

        def watcher_factory(callback):
            watcher = FakeWatcher(callback)
            watchers.append(watcher)
            return watcher

        service = VoiceService(
            permission=permission,
            runtime_factory=lambda **_: VoiceRuntime(
                engine=engine,
                capture=capture,
                resources=resources,
                caption_bridge=bridge,
            ),
            emergency_watcher_factory=watcher_factory,
            stop_timeout=1,
        )
        service.start()
        self.assertTrue(capture.entered.wait(timeout=1))

        callback_done = threading.Event()
        callback_thread = threading.Thread(
            target=lambda: (watchers[0].callback(), callback_done.set()),
            daemon=True,
        )
        callback_thread.start()

        self.assertTrue(callback_done.wait(timeout=0.5))
        callback_thread.join(timeout=1)
        deadline = time.time() + 1
        while service.status()["running"] and time.time() < deadline:
            time.sleep(0.01)

        self.assertFalse(service.status()["running"])
        self.assertEqual(engine.cancel_calls, 1)
        self.assertEqual(capture.stop_calls, 1)
        self.assertEqual(capture.close_calls, 1)
        self.assertEqual(watchers[0].close_calls, 1)
        self.assertEqual(bridge.close_calls, 1)
        self.assertEqual(resources.close_calls, 1)

    def test_caption_finish_failure_does_not_block_final_command_asr(self):
        permission = Permission(True)
        engine = FakeEngine()
        raw_capture = BlockingCapture()
        bridge = FakeCaptionBridge(finish_error=RuntimeError("caption native failure"))
        raw_capture.released.set()
        capture = _CaptionedCapture(raw_capture, bridge, VoiceEventHub())
        service = VoiceService(
            permission=permission,
            runtime_factory=lambda **_: VoiceRuntime(
                engine=engine,
                capture=capture,
                caption_bridge=bridge,
            ),
            stop_timeout=1,
        )

        service.start()
        deadline = time.time() + 1
        while engine.audio_calls == 0 and time.time() < deadline:
            time.sleep(0.01)
        service.stop()

        self.assertGreaterEqual(bridge.finish_calls, 1)
        self.assertGreaterEqual(engine.audio_calls, 1)
        self.assertEqual(bridge.close_calls, 1)

    def test_cancelled_capture_resets_caption_without_emitting_late_final(self):
        token = CancellationToken()
        bridge = FakeCaptionBridge()
        events = VoiceEventHub()

        class Capture:
            def capture_utterance(self, *, cancellation=None):
                token.cancel()
                return object()

        capture = _CaptionedCapture(Capture(), bridge, events)

        with self.assertRaises(VoiceCancelled):
            capture.capture_utterance(cancellation=token)

        self.assertEqual(bridge.reset_calls, 1)
        self.assertEqual(bridge.finish_calls, 0)

    def test_default_dry_run_skips_model_preflight_and_capture(self):
        settings = SimpleNamespace()
        engine = FakeEngine()
        resources = Closable()
        with (
            mock.patch("voice.config.VoiceSettings.load", return_value=settings),
            mock.patch("voice.runtime.build_runtime", return_value=(engine, resources)) as build,
            mock.patch("voice.runtime.build_capture") as build_capture,
            mock.patch("voice.models.load_model_inventory") as load_inventory,
            mock.patch("voice.models.validate_model_assets") as validate,
            mock.patch("voice.models.warmup_sensevoice") as warmup,
        ):
            runtime = _default_runtime_factory(events=VoiceEventHub(), dry_run=True)

        build.assert_called_once_with(
            settings,
            mode="fast",
            act=False,
            speak=False,
            enable_asr=False,
        )
        build_capture.assert_not_called()
        load_inventory.assert_not_called()
        validate.assert_not_called()
        warmup.assert_not_called()
        self.assertIsNone(runtime.capture)
        self.assertIs(runtime.engine, engine)
        resources.close()

    def test_default_real_start_model_failure_precedes_runtime_mic_and_watcher(self):
        settings = SimpleNamespace(
            sample_rate=16_000,
            sensevoice_dir=Path("C:/Models/SenseVoice"),
        )
        report = mock.Mock()
        report.raise_for_errors.side_effect = RuntimeError("missing model assets")
        watcher_factory = mock.Mock()
        service = VoiceService(
            permission=Permission(True),
            emergency_watcher_factory=watcher_factory,
        )
        with (
            mock.patch("voice.config.VoiceSettings.load", return_value=settings),
            mock.patch("voice.models.load_model_inventory", return_value=mock.Mock()),
            mock.patch("voice.models.validate_model_assets", return_value=report),
            mock.patch("voice.runtime.build_runtime") as build_runtime,
            mock.patch("voice.runtime.build_capture") as build_capture,
        ):
            with self.assertRaisesRegex(RuntimeError, "missing model assets"):
                service.start()

        build_runtime.assert_not_called()
        build_capture.assert_not_called()
        watcher_factory.assert_not_called()
        self.assertEqual(service.status()["state"], "error")

    def test_default_real_start_warmup_failure_releases_before_mic_or_watcher(self):
        settings = SimpleNamespace(
            sample_rate=16_000,
            sensevoice_dir=Path("C:/Models/SenseVoice"),
        )
        inventory = mock.Mock()
        report = mock.Mock()
        engine = SimpleNamespace(
            events=None,
            recognizer=SimpleNamespace(_get_recognizer=lambda: object()),
        )
        resources = Closable()
        watcher_factory = mock.Mock()
        service = VoiceService(
            permission=Permission(True),
            emergency_watcher_factory=watcher_factory,
        )
        with (
            mock.patch("voice.config.VoiceSettings.load", return_value=settings),
            mock.patch("voice.models.load_model_inventory", return_value=inventory),
            mock.patch("voice.models.validate_model_assets", return_value=report),
            mock.patch(
                "voice.models.warmup_sensevoice",
                return_value=SimpleNamespace(ok=False, error="native load failed"),
            ),
            mock.patch("voice.runtime.build_runtime", return_value=(engine, resources)),
            mock.patch("voice.runtime.build_capture") as build_capture,
        ):
            with self.assertRaisesRegex(RuntimeError, "warm-up failed"):
                service.start()

        report.raise_for_errors.assert_called_once_with()
        build_capture.assert_not_called()
        watcher_factory.assert_not_called()
        self.assertEqual(resources.close_calls, 1)
        self.assertEqual(service.status()["state"], "error")

    def test_default_real_factory_wires_shared_guard_caption_and_capture(self):
        settings = SimpleNamespace(
            sample_rate=16_000,
            sensevoice_dir=Path("C:/Models/SenseVoice"),
        )
        guard = object()
        recognizer_backend = object()
        engine = SimpleNamespace(
            events=None,
            wake=object(),
            recognizer=SimpleNamespace(_get_recognizer=lambda: recognizer_backend),
        )

        class Resources(Closable):
            echo_guard = guard

        resources = Resources()
        files = tuple(
            SimpleNamespace(name=name, path=Path("C:/Models/Streaming") / name)
            for name in (
                "tokens.txt",
                "encoder-epoch-99-avg-1.onnx",
                "decoder-epoch-99-avg-1.onnx",
                "joiner-epoch-99-avg-1.onnx",
            )
        )
        inventory = mock.Mock()
        inventory.require.return_value = SimpleNamespace(files=files)
        report = mock.Mock()
        warmup = SimpleNamespace(ok=True, error=None, total_seconds=0.125)
        bridge = FakeCaptionBridge()
        bridge.accept_pcm = mock.Mock()
        bridge.reset = mock.Mock()
        raw_capture = BlockingCapture()
        streaming_recognizer = mock.Mock()
        streaming_recognizer.warmup.return_value = None
        events = VoiceEventHub()
        bridge.events = events

        with (
            mock.patch("voice.config.VoiceSettings.load", return_value=settings),
            mock.patch("voice.models.load_model_inventory", return_value=inventory) as load_inventory,
            mock.patch("voice.models.validate_model_assets", return_value=report) as validate,
            mock.patch("voice.models.warmup_sensevoice", return_value=warmup) as warm,
            mock.patch("voice.runtime.build_runtime", return_value=(engine, resources)) as build_runtime,
            mock.patch("voice.runtime.build_capture", return_value=raw_capture) as build_capture,
            mock.patch(
                "voice.asr_streaming.StreamingZipformerRecognizer",
                return_value=streaming_recognizer,
            ) as streaming_cls,
            mock.patch(
                "voice.captions.StreamingCaptionBridge",
                return_value=bridge,
            ) as bridge_cls,
        ):
            runtime = _default_runtime_factory(events=events, dry_run=False)

        build_runtime.assert_called_once_with(
            settings,
            mode="fast",
            act=True,
            speak=True,
            enable_asr=True,
        )
        validate.assert_called_once_with(
            load_inventory.return_value,
            model_names=("sensevoice", "streaming_zipformer"),
        )
        report.raise_for_errors.assert_called_once_with()
        self.assertIs(warm.call_args.kwargs["recognizer_factory"](Path("unused")), recognizer_backend)
        bridge_cls.assert_called_once_with(streaming_recognizer, engine.wake, events, guard)
        self.assertIs(build_capture.call_args.kwargs["echo_guard"], guard)
        self.assertIsInstance(runtime.caption_bridge, AsyncCaptionPump)
        self.assertIs(runtime.caption_bridge.bridge, bridge)
        self.assertEqual(build_capture.call_args.kwargs["frame_resetter"], runtime.caption_bridge.reset)
        self.assertIs(runtime.resources, resources)
        self.assertIsInstance(runtime.capture, _CaptionedCapture)
        streaming_cls.assert_called_once()
        runtime.caption_bridge.close()
        runtime.resources.close()

    def test_microphone_result_sanitizes_wake_miss_and_echo_drop_text(self):
        for status in ("wake_miss", "echo_drop"):
            with self.subTest(status=status):
                engine = FakeEngine(command_status=status)
                capture = BlockingCapture()
                capture.released.set()
                events = VoiceEventHub()
                collected: list[dict[str, object]] = []
                original_emit = events.emit

                def record(event_type, payload):
                    collected.append({"type": event_type, "payload": dict(payload)})
                    original_emit(event_type, payload)

                events.emit = record
                engine.process_audio = lambda _audio, value=status: VoiceResult(
                    status=value,
                    transcript="私人环境对话",
                    command="不应泄露",
                )
                service = VoiceService(
                    permission=Permission(True),
                    runtime_factory=lambda **_: VoiceRuntime(engine=engine, capture=capture),
                    event_hub=events,
                    stop_timeout=0.1,
                )
                service.start()
                deadline = time.time() + 0.5
                while not any(item["type"] == "voice.result" for item in collected) and time.time() < deadline:
                    time.sleep(0.01)
                service.stop()

                results = [item for item in collected if item["type"] == "voice.result"]
                self.assertTrue(results)
                payload = results[0]["payload"]
                self.assertEqual(payload["result"], {"status": status})
                self.assertNotIn("私人环境对话", repr(payload))

    def test_sse_subscription_disconnects_without_leaking(self):
        async def scenario():
            service = VoiceService(permission=Permission(True))
            disconnected = False
            events = []

            async def is_disconnected():
                return disconnected

            iterator = service.stream_events(is_disconnected)
            events.append(await anext(iterator))
            self.assertEqual(service.events.subscriber_count, 1)
            service.events.emit("voice.metric", {"name": "test", "value": 1})
            events.append(await asyncio.wait_for(anext(iterator), timeout=1))
            disconnected = True
            with self.assertRaises(StopAsyncIteration):
                await asyncio.wait_for(anext(iterator), timeout=1)
            return service, events

        service, events = asyncio.run(scenario())

        self.assertEqual(service.events.subscriber_count, 0)
        self.assertEqual([event["type"] for event in events], ["voice.status", "voice.metric"])
        for event in events:
            self.assertTrue(event["type"].startswith("voice."))
            self.assertEqual(set(event), {"type", "seq", "ts", "payload"})

    def test_event_hub_rejects_non_voice_namespace(self):
        hub = VoiceEventHub()
        with self.assertRaisesRegex(ValueError, "voice\\.\\*"):
            hub.emit("done", {})


class FakeApiService:
    def __init__(self, *, authorized: bool = True) -> None:
        self.authorized = authorized
        self.started = 0
        self.stopped = 0
        self.commands: list[str] = []

    def status(self):
        return {
            "state": "stopped",
            "running": False,
            "authorized": self.authorized,
            "generation": 0,
            "started_at": None,
            "last_error": None,
            "subscribers": 0,
        }

    def start(self):
        if not self.authorized:
            raise VoicePermissionDenied("voice_control is not granted")
        self.started += 1
        return {**self.status(), "state": "starting", "changed": True, "idempotent": False}

    def stop(self):
        self.stopped += 1
        return {**self.status(), "changed": True, "idempotent": False}

    def test_command(self, text):
        self.commands.append(text)
        return {
            "ok": True,
            "dry_run": True,
            "result": {"status": "executed", "command": text},
        }

    async def stream_events(self, is_disconnected=None):
        yield {
            "type": "voice.status",
            "seq": 1,
            "ts": 1.0,
            "payload": self.status(),
        }
        yield {
            "type": "voice.decision",
            "seq": 2,
            "ts": 2.0,
            "payload": {"accepted": True},
        }


class VoiceApiTests(unittest.TestCase):
    def make_client(self, service: FakeApiService) -> TestClient:
        app = FastAPI()
        app.include_router(router, prefix="/api/voice")
        app.dependency_overrides[get_voice_service] = lambda: service
        return TestClient(app)

    def test_status_start_stop_and_test_command_routes(self):
        service = FakeApiService()
        with self.make_client(service) as client:
            status = client.get("/api/voice/status")
            started = client.post("/api/voice/start", json={})
            tested = client.post(
                "/api/voice/command/test",
                json={"text": "幕僚幕僚，打开记事本"},
            )
            stopped = client.post("/api/voice/stop", json={})

        self.assertEqual(status.status_code, 200)
        self.assertEqual(started.status_code, 200)
        self.assertEqual(tested.status_code, 200)
        self.assertTrue(tested.json()["dry_run"])
        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(service.started, 1)
        self.assertEqual(service.stopped, 1)
        self.assertEqual(service.commands, ["幕僚幕僚，打开记事本"])

    def test_start_permission_denial_returns_403(self):
        with self.make_client(FakeApiService(authorized=False)) as client:
            response = client.post("/api/voice/start", json={})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "permission_denied")

    def test_sse_uses_only_voice_event_names_and_envelopes(self):
        with self.make_client(FakeApiService()) as client:
            response = client.get("/api/voice/events")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        blocks = [block for block in response.text.strip().split("\n\n") if block]
        self.assertEqual(len(blocks), 2)
        for block in blocks:
            lines = block.splitlines()
            self.assertTrue(lines[0].startswith("event: voice."))
            self.assertNotIn(lines[0], {"event: done", "event: error", "event: delta"})
            envelope = json.loads(lines[1].removeprefix("data: "))
            self.assertTrue(envelope["type"].startswith("voice."))
            self.assertEqual(set(envelope), {"type", "seq", "ts", "payload"})


class VoicePageAssetTests(unittest.TestCase):
    def test_page_is_standalone_and_contains_required_views(self):
        index = (ROOT / "static" / "voice" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "voice" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "static" / "voice" / "styles.css").read_text(encoding="utf-8")

        for phrase in (
            "语音监听台",
            "实时字幕",
            "判断",
            "动作",
            "播报",
            "指标",
            "错误",
            "启动监听",
            "停止并释放",
            "dry-run",
        ):
            self.assertIn(phrase, index)
        self.assertIn("/api/voice", script)
        self.assertIn("voice.decision", script)
        self.assertIn("voice.action", script)
        self.assertIn("voice.tts", script)
        self.assertIn("voice.metric", script)
        self.assertIn("voice.error", script)
        self.assertNotIn("sendTurn", index + script)
        self.assertNotIn("#send", index + script)
        self.assertIn("prefers-reduced-motion", styles)
        self.assertNotIn("/api/chat", index + script)


if __name__ == "__main__":
    unittest.main()
