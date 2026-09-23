"""小米 MiMo Chat Completions 云端 ASR 适配器。"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import io
import wave
from typing import Any

import httpx

from .cancellation import CancellationToken
from .contracts import AudioSegment, Transcript


MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
MAX_BASE64_BYTES = 10 * 1024 * 1024
_SUPPORTED_MIME_TYPES = frozenset({"audio/wav", "audio/mpeg", "audio/mp3"})


@dataclass
class MiMoAsrError(RuntimeError):
    """调用方可据 ``code``/``retryable`` 决定是否降级到本地 ASR。"""

    code: str
    message: str
    status_code: int | None = None
    retryable: bool = False

    def __str__(self) -> str:
        return self.message


class MiMoAsrClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "mimo-v2.5-asr",
        base_url: str = MIMO_BASE_URL,
        timeout: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = client or httpx.Client(
            timeout=timeout,
            trust_env=False,
            verify=True,
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def transcribe(
        self,
        audio: AudioSegment | bytes,
        *,
        mime_type: str | None = None,
        language: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> Transcript:
        """转写已通过本地唤醒门控的音频。

        ``AudioSegment`` 会被正确封装为 WAV；裸 bytes 仅保留给已编码 WAV/MP3 调用方。
        本适配器不得作为唤醒前的首层 recognizer。
        """
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if isinstance(audio, AudioSegment):
            audio_bytes = _audio_segment_to_wav(audio)
            effective_mime = "audio/wav"
        else:
            audio_bytes = bytes(audio)
            effective_mime = mime_type or "audio/wav"
        if not self.api_key:
            raise MiMoAsrError("missing_api_key", "MiMo ASR API key is missing")
        if effective_mime not in _SUPPORTED_MIME_TYPES:
            raise MiMoAsrError(
                "unsupported_mime_type",
                f"MiMo ASR supports WAV/MP3, not {effective_mime!r}",
            )
        encoded = base64.b64encode(audio_bytes)
        if len(encoded) > MAX_BASE64_BYTES:
            raise MiMoAsrError(
                "audio_too_large",
                "MiMo ASR Base64 audio exceeds 10 MB",
            )

        data_uri = f"data:{effective_mime};base64,{encoded.decode('ascii')}"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": data_uri},
                        }
                    ],
                }
            ],
        }
        if language:
            body["asr_options"] = {"language": language}

        try:
            response = self._client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "api-key": self.api_key,
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise MiMoAsrError(
                "timeout", "MiMo ASR request timed out", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise MiMoAsrError(
                "network_error", "MiMo ASR network request failed", retryable=True
            ) from exc

        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if response.status_code >= 400:
            retryable = response.status_code == 429 or response.status_code >= 500
            raise MiMoAsrError(
                "rate_limited" if response.status_code == 429 else "http_error",
                f"MiMo ASR returned HTTP {response.status_code}",
                status_code=response.status_code,
                retryable=retryable,
            )

        try:
            payload = response.json()
            text = _extract_transcript(payload)
        except (TypeError, ValueError, KeyError) as exc:
            raise MiMoAsrError(
                "invalid_response", "MiMo ASR returned an invalid response"
            ) from exc
        if not text:
            raise MiMoAsrError(
                "empty_transcript", "MiMo ASR returned no transcript"
            )
        return Transcript(
            text=text,
            language=language or "auto",
            metadata={"provider": "mimo", "model": self.model},
        )


def _audio_segment_to_wav(audio: AudioSegment) -> bytes:
    if audio.channels < 1 or audio.sample_width not in {1, 2, 3, 4}:
        raise MiMoAsrError("invalid_audio", "AudioSegment has an unsupported PCM format")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(audio.channels)
        wav_file.setsampwidth(audio.sample_width)
        wav_file.setframerate(audio.sample_rate)
        wav_file.writeframes(audio.pcm)
    return output.getvalue()


def _extract_transcript(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise TypeError("response must be an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise KeyError("choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise TypeError("choice must be an object")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise KeyError("message")
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts).strip()
    raise TypeError("message.content must be text")
