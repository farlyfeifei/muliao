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


class CancellableAudioPlayer:
    """按 generation 隔离流式音频，并用有界队列限制内存。

    ``begin()`` 会取消上一代并返回新的 generation id。只有当前 generation
    的 chunk 才能入队和写出，因此取消后的迟到网络 chunk 会被直接丢弃。
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
        self._lock = threading.Lock()
        self._generation = 0
        self._closed = False
        self._worker = thread_factory(target=self._run, name="voice-audio-player", daemon=True)
        self._worker.start()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def queued_chunks(self) -> int:
        return self._queue.qsize()

    def begin(self) -> int:
        """开始新一代播放；上一代排队内容立即失效。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("audio player is closed")
            self._generation += 1
            generation = self._generation
        self._sink.stop()
        self._drain_queue()
        self._sink.clear()
        return generation

    def play(self, pcm: bytes, *, generation: int) -> bool:
        """将 PCM chunk 交给当前 generation；过期或队列满时返回 False。"""
        chunk = bytes(pcm)
        if not chunk:
            return False
        with self._lock:
            if self._closed or generation != self._generation:
                return False
        try:
            self._queue.put_nowait((generation, chunk))
        except queue.Full:
            return False
        return True

    def stop(self, *, generation: int | None = None) -> bool:
        """取消当前代、清空排队与 sink；指定过期 generation 时不影响当前代。"""
        with self._lock:
            if self._closed:
                return False
            if generation is not None and generation != self._generation:
                return False
            self._generation += 1
        self._sink.stop()
        self._drain_queue()
        self._sink.clear()
        return True

    def clear(self) -> None:
        """清空所有尚未播放的 chunk，并清空 sink 缓冲。"""
        self._drain_queue()
        self._sink.clear()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
        self._sink.stop()
        self._drain_queue()
        try:
            self._queue.put_nowait(self._STOP)
        except queue.Full:  # pragma: no cover - drain 后仅防御竞态
            pass
        self._worker.join(timeout=1.0)
        self._sink.clear()

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return
            else:
                self._queue.task_done()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                generation, chunk = item
                with self._lock:
                    if not self._closed and generation == self._generation:
                        # 与 stop/begin 串行：它们返回后，旧代 chunk 不可能再写入 sink。
                        self._sink.write(chunk)
            finally:
                self._queue.task_done()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
