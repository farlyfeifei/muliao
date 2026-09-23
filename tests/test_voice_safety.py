from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.safety import (
    DEFAULT_EMERGENCY_KEYS,
    CaptureMuteGate,
    EchoGuard,
    EmergencyStopUnavailable,
    EmergencyStopWatcher,
)


WAIT_SECONDS = 2.0


class MutableClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.value += seconds


class EmergencyStopWatcherTests(unittest.TestCase):
    def test_combination_triggers_once_per_rising_edge(self) -> None:
        pressed: set[int] = set()
        calls: list[str] = []
        watcher = EmergencyStopWatcher(
            lambda: calls.append("stop"),
            key_state_reader=lambda key: key in pressed,
        )

        self.assertFalse(watcher.poll_once())
        pressed.update(DEFAULT_EMERGENCY_KEYS)
        self.assertTrue(watcher.poll_once())
        self.assertEqual(calls, ["stop"])

        self.assertFalse(watcher.poll_once(), "holding the chord must not retrigger")
        self.assertEqual(calls, ["stop"])

        pressed.remove(DEFAULT_EMERGENCY_KEYS[-1])
        self.assertFalse(watcher.poll_once())
        pressed.add(DEFAULT_EMERGENCY_KEYS[-1])
        self.assertTrue(watcher.poll_once())
        self.assertEqual(calls, ["stop", "stop"])

    def test_background_hold_does_not_repeat_and_stop_joins_thread(self) -> None:
        pressed: set[int] = set(DEFAULT_EMERGENCY_KEYS)
        callback_entered = threading.Event()
        callback_calls = 0
        callback_lock = threading.Lock()

        def callback() -> None:
            nonlocal callback_calls
            with callback_lock:
                callback_calls += 1
            callback_entered.set()

        watcher = EmergencyStopWatcher(
            callback,
            key_state_reader=lambda key: key in pressed,
            poll_interval=0.005,
        )
        self.assertTrue(watcher.start())
        worker = watcher.thread
        self.assertIsNotNone(worker)
        self.assertTrue(callback_entered.wait(WAIT_SECONDS))
        time.sleep(0.04)
        with callback_lock:
            self.assertEqual(callback_calls, 1)

        self.assertTrue(watcher.stop(timeout=WAIT_SECONDS))
        self.assertIsNotNone(worker)
        self.assertFalse(worker.is_alive())
        self.assertFalse(watcher.is_running)

    def test_callback_can_stop_its_own_background_watcher(self) -> None:
        pressed: set[int] = set(DEFAULT_EMERGENCY_KEYS)
        stopped_in_callback = threading.Event()
        watcher: EmergencyStopWatcher

        def callback() -> None:
            self.assertTrue(watcher.stop())
            stopped_in_callback.set()

        watcher = EmergencyStopWatcher(
            callback,
            key_state_reader=lambda key: key in pressed,
            poll_interval=0.005,
        )
        watcher.start()
        self.assertTrue(stopped_in_callback.wait(WAIT_SECONDS))
        worker = watcher.thread
        if worker is not None:
            worker.join(timeout=WAIT_SECONDS)
        self.assertFalse(watcher.is_running)

    def test_close_is_idempotent_and_prevents_restart(self) -> None:
        watcher = EmergencyStopWatcher(lambda: None, key_state_reader=lambda key: False)
        watcher.start()
        self.assertTrue(watcher.close(timeout=WAIT_SECONDS))
        self.assertTrue(watcher.close(timeout=WAIT_SECONDS))
        with self.assertRaisesRegex(RuntimeError, "closed"):
            watcher.start()

    def test_non_windows_default_backend_is_explicitly_unavailable(self) -> None:
        watcher = EmergencyStopWatcher(lambda: None, platform="linux")
        self.assertFalse(watcher.available)
        with self.assertRaisesRegex(EmergencyStopUnavailable, "Windows"):
            watcher.start()


class CaptureMuteGateTests(unittest.TestCase):
    def test_playback_mutes_frames_and_finish_restores_capture(self) -> None:
        gate = CaptureMuteGate()
        frame = object()
        self.assertIs(gate.filter_frame(frame), frame)

        gate.tts_started(1)
        self.assertTrue(gate.muted)
        self.assertIsNone(gate.filter_frame(frame))

        self.assertTrue(gate.tts_finished(1))
        self.assertFalse(gate.muted)
        self.assertIs(gate.filter_frame(frame), frame)

    def test_cancel_restores_capture_and_stale_generation_cannot_unmute(self) -> None:
        gate = CaptureMuteGate()
        gate.tts_started(1)
        gate.tts_started(2)
        self.assertFalse(gate.tts_cancelled(1))
        self.assertTrue(gate.muted)
        self.assertEqual(gate.active_generation, 2)

        self.assertTrue(gate.tts_cancelled(2))
        self.assertFalse(gate.muted)

        gate.tts_started(5)
        gate.tts_started(4)
        self.assertTrue(gate.muted)
        self.assertEqual(gate.active_generation, 5)
        self.assertFalse(gate.tts_finished(4))
        self.assertTrue(gate.tts_finished(5))

    def test_submit_is_atomic_with_state_transitions_under_concurrency(self) -> None:
        gate = CaptureMuteGate()
        stop = threading.Event()
        failures: list[str] = []
        delivered_while_muted = 0
        delivered_lock = threading.Lock()

        def consume(frame: bytes) -> None:
            nonlocal delivered_while_muted
            if frame != b"frame":
                failures.append("corrupt frame")
            # muted cannot change during the consumer because submit holds the
            # same lock as tts_started/finished.
            if gate.muted:
                with delivered_lock:
                    delivered_while_muted += 1

        def producer() -> None:
            try:
                while not stop.is_set():
                    gate.submit(b"frame", consume)
            except BaseException as exc:
                failures.append(repr(exc))
                stop.set()

        producers = [threading.Thread(target=producer) for _ in range(4)]
        for producer_thread in producers:
            producer_thread.start()
        try:
            for generation in range(1, 300):
                gate.tts_started(generation)
                self.assertIsNone(gate.filter_frame(b"frame"))
                gate.tts_finished(generation)
        finally:
            stop.set()
            for producer_thread in producers:
                producer_thread.join(timeout=WAIT_SECONDS)

        self.assertTrue(all(not thread.is_alive() for thread in producers))
        self.assertEqual(failures, [])
        self.assertEqual(delivered_while_muted, 0)
        self.assertFalse(gate.muted)


