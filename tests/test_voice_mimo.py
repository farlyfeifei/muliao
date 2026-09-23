from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
import threading
import time
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.asr_mimo import MiMoAsrClient, MiMoAsrError
from voice.audio_player import CancellableAudioPlayer, MemoryAudioSink
from voice.cancellation import CancellationToken
from voice.tts_mimo import MiMoTtsClient, MiMoTtsError


class RecordingPlayer:
    def __init__(self) -> None:
        self.generation = 0
        self.played: list[tuple[int, bytes]] = []
        self.stop_calls: list[int | None] = []

    def begin(self) -> int:
        self.generation += 1
        return self.generation

    def play(self, pcm: bytes, *, generation: int) -> bool:
        if generation != self.generation:
            return False
        self.played.append((generation, bytes(pcm)))
        return True

    def stop(self, *, generation: int | None = None) -> bool:
        self.stop_calls.append(generation)
        if generation is not None and generation != self.generation:
            return False
        self.generation += 1
        return True


class MiMoAsrTests(unittest.TestCase):
    def test_asr_uses_data_uri_api_key_and_returns_transcript(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "打开记事本"}}]},
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        asr = MiMoAsrClient(api_key="test-key", client=client)
        result = asr.transcribe(b"RIFF-test", mime_type="audio/wav", language="zh")
        self.assertEqual(result.text, "打开记事本")
        self.assertEqual(len(seen), 1)
        request = seen[0]
        self.assertEqual(request.url, "https://api.xiaomimimo.com/v1/chat/completions")
        self.assertEqual(request.headers["api-key"], "test-key")
        body = json.loads(request.content)
        item = body["messages"][0]["content"][0]
        self.assertEqual(item["type"], "input_audio")
        self.assertTrue(item["input_audio"]["data"].startswith("data:audio/wav;base64,"))
        self.assertEqual(body["asr_options"], {"language": "zh"})

    def test_asr_supports_mp3_mime(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "测试"}}]},
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = MiMoAsrClient(api_key="test-key", client=client).transcribe(
            b"ID3", mime_type="audio/mpeg"
        )
        self.assertEqual(result.text, "测试")
        uri = seen[0]["messages"][0]["content"][0]["input_audio"]["data"]
        self.assertTrue(uri.startswith("data:audio/mpeg;base64,"))

    def test_asr_missing_key_never_touches_network(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise AssertionError("network must not be called")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with self.assertRaises(MiMoAsrError) as caught:
            MiMoAsrClient(api_key="", client=client).transcribe(b"RIFF")
        self.assertEqual(caught.exception.code, "missing_api_key")
        self.assertEqual(calls, [])

    def test_asr_timeout_is_structured(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with self.assertRaises(MiMoAsrError) as caught:
            MiMoAsrClient(api_key="test-key", client=client).transcribe(b"RIFF")
        self.assertEqual(caught.exception.code, "timeout")
        self.assertTrue(caught.exception.retryable)


class MiMoTtsTests(unittest.TestCase):
    @staticmethod
    def _sse(*chunks: bytes) -> bytes:
        rows = [b"data: {}\n\n"]
        for chunk in chunks:
            payload = {
                "choices": [
                    {"delta": {"audio": {"data": base64.b64encode(chunk).decode()}}}
                ]
            }
            rows.append(f"data: {json.dumps(payload)}\n\n".encode())
        rows.append(b"data: [DONE]\n\n")
        return b"".join(rows)

    def test_tts_uses_assistant_role_api_key_pcm16_and_stream(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=self._sse(b"first", b"second"))

        player = RecordingPlayer()
        client = httpx.Client(transport=httpx.MockTransport(handler))
        generation = MiMoTtsClient(
            api_key="test-key", player=player, client=client
        ).speak("你好", instruction="轻快地说")
        self.assertEqual(generation, 1)
        self.assertEqual([chunk for _, chunk in player.played], [b"first", b"second"])
        request = seen[0]
        self.assertEqual(request.headers["api-key"], "test-key")
        body = json.loads(request.content)
        self.assertEqual(body["messages"][-1], {"role": "assistant", "content": "你好"})
        self.assertEqual(body["audio"]["format"], "pcm16")
        self.assertTrue(body["stream"])

    def test_tts_cancel_discards_late_chunk(self):
        first_yielded = threading.Event()
        allow_late = threading.Event()
        token = CancellationToken()

        class LateStream(httpx.SyncByteStream):
            def __iter__(self):
                yield MiMoTtsTests._sse(b"first").removesuffix(b"data: [DONE]\n\n")
                first_yielded.set()
                allow_late.wait(timeout=1.0)
                late = {
                    "choices": [
                        {
                            "delta": {
                                "audio": {
                                    "data": base64.b64encode(b"late").decode()
                                }
                            }
                        }
                    ]
                }
                yield f"data: {json.dumps(late)}\n\ndata: [DONE]\n\n".encode()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=LateStream())

        player = RecordingPlayer()
        tts = MiMoTtsClient(
            api_key="test-key",
            player=player,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        errors = []

        def run() -> None:
            try:
                tts.speak("会被打断", cancellation=token)
            except Exception as exc:  # pragma: no cover - 失败时保留线程异常
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(first_yielded.wait(timeout=1.0))
        token.cancel()
        allow_late.set()
        thread.join(timeout=1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([chunk for _, chunk in player.played], [b"first"])
        self.assertTrue(player.stop_calls)

    def test_tts_429_is_structured_and_not_retried(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(429, json={"error": "busy"})

        player = RecordingPlayer()
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with self.assertRaises(MiMoTtsError) as caught:
            MiMoTtsClient(api_key="test-key", player=player, client=client).speak("你好")
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertEqual(caught.exception.status_code, 429)
        self.assertTrue(caught.exception.fallback_recommended)
        self.assertEqual(calls, 1)

    def test_tts_5xx_is_structured_and_not_retried(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503, json={"error": "unavailable"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with self.assertRaises(MiMoTtsError) as caught:
            MiMoTtsClient(
                api_key="test-key", player=RecordingPlayer(), client=client
            ).speak("你好")
        self.assertEqual(caught.exception.code, "service_unavailable")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(calls, 1)

    def test_tts_first_audio_timeout_is_structured(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("no first audio", request=request)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with self.assertRaises(MiMoTtsError) as caught:
            MiMoTtsClient(
                api_key="test-key",
                player=RecordingPlayer(),
                client=client,
                first_audio_timeout=1.2,
            ).speak("你好")
        self.assertEqual(caught.exception.code, "first_audio_timeout")
        self.assertTrue(caught.exception.fallback_recommended)

    def test_tts_missing_key_never_touches_network(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise AssertionError("network must not be called")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with self.assertRaises(MiMoTtsError) as caught:
            MiMoTtsClient(
                api_key="", player=RecordingPlayer(), client=client
            ).speak("你好")
        self.assertEqual(caught.exception.code, "missing_api_key")
        self.assertEqual(calls, [])


class AudioPlayerTests(unittest.TestCase):
    def test_generation_rejects_late_chunks_and_stop_clears(self):
        sink = MemoryAudioSink()
        player = CancellableAudioPlayer(sink, max_queue_chunks=2)
        try:
            old = player.begin()
            new = player.begin()
            self.assertFalse(player.play(b"late", generation=old))
            self.assertTrue(player.play(b"current", generation=new))
            deadline = time.monotonic() + 1.0
            while not sink.chunks and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(sink.chunks, [b"current"])
            self.assertTrue(player.stop(generation=new))
            self.assertEqual(sink.chunks, [])
            self.assertGreaterEqual(sink.stop_calls, 3)
            self.assertGreaterEqual(sink.clear_calls, 3)
        finally:
            player.close()


class SecretScanTests(unittest.TestCase):
    def test_new_mimo_sources_contain_no_real_api_key(self):
        paths = [
            ROOT / "voice" / "asr_mimo.py",
            ROOT / "voice" / "tts_mimo.py",
            ROOT / "voice" / "audio_player.py",
            ROOT / "tests" / "test_voice_mimo.py",
        ]
        source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
        secret_prefixes = tuple("".join(parts) for parts in (("s", "k", "-"), ("t", "p", "-")))
        for prefix in secret_prefixes:
            self.assertNotIn(prefix, source)
        self.assertNotRegex(source, r"(?i)(api[_-]?key)\s*=\s*['\"][^'\"]{16,}")


if __name__ == "__main__":
    unittest.main()
