"""PyAudio + WebRTC VAD 单句采集。"""
from __future__ import annotations

from collections import deque
from typing import Any, Callable

from .contracts import AudioSegment


class PyAudioVADCapture:
    """等待说话开始，并在连续静音后返回一个 PCM 语音段。"""

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        frame_ms: int = 30,
        vad_mode: int = 2,
        silence_ms: int = 600,
        pre_roll_ms: int = 300,
        max_utterance_ms: int = 12_000,
        listen_timeout_ms: int = 15_000,
        min_speech_ms: int = 300,
        input_device_index: int | None = None,
        pyaudio_factory: Callable[[], Any] | None = None,
        vad_factory: Callable[[int], Any] | None = None,
    ) -> None:
        if sample_rate not in {8_000, 16_000, 32_000, 48_000}:
            raise ValueError("unsupported VAD sample rate")
        if frame_ms not in {10, 20, 30}:
            raise ValueError("frame_ms must be 10, 20, or 30")
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.vad_mode = vad_mode
        self.silence_ms = silence_ms
        self.pre_roll_ms = pre_roll_ms
        self.max_utterance_ms = max_utterance_ms
        self.listen_timeout_ms = listen_timeout_ms
        self.min_speech_ms = min_speech_ms
        self.input_device_index = input_device_index
        self._pyaudio_factory = pyaudio_factory
        self._vad_factory = vad_factory

    @property
    def samples_per_frame(self) -> int:
        return self.sample_rate * self.frame_ms // 1000

    def capture_utterance(self) -> AudioSegment:
        import pyaudio
        import webrtcvad

        audio = (self._pyaudio_factory or pyaudio.PyAudio)()
        vad = (self._vad_factory or webrtcvad.Vad)(self.vad_mode)
        stream = None
        try:
            stream = audio.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self.sample_rate,
                input=True,
                input_device_index=self.input_device_index,
                frames_per_buffer=self.samples_per_frame,
            )
            return self._read_segment(stream, vad)
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            audio.terminate()

    def _read_segment(self, stream: Any, vad: Any) -> AudioSegment:
        pre_roll_frames = max(1, self.pre_roll_ms // self.frame_ms)
        silence_frames = max(1, self.silence_ms // self.frame_ms)
        min_speech_frames = max(1, self.min_speech_ms // self.frame_ms)
        max_frames = max(1, self.max_utterance_ms // self.frame_ms)
        listen_frames = max(1, self.listen_timeout_ms // self.frame_ms)
        pre_roll: deque[bytes] = deque(maxlen=pre_roll_frames)
        frames: list[bytes] = []
        started = False
        speech_count = 0
        trailing_silence = 0
        frames_read = 0

        while len(frames) < max_frames:
            if not started and frames_read >= listen_frames:
                break
            frame = stream.read(self.samples_per_frame, exception_on_overflow=False)
            frames_read += 1
            if len(frame) != self.samples_per_frame * 2:
                continue
            speaking = bool(vad.is_speech(frame, self.sample_rate))
            if not started:
                pre_roll.append(frame)
                if not speaking:
                    continue
                started = True
                frames.extend(pre_roll)
                speech_count = 1
                continue

            frames.append(frame)
            if speaking:
                speech_count += 1
                trailing_silence = 0
            else:
                trailing_silence += 1
                if trailing_silence >= silence_frames:
                    break

        if not started:
            raise TimeoutError("no speech detected before capture timeout")
        if speech_count < min_speech_frames:
            raise ValueError("speech segment is shorter than min_speech_ms")
        return AudioSegment(pcm=b"".join(frames), sample_rate=self.sample_rate)