class EchoGuardTests(unittest.TestCase):
    def test_playback_blocks_capture_and_every_asr_text(self) -> None:
        clock = MutableClock(10.0)
        guard = EchoGuard(clock=clock)
        generation = guard.tts_started("好的，记事本已经打开。", generation=1)

        self.assertEqual(generation, 1)
        self.assertTrue(guard.playing)
        self.assertTrue(guard.mute_gate.muted)
        self.assertIsNone(guard.mute_gate.filter_frame(b"raw-pcm"))
        self.assertTrue(guard.should_drop("完全不相似的用户指令"))

    def test_default_500ms_window_drops_normalized_similar_echo(self) -> None:
        clock = MutableClock(5.0)
        guard = EchoGuard(clock=clock)
        guard.tts_started("好的，记事本 已经打开！", generation=7)
        clock.advance(1.0)
        guard.tts_finished(7)

        clock.advance(0.499)
        self.assertTrue(guard.should_drop("好的记事本已经打开", generation=7))
        clock.advance(0.001)
        self.assertTrue(guard.should_drop("好的，记事本已经打开。", generation=7))
        clock.advance(0.001)
        self.assertFalse(guard.should_drop("好的记事本已经打开", generation=7))

    def test_edit_distance_and_contains_detect_echo_but_unrelated_text_passes(self) -> None:
        clock = MutableClock(20.0)
        guard = EchoGuard(clock=clock, similarity_threshold=0.80)
        guard.tts_started("正在为你打开记事本", generation=3)
        guard.tts_finished(3)

        self.assertTrue(guard.should_drop("正在为您打开记事本"))
        self.assertTrue(guard.should_drop("打开记事本"), "contained ASR should be echo")
        self.assertTrue(guard.should_drop("系统正在为你打开记事本请稍候"))
        self.assertFalse(guard.should_drop("帮我关闭浏览器"))
        self.assertTrue(guard.accept_asr("帮我关闭浏览器"))

    def test_generation_prevents_old_tts_events_and_old_text_pollution(self) -> None:
        clock = MutableClock(30.0)
        guard = EchoGuard(clock=clock)
        guard.tts_started("旧播报内容", generation=1)
        guard.tts_started("新的播报内容", generation=2)

        self.assertFalse(guard.tts_finished(1))
        self.assertTrue(guard.playing)
        self.assertEqual(guard.generation, 2)
        self.assertTrue(guard.tts_finished(2))

        self.assertFalse(guard.should_drop("旧播报内容", generation=1))
        self.assertTrue(guard.should_drop("新的播报内容", generation=2))

        guard.tts_started("迟到的旧播报", generation=1)
        self.assertEqual(guard.generation, 2)
        self.assertFalse(guard.playing)
        self.assertTrue(guard.should_drop("新的播报内容", generation=2))
        self.assertFalse(guard.should_drop("迟到的旧播报", generation=1))

    def test_cancelled_playback_restores_capture_and_keeps_short_echo_window(self) -> None:
        clock = MutableClock(40.0)
        guard = EchoGuard(clock=clock)
        guard.tts_started("操作已取消", generation=9)
        self.assertTrue(guard.mute_gate.muted)

        self.assertTrue(guard.tts_cancelled(9))
        self.assertFalse(guard.playing)
        self.assertFalse(guard.mute_gate.muted)
        self.assertTrue(guard.should_drop("操作已取消", generation=9))

        clock.advance(0.501)
        self.assertFalse(guard.should_drop("操作已取消", generation=9))

    def test_echo_guard_state_is_thread_safe(self) -> None:
        guard = EchoGuard()
        start = threading.Barrier(5)
        failures: list[str] = []

        def writer(offset: int) -> None:
            try:
                start.wait(timeout=WAIT_SECONDS)
                for index in range(200):
                    generation = offset + index
                    guard.tts_started(f"播报 {generation}", generation=generation)
                    guard.tts_finished(generation)
                    guard.should_drop(f"播报{generation}", generation=generation)
            except BaseException as exc:
                failures.append(repr(exc))

        threads = [
            threading.Thread(target=writer, args=(offset,))
            for offset in (0, 1_000, 2_000, 3_000)
        ]
        for thread in threads:
            thread.start()
        start.wait(timeout=WAIT_SECONDS)
        for thread in threads:
            thread.join(timeout=WAIT_SECONDS)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        latest = guard.generation
        self.assertIsInstance(latest, int)
        self.assertGreaterEqual(latest, 3_000)
        self.assertFalse(guard.playing)
        self.assertFalse(guard.mute_gate.muted)

    def test_guard_retains_text_metadata_but_no_audio(self) -> None:
        guard = EchoGuard()
        guard.tts_started("只保存文本", generation=1)
        recent = guard.recent_tts
        self.assertIsNotNone(recent)
        self.assertEqual(recent.text, "只保存文本")
        self.assertNotIn("audio", vars(recent))
        self.assertNotIn("pcm", vars(recent))


if __name__ == "__main__":
    unittest.main()
