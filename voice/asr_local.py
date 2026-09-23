"""SenseVoice-Small 的延迟加载封装。"""
from __future__ import annotations

from pathlib import Path
import threading

import numpy as np

from .contracts import AudioSegment, Transcript


class SenseVoiceRecognizer:
    def __init__(
        self,
        model_dir: str | Path,
        *,
        num_threads: int = 4,
        language: str = "zh",
        use_itn: bool = True,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.num_threads = num_threads
        self.language = language
        self.use_itn = use_itn
        self._recognizer = None
        self._lock = threading.Lock()

    def _get_recognizer(self):
        with self._lock:
            if self._recognizer is not None:
                return self._recognizer
            model = self.model_dir / "model.int8.onnx"
            tokens = self.model_dir / "tokens.txt"
            missing = [str(path) for path in (model, tokens) if not path.is_file()]
            if missing:
                raise FileNotFoundError("missing SenseVoice assets: " + ", ".join(missing))
            import sherpa_onnx

            self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(model),
                tokens=str(tokens),
                num_threads=self.num_threads,
                debug=False,
                use_itn=self.use_itn,
                language=self.language,
            )
            return self._recognizer

    def transcribe(self, audio: AudioSegment) -> Transcript:
        if audio.channels != 1 or audio.sample_width != 2:
            raise ValueError("SenseVoice expects 16-bit mono PCM")
        if len(audio.pcm) % 2:
            raise ValueError("PCM byte length must align to int16 samples")
        samples = np.frombuffer(audio.pcm, dtype=np.int16).astype(np.float32) / 32768.0
        recognizer = self._get_recognizer()
        stream = recognizer.create_stream()
        stream.accept_waveform(audio.sample_rate, samples)
        recognizer.decode_stream(stream)
        result = stream.result
        text = str(result.text or "").strip()
        metadata = {
            "emotion": getattr(result, "emotion", ""),
            "event": getattr(result, "event", ""),
            "lang": getattr(result, "lang", self.language),
            "timestamps": list(getattr(result, "timestamps", []) or []),
        }
        return Transcript(text=text, language=metadata["lang"] or self.language, metadata=metadata)
