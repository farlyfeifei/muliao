"""小米 MiMo Chat Completions 流式 TTS 适配器。"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import threading
import time
from typing import Any, Iterable

import httpx

from .audio_player import CancellableAudioPlayer
from .cancellation import CancellationToken


MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"


@dataclass
class MiMoTtsError(RuntimeError):
    """调用方可据结构化字段立即降级，不在实时路径自动重试。"""

    code: str
    message: str
    status_code: int | None = None
    retryable: bool = False
    fallback_recommended: bool = True

    def __str__(self) -> str:
        return self.message


class MiMoTtsClient:
    def __init__(
        self,
        *,
        api_key: str,
        player: CancellableAudioPlayer,
        model: str = "mimo-v2.5-tts",
        voice: str = "Chloe",
        base_url: str = MIMO_BASE_URL,
        timeout: float = 15.0,
        first_audio_timeout: float = 1.2,
        client: httpx.Client | None = None,
    ) -> None:
        if first_audio_timeout <= 0:
            raise ValueError("first_audio_timeout must be positive")
        self.api_key = api_key
        self.player = player
        self.model = model
        self.voice = voice
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.first_audio_timeout = first_audio_timeout
        self._client = client or httpx.Client(
            timeout=timeout,
            trust_env=False,
            verify=True,
        )
        self._owns_client = client is None
        self._lock = threading.Lock()
        self._generation = 0
        self._active_token: CancellationToken | None = None
        self._active_player_generation: int | None = None

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def close(self) -> None:
        self.cancel()
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def cancel(self, generation: int | None = None) -> bool:
        """取消当前合成；迟到 chunk 会因 generation/token 双检查被丢弃。"""
        with self._lock:
            if generation is not None and generation != self._generation:
                return False
            token = self._active_token
            player_generation = self._active_player_generation
            self._generation += 1
            self._active_token = None
            self._active_player_generation = None
        if token is not None:
            token.cancel()
        if player_generation is not None:
            self.player.stop(generation=player_generation)
        return token is not None or player_generation is not None

    def speak(
        self,
        text: str,
        *,
        instruction: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> int:
        """同步消费 SSE，第一段有效音频到达即交给播放器。"""
        if not self.api_key:
            raise MiMoTtsError("missing_api_key", "MiMo TTS API key is missing")
        if not str(text).strip():
            raise MiMoTtsError("empty_text", "MiMo TTS text is empty")

        token = cancellation or CancellationToken()
        with self._lock:
            previous = self._active_token
            previous_player_generation = self._active_player_generation
            self._generation += 1
            generation = self._generation
            self._active_token = token
            self._active_player_generation = None
        if previous is not None:
            previous.cancel()
        if previous_player_generation is not None:
            self.player.stop(generation=previous_player_generation)
        player_generation = self.player.begin()
        with self._lock:
            if generation != self._generation or token.cancelled:
                self.player.stop(generation=player_generation)
                return generation
            self._active_player_generation = player_generation

        messages: list[dict[str, str]] = []
        if instruction:
            messages.append({"role": "user", "content": instruction})
        messages.append({"role": "assistant", "content": str(text)})
        body = {
            "model": self.model,
            "messages": messages,
            "audio": {"format": "pcm16", "voice": self.voice},
            "stream": True,
        }
        deadline = time.monotonic() + self.first_audio_timeout
        received_audio = False
        request_timeout = httpx.Timeout(min(self.timeout, self.first_audio_timeout))
        try:
            with self._client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers={
                    "api-key": self.api_key,
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                json=body,
                timeout=request_timeout,
            ) as response:
                if response.status_code >= 400:
                    raise _http_error(response.status_code)
                for event in _iter_sse_events(response.iter_lines()):
                    if self._cancelled(generation, token):
                        break
                    if not received_audio and time.monotonic() > deadline:
                        raise MiMoTtsError(
                            "first_audio_timeout",
                            f"MiMo TTS produced no audio within {self.first_audio_timeout:g} seconds",
                            retryable=True,
                        )
                    chunk = _extract_audio_chunk(event)
                    if not chunk:
                        continue
                    if self._cancelled(generation, token):
                        break
                    received_audio = True
                    self.player.play(chunk, generation=player_generation)
                if not received_audio and not self._cancelled(generation, token):
                    code = (
                        "first_audio_timeout"
                        if time.monotonic() > deadline
                        else "empty_audio"
                    )
                    raise MiMoTtsError(
                        code,
                        "MiMo TTS stream ended before the first audio chunk",
                        retryable=code == "first_audio_timeout",
                    )
        except MiMoTtsError:
            self.player.stop(generation=player_generation)
            raise
        except httpx.TimeoutException as exc:
            self.player.stop(generation=player_generation)
            raise MiMoTtsError(
                "first_audio_timeout" if not received_audio else "stream_timeout",
                "MiMo TTS request timed out",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            self.player.stop(generation=player_generation)
            raise MiMoTtsError(
                "network_error", "MiMo TTS network request failed", retryable=True
            ) from exc
        except (TypeError, ValueError) as exc:
            self.player.stop(generation=player_generation)
            raise MiMoTtsError(
                "invalid_response", "MiMo TTS returned an invalid stream"
            ) from exc
        finally:
            if self._cancelled(generation, token):
                self.player.stop(generation=player_generation)
            with self._lock:
                if generation == self._generation:
                    self._active_token = None
                    self._active_player_generation = None
        return generation

    def _cancelled(self, generation: int, token: CancellationToken) -> bool:
        if token.cancelled:
            return True
        with self._lock:
            return generation != self._generation


def _http_error(status_code: int) -> MiMoTtsError:
    if status_code == 429:
        return MiMoTtsError(
            "rate_limited",
            "MiMo TTS rate limited the request",
            status_code=status_code,
            retryable=True,
        )
    return MiMoTtsError(
        "service_unavailable" if status_code >= 500 else "http_error",
        f"MiMo TTS returned HTTP {status_code}",
        status_code=status_code,
        retryable=status_code >= 500,
    )


def _iter_sse_events(lines: Iterable[str | bytes]) -> Iterable[dict[str, Any]]:
    """逐事件解析 SSE；注释、空行、空壳 JSON 与 ``[DONE]`` 均忽略。"""
    data_lines: list[str] = []
    for raw_line in lines:
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        line = line.rstrip("\r")
        if not line:
            event = _decode_sse_data(data_lines)
            data_lines.clear()
            if event is not None:
                yield event
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    event = _decode_sse_data(data_lines)
    if event is not None:
        yield event


def _decode_sse_data(data_lines: list[str]) -> dict[str, Any] | None:
    if not data_lines:
        return None
    data = "\n".join(data_lines).strip()
    if not data or data == "[DONE]":
        return None
    payload = json.loads(data)
    if not isinstance(payload, dict) or not payload:
        return None
    return payload


def _extract_audio_chunk(payload: dict[str, Any]) -> bytes | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, dict):
        return None
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        return None
    audio = delta.get("audio")
    if not isinstance(audio, dict):
        return None
    data = audio.get("data")
    if not isinstance(data, str) or not data:
        return None
    return base64.b64decode(data, validate=True)
