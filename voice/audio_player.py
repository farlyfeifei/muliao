"""可取消、可测试的有界 PCM 音频播放器。"""
from __future__ import annotations

from collections.abc import Callable
import queue
import threading
from typing import Protocol


class AudioSink(Protocol):
    """播放器最终写入目标；生产环境可注入真实声卡流。"""

    def write(self, pcm: bytes) -> None: ...

    def stop(self) -> None: ...

    def clear(self) -> None: ...


class NullAudioSink:
    def write(self, pcm: bytes) -> None:
        return None

    def stop(self) -> None:
        return None

    def clear(self) -> None:
        return None


class MemoryAudioSink:
    """测试用 sink，不依赖真实声卡。"""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.stop_calls = 0
        self.clear_calls = 0
        self._lock = threading.Lock()

    def write(self, pcm: bytes) -> None:
        with self._lock:
            self.chunks.append(bytes(pcm))

    def stop(self) -> None:
        with self._lock:
            self.stop_calls += 1

    def clear(self) -> None:
        with self._lock:
            self.clear_calls += 1
            self.chunks.clear()


class PyAudioPcmSink:
    """24kHz PCM16LE mono 声卡 sink；延迟打开设备，可安全 stop/clear/close。"""

    def __init__(
        self,
        *,
        sample_rate: int = 24_000,
        channels: int = 1,
        sample_width: int = 2,
        pyaudio_factory=None,
        output_device_index: int | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width
        self.output_device_index = output_device_index
        self._pyaudio_factory = pyaudio_factory
        self._audio = None
        self._stream = None
        self._lock = threading.RLock()

    def _ensure_stream(self):
        with self._lock:
            if self._stream is not None:
                is_stopped = getattr(self._stream, "is_stopped", None)
                if callable(is_stopped) and is_stopped():
                    start_stream = getattr(self._stream, "start_stream", None)
                    if callable(start_stream):
                        start_stream()
                return self._stream
            import pyaudio

            self._audio = (self._pyaudio_factory or pyaudio.PyAudio)()
            self._stream = self._audio.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                output=True,
                output_device_index=self.output_device_index,
            )
            return self._stream

    def write(self, pcm: bytes) -> None:
        self._ensure_stream().write(bytes(pcm))

    def stop(self) -> None:
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.stop_stream()
                except Exception:
                    pass

    def clear(self) -> None:
        # PortAudio blocking stream has no queued Python-side buffer beyond the player queue.
        return None

    def close(self) -> None:
        with self._lock:
            stream, audio = self._stream, self._audio
            self._stream = None
            self._audio = None
        if stream is not None:
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        if audio is not None:
            audio.terminate()


