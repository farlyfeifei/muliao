from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
import time
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.api import get_voice_service, router
from voice.contracts import VoiceResult
from voice.service import (
    VoiceEventHub,
    VoicePermissionDenied,
    VoiceRuntime,
    VoiceService,
)


class Permission:
    def __init__(self, allowed: bool) -> None:
        self.enabled = allowed
        self.calls = 0

    def allowed(self) -> bool:
        self.calls += 1
        return self.enabled


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

    def cancel_current(self) -> None:
        self.cancel_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class Closable:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


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

        def runtime_factory(*, events, dry_run):
            modes.append(dry_run)
            self.assertTrue(dry_run)
            return VoiceRuntime(engine=dry_engine, capture=None)

        service = VoiceService(permission=permission, runtime_factory=runtime_factory)
        result = service.test_command("幕僚幕僚，打开记事本")

        self.assertEqual(modes, [True])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["result"]["status"], "executed")
        self.assertEqual(dry_engine.commands, ["幕僚幕僚，打开记事本"])

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
