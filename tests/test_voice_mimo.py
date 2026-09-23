from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import sys
import threading
import time
import unittest
import wave

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.asr_mimo import MiMoAsrClient, MiMoAsrError
from voice.audio_player import CancellableAudioPlayer, MemoryAudioSink
from voice.cancellation import CancellationToken
from voice.contracts import AudioSegment
from voice.tts_mimo import MiMoTtsClient, MiMoTtsError


class RecordingPlayer:
    def __init__(self) -> None:
        self.generation = 0
        self.played: list[tuple[int, bytes]] = []
        self.stop_calls: list[int | None] = []
        self.drained = True

    def begin(self) -> int:
        self.generation += 1
        self.drained = True
        return self.generation

    def play(self, pcm: bytes, *, generation: int) -> bool:
        if generation != self.generation:
            return False
        self.played.append((generation, bytes(pcm)))
        self.drained = False
        return True

    def stop(self, *, generation: int | None = None) -> bool:
        self.stop_calls.append(generation)
        if generation is not None and generation != self.generation:
            return False
        self.generation += 1
        self.drained = True
        return True

    def is_drained(self, generation: int) -> bool:
        return self.drained


class BlockingSink:
    """write blocks until stop, exposing player lock and generation races deterministically."""

    def __init__(self) -> None:
        self.write_started = threading.Event()
        self.write_released = threading.Event()
        self.stop_called = threading.Event()
        self.clear_called = threading.Event()
        self.writes: list[bytes] = []
        self.write_returns = 0
        self._lock = threading.Lock()

    def write(self, pcm: bytes) -> None:
        with self._lock:
            self.writes.append(bytes(pcm))
        self.write_started.set()
        if not self.write_released.wait(timeout=2.0):
            raise AssertionError("BlockingSink.write was not interrupted")
        with self._lock:
            self.write_returns += 1

    def stop(self) -> None:
        self.stop_called.set()
        self.write_released.set()

    def clear(self) -> None:
        self.clear_called.set()