class CancellableAudioPlayer:
    """按 generation 隔离流式音频，并用有界队列限制内存。

    ``begin()``、``stop()``、``clear()`` 与 ``close()`` 是完整串行的状态转换。
    worker 绝不持状态锁调用可能阻塞的 ``sink.write()``；转换先使旧 generation
    失效，再调用 ``sink.stop()`` 打断写入，并等待旧代所有已接收 chunk 完成或丢弃。
    """

    _STOP = object()

    def __init__(
        self,
        sink: AudioSink | None = None,
        *,
        max_queue_chunks: int = 32,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        if max_queue_chunks < 1:
            raise ValueError("max_queue_chunks must be positive")
        self._sink = sink or NullAudioSink()
        self._queue: queue.Queue[tuple[int, bytes] | object] = queue.Queue(
            maxsize=max_queue_chunks
        )
        # transition_lock makes each public state transition indivisible.  The
        # condition protects generation/pending state only and is never held over
        # sink calls, especially the potentially blocking write().
        self._transition_lock = threading.Lock()
        self._state = threading.Condition(threading.Lock())
        self._generation = 0
        self._pending: dict[int, int] = {}
        self._failures: dict[int, BaseException] = {}
        self._closed = False
        self._close_complete = threading.Event()
        self._worker = thread_factory(target=self._run, name="voice-audio-player", daemon=True)
        self._worker.start()

    @property
    def generation(self) -> int:
        with self._state:
            return self._generation

    @property
    def queued_chunks(self) -> int:
        return self._queue.qsize()

    def begin(self) -> int:
        """开始新一代播放；返回前上一代已停止且其队列已清空。"""
        with self._transition_lock:
            with self._state:
                if self._closed:
                    raise RuntimeError("audio player is closed")
                previous = self._generation
                self._generation += 1
                generation = self._generation
                self._failures.pop(generation, None)
                for stale in tuple(self._failures):
                    if stale < previous:
                        self._failures.pop(stale, None)
            self._interrupt_generation(previous)
            self._sink.clear()
            return generation

    def play(self, pcm: bytes, *, generation: int) -> bool:
        """将 PCM chunk 交给当前 generation；过期或队列满时返回 False。"""
        chunk = bytes(pcm)
        if not chunk:
            return False
        # Keeping the transition lock through put closes the check/close race:
        # close cannot drain the queue and enqueue its sole sentinel between them.
        with self._transition_lock:
            with self._state:
                if self._closed or generation != self._generation:
                    return False
                self._pending[generation] = self._pending.get(generation, 0) + 1
                try:
                    self._queue.put_nowait((generation, chunk))
                except queue.Full:
                    self._decrement_pending_locked(generation)
                    return False
        return True

    def stop(self, *, generation: int | None = None) -> bool:
        """取消当前代；返回后该代不可能再调用或停留在 ``sink.write`` 中。"""
        with self._transition_lock:
            with self._state:
                if self._closed:
                    return False
                if generation is not None and generation != self._generation:
                    return False
                previous = self._generation
                self._generation += 1
            self._interrupt_generation(previous)
            self._sink.clear()
            return True

    def clear(self) -> None:
        """取消当前代并清空尚未播放及 sink 内的音频。"""
        # clear invalidates the current generation so an item already removed from
        # Queue cannot become writable after the drain.
        self.stop()

    def is_drained(self, generation: int) -> bool:
        """该 generation 是否已无排队或正在写入的 chunk。"""
        with self._state:
            return self._pending.get(generation, 0) == 0

    def failure(self, generation: int) -> BaseException | None:
        """返回该 generation 的声卡写入异常；读取不清除。"""
        with self._state:
            return self._failures.get(generation)

    def wait_until_drained(self, generation: int, timeout: float | None = None) -> bool:
        """等待 generation 播放/丢弃完毕；超时返回 False。"""
        with self._state:
            return self._state.wait_for(
                lambda: self._pending.get(generation, 0) == 0,
                timeout=timeout,
            )

    def close(self) -> None:
        first_closer = False
        with self._transition_lock:
            with self._state:
                if self._closed:
                    complete = self._close_complete
                else:
                    self._closed = True
                    previous = self._generation
                    self._generation += 1
                    complete = self._close_complete
                    first_closer = True
            if first_closer:
                self._interrupt_generation(previous)
                # The queue is empty and play is excluded by transition_lock,
                # so this put cannot lose or race the worker's sole stop signal.
                self._queue.put_nowait(self._STOP)
        if not first_closer:
            complete.wait()
            return

        try:
            # No timeout: sink.stop() plus the pending wait above guarantee that a
            # cooperative blocking sink has left write(), and the sentinel must be
            # consumed before close reports completion.
            self._worker.join()
            self._sink.clear()
        finally:
            complete.set()

    def _interrupt_generation(self, generation: int) -> None:
        """Stop, drain, and quiesce one generation despite check/write races."""
        self._sink.stop()
        self._drain_queue()
        while True:
            with self._state:
                pending = self._pending.get(generation, 0)
                if pending == 0:
                    return
                # Give a worker that already passed its check a chance to enter
                # write, then interrupt again.  No control lock is held while the
                # blocking sink is called.
                self._state.wait(timeout=0.01)
            self._sink.stop()
            self._drain_queue()

    def _decrement_pending_locked(self, generation: int) -> None:
        remaining = self._pending.get(generation, 0) - 1
        if remaining > 0:
            self._pending[generation] = remaining
        else:
            self._pending.pop(generation, None)
        self._state.notify_all()

    def _drain_queue(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                if item is self._STOP:  # defensive; public transitions never drain it
                    self._queue.put_nowait(self._STOP)
                    return
                generation, _ = item
                with self._state:
                    self._decrement_pending_locked(generation)
            finally:
                self._queue.task_done()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                generation, chunk = item
                with self._state:
                    should_write = (
                        not self._closed and generation == self._generation
                    )
                if should_write:
                    try:
                        self._sink.write(chunk)
                    except BaseException as exc:
                        # 记录到 generation，等待方将其提升为结构化 TTS 失败，触发本地降级。
                        with self._state:
                            self._failures.setdefault(generation, exc)
                            self._state.notify_all()
            finally:
                if item is not self._STOP:
                    generation, _ = item
                    with self._state:
                        self._decrement_pending_locked(generation)
                self._queue.task_done()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