class SlowSink:
    """Hold one write so MiMo speak must wait for real playback completion."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.writes: list[bytes] = []

    def write(self, pcm: bytes) -> None:
        self.entered.set()
        if not self.release.wait(timeout=2.0):
            raise AssertionError("slow sink was not released")
        self.writes.append(bytes(pcm))

    def stop(self) -> None:
        if self.entered.is_set():
            self.release.set()

    def clear(self) -> None:
        return None


class FailingSink:
    def write(self, pcm: bytes) -> None:
        raise OSError("device disconnected")

    def stop(self) -> None:
        return None

    def clear(self) -> None:
        return None


class TransitionSink:
    """Blocks the first begin's stop so begin ordering can be asserted."""

    def __init__(self) -> None:
        self.first_stop_started = threading.Event()
        self.allow_first_stop = threading.Event()
        self.stop_count = 0
        self.clear_count = 0
        self.writes: list[bytes] = []
        self._lock = threading.Lock()

    def write(self, pcm: bytes) -> None:
        with self._lock:
            self.writes.append(bytes(pcm))

    def stop(self) -> None:
        with self._lock:
            self.stop_count += 1
            count = self.stop_count
        if count == 1:
            self.first_stop_started.set()
            self.allow_first_stop.wait(timeout=2.0)

    def clear(self) -> None:
        with self._lock:
            self.clear_count += 1


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

    def test_asr_audio_segment_is_wrapped_as_valid_wav(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "幕僚幕僚打开记事本"}}]},
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        segment = AudioSegment(
            pcm=b"\0\0\1\0" * 80,
            sample_rate=16_000,
            sample_width=2,
            channels=1,
        )
        result = MiMoAsrClient(api_key="test-key", client=client).transcribe(segment)
        self.assertEqual(result.text, "幕僚幕僚打开记事本")
        uri = seen[0]["messages"][0]["content"][0]["input_audio"]["data"]
        self.assertTrue(uri.startswith("data:audio/wav;base64,"))
        raw = base64.b64decode(uri.split(",", 1)[1])
        with wave.open(io.BytesIO(raw), "rb") as wav_file:
            self.assertEqual(wav_file.getframerate(), 16_000)
            self.assertEqual(wav_file.getnchannels(), 1)
            self.assertEqual(wav_file.getsampwidth(), 2)
            self.assertEqual(wav_file.readframes(wav_file.getnframes()), segment.pcm)

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

    def test_tts_two_concurrent_startups_cannot_let_old_begin_clear_new_generation(self):
        first_begin_started = threading.Event()
        allow_first_begin = threading.Event()

        class StartupPlayer(RecordingPlayer):
            def __init__(self) -> None:
                super().__init__()
                self.begin_calls = 0
                self.first_begin_generation = None

            def begin(self) -> int:
                self.begin_calls += 1
                if self.begin_calls == 1:
                    first_begin_started.set()
                    self.first_begin_generation = super().begin()
                    allow_first_begin.wait(timeout=2.0)
                    return self.first_begin_generation
                return super().begin()

        responses = 0
        responses_lock = threading.Lock()

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal responses
            with responses_lock:
                responses += 1
                number = responses
            return httpx.Response(200, content=self._sse(f"audio-{number}".encode()))

        player = StartupPlayer()
        tts = MiMoTtsClient(
            api_key="test-key",
            player=player,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        errors: list[BaseException] = []

        def run(text: str) -> None:
            try:
                tts.speak(text)
            except BaseException as exc:  # pragma: no cover - diagnostic
                errors.append(exc)

        old = threading.Thread(target=run, args=("old",))
        new = threading.Thread(target=run, args=("new",))
        old.start()
        self.assertTrue(first_begin_started.wait(timeout=1.0))
        new.start()
        # New startup is serialized behind old begin rather than overtaking it.
        self.assertEqual(player.begin_calls, 1)
        allow_first_begin.set()
        old.join(timeout=2.0)
        new.join(timeout=2.0)
        self.assertFalse(old.is_alive())
        self.assertFalse(new.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(player.begin_calls, 2)
        self.assertEqual(player.played[-1][1], b"audio-2")

    def test_tts_cancel_after_sse_eof_still_stops_queued_player_generation(self):
        player = RecordingPlayer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=self._sse(b"queued"))

        tts = MiMoTtsClient(
            api_key="test-key",
            player=player,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        generation = tts.speak("still queued")
        self.assertFalse(player.is_drained(player.generation))
        self.assertTrue(tts.cancel(generation))
        self.assertEqual(player.stop_calls[-1], 1)

    def test_tts_cancelled_generation_cannot_stop_new_player_generation(self):
        first_stream_started = threading.Event()
        allow_first_stream_end = threading.Event()
        call_count = 0
        call_lock = threading.Lock()

        class ControlledStream(httpx.SyncByteStream):
            def __iter__(self):
                yield MiMoTtsTests._sse(b"old").removesuffix(b"data: [DONE]\n\n")
                first_stream_started.set()
                allow_first_stream_end.wait(timeout=2.0)
                yield b"data: [DONE]\n\n"

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            with call_lock:
                call_count += 1
                current = call_count
            if current == 1:
                return httpx.Response(200, stream=ControlledStream())
            return httpx.Response(200, content=self._sse(b"new"))

        player = RecordingPlayer()
        tts = MiMoTtsClient(
            api_key="test-key",
            player=player,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        old = threading.Thread(target=lambda: tts.speak("old"))
        old.start()
        self.assertTrue(first_stream_started.wait(timeout=1.0))
        tts.speak("new")
        new_player_generation = player.generation
        allow_first_stream_end.set()
        old.join(timeout=2.0)
        self.assertFalse(old.is_alive())
        self.assertEqual(player.generation, new_player_generation)
        self.assertEqual(player.played[-1][1], b"new")

    def test_tts_waits_until_pcm_is_actually_written(self):
        sink = SlowSink()
        player = CancellableAudioPlayer(sink)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=self._sse(b"played"))

        tts = MiMoTtsClient(
            api_key="test-key",
            player=player,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        outcome: list[object] = []
        thread = threading.Thread(target=lambda: outcome.append(tts.speak("等待播放")))
        thread.start()
        self.assertTrue(sink.entered.wait(timeout=1.0))
        self.assertTrue(thread.is_alive(), "speak returned before the PCM sink finished")
        sink.release.set()
        thread.join(timeout=1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(sink.writes, [b"played"])
        self.assertEqual(outcome, [1])
        player.close()

    def test_tts_rejected_player_chunk_is_structured_error(self):
        class RejectingPlayer(RecordingPlayer):
            def play(self, pcm: bytes, *, generation: int) -> bool:
                return False

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=self._sse(b"no-room"))

        with self.assertRaises(MiMoTtsError) as caught:
            MiMoTtsClient(
                api_key="test-key",
                player=RejectingPlayer(),
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            ).speak("队列满")
        self.assertEqual(caught.exception.code, "audio_queue_full")
        self.assertTrue(caught.exception.fallback_recommended)

    def test_tts_audio_device_failure_is_structured_error(self):
        player = CancellableAudioPlayer(FailingSink())

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=self._sse(b"device-error"))

        try:
            with self.assertRaises(MiMoTtsError) as caught:
                MiMoTtsClient(
                    api_key="test-key",
                    player=player,
                    client=httpx.Client(transport=httpx.MockTransport(handler)),
                ).speak("设备错误")
            self.assertEqual(caught.exception.code, "audio_output_error")
            self.assertTrue(caught.exception.fallback_recommended)
        finally:
            player.close()

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
            self.assertTrue(player.wait_until_drained(new, timeout=1.0))
            self.assertEqual(sink.chunks, [b"current"])
            self.assertTrue(player.stop(generation=new))
            self.assertEqual(sink.chunks, [])
            self.assertGreaterEqual(sink.stop_calls, 3)
            self.assertGreaterEqual(sink.clear_calls, 3)
        finally:
            player.close()

    def test_stop_interrupts_blocking_write_without_holding_control_lock(self):
        sink = BlockingSink()
        player = CancellableAudioPlayer(sink)
        generation = player.begin()
        sink.stop_called.clear()
        sink.write_released.clear()
        self.assertTrue(player.play(b"blocked", generation=generation))
        self.assertTrue(sink.write_started.wait(timeout=1.0))
        stopped = threading.Event()
        thread = threading.Thread(
            target=lambda: (player.stop(generation=generation), stopped.set())
        )
        thread.start()
        self.assertTrue(sink.stop_called.wait(timeout=1.0))
        self.assertTrue(stopped.wait(timeout=1.0))
        thread.join(timeout=1.0)
        self.assertEqual(sink.write_returns, 1)
        writes_after_stop = list(sink.writes)
        self.assertFalse(player.play(b"late", generation=generation))
        self.assertEqual(sink.writes, writes_after_stop)
        player.close()

    def test_close_interrupts_blocking_write_and_joins_worker(self):
        sink = BlockingSink()
        player = CancellableAudioPlayer(sink)
        generation = player.begin()
        sink.stop_called.clear()
        sink.write_released.clear()
        self.assertTrue(player.play(b"blocked", generation=generation))
        self.assertTrue(sink.write_started.wait(timeout=1.0))
        closed = threading.Event()
        thread = threading.Thread(target=lambda: (player.close(), closed.set()))
        thread.start()
        self.assertTrue(sink.stop_called.wait(timeout=1.0))
        self.assertTrue(closed.wait(timeout=1.0))
        thread.join(timeout=1.0)
        self.assertFalse(player._worker.is_alive())
        self.assertFalse(player.play(b"after-close", generation=generation))

    def test_begin_transitions_are_serial_and_old_begin_cannot_clear_new_generation(self):
        sink = TransitionSink()
        player = CancellableAudioPlayer(sink)
        results: list[int] = []
        first = threading.Thread(target=lambda: results.append(player.begin()))
        second = threading.Thread(target=lambda: results.append(player.begin()))
        first.start()
        self.assertTrue(sink.first_stop_started.wait(timeout=1.0))
        second.start()
        self.assertEqual(player.generation, 1)
        self.assertEqual(sink.stop_count, 1)
        sink.allow_first_stop.set()
        first.join(timeout=1.0)
        second.join(timeout=1.0)
        self.assertEqual(sorted(results), [1, 2])
        self.assertEqual(sink.clear_count, 2)
        self.assertEqual(player.generation, 2)
        self.assertTrue(player.play(b"new", generation=2))
        self.assertTrue(player.wait_until_drained(2, timeout=1.0))
        self.assertEqual(sink.writes, [b"new"])
        player.close()

    def test_play_and_close_race_cannot_enqueue_after_stop_signal(self):
        sink = MemoryAudioSink()
        player = CancellableAudioPlayer(sink, max_queue_chunks=1)
        generation = player.begin()
        start = threading.Barrier(2)
        result: list[bool] = []

        def play() -> None:
            start.wait()
            result.append(player.play(b"racy", generation=generation))

        thread = threading.Thread(target=play)
        thread.start()
        start.wait()
        player.close()
        thread.join(timeout=1.0)
        self.assertFalse(thread.is_alive())
        self.assertFalse(player._worker.is_alive())
        self.assertEqual(len(result), 1)
        if not result[0]:
            self.assertEqual(sink.chunks, [])

    def test_clear_does_not_remove_worker_stop_signal_or_leak_thread(self):
        sink = MemoryAudioSink()
        player = CancellableAudioPlayer(sink)
        generation = player.begin()
        self.assertTrue(player.play(b"queued", generation=generation))
        player.clear()
        player.close()
        self.assertFalse(player._worker.is_alive())


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
