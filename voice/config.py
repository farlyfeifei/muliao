"""语音子系统配置：只读环境变量或仓库外 JSON，不保存任何密钥。"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any


def _external_config() -> dict[str, Any]:
    configured = os.environ.get("MULIAO_CONFIG")
    if configured:
        path = Path(os.path.expandvars(os.path.expanduser(configured)))
    else:
        appdata = Path(os.environ.get("APPDATA") or Path.home())
        path = appdata / "Muliao" / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _setting(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is not None:
        return value
    value = _external_config().get(name, default)
    return str(value) if value is not None else default


def _int_setting(name: str, default: int) -> int:
    try:
        return int(_setting(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class VoiceSettings:
    sensevoice_dir: Path
    jev_url: str
    jev_key: str
    jev_model: str
    sample_rate: int = 16_000
    frame_ms: int = 30
    vad_mode: int = 2
    silence_ms: int = 600
    pre_roll_ms: int = 300
    max_utterance_ms: int = 12_000
    listen_timeout_ms: int = 15_000
    min_speech_ms: int = 300
    input_device_index: int | None = None

    @classmethod
    def load(cls) -> "VoiceSettings":
        default_model = (
            Path(os.environ.get("LOCALAPPDATA") or Path.home())
            / "Muliao"
            / "models"
            / "sensevoice"
        )
        raw_device = _setting("MULIAO_VOICE_INPUT_DEVICE", "").strip()
        try:
            device = int(raw_device) if raw_device else None
        except ValueError:
            device = None
        return cls(
            sensevoice_dir=Path(
                os.path.expandvars(
                    os.path.expanduser(_setting("MULIAO_VOICE_SENSEVOICE_DIR", str(default_model)))
                )
            ).resolve(),
            jev_url=_setting("MULIAO_JEV_URL", "https://api.typesafe.ai/v1/systemone"),
            jev_key=_setting("MULIAO_JEV_KEY"),
            jev_model=_setting("MULIAO_JEV_MODEL", "jev-latest"),
            sample_rate=_int_setting("MULIAO_VOICE_SAMPLE_RATE", 16_000),
            frame_ms=_int_setting("MULIAO_VOICE_FRAME_MS", 30),
            vad_mode=_int_setting("MULIAO_VOICE_VAD_MODE", 2),
            silence_ms=_int_setting("MULIAO_VOICE_SILENCE_MS", 600),
            pre_roll_ms=_int_setting("MULIAO_VOICE_PRE_ROLL_MS", 300),
            max_utterance_ms=_int_setting("MULIAO_VOICE_MAX_UTTERANCE_MS", 12_000),
            listen_timeout_ms=_int_setting("MULIAO_VOICE_LISTEN_TIMEOUT_MS", 15_000),
            min_speech_ms=_int_setting("MULIAO_VOICE_MIN_SPEECH_MS", 300),
            input_device_index=device,
        )

    def validate_m0(self) -> None:
        if self.sample_rate not in {8_000, 16_000, 32_000, 48_000}:
            raise ValueError("webrtcvad sample rate must be 8/16/32/48 kHz")
        if self.frame_ms not in {10, 20, 30}:
            raise ValueError("webrtcvad frame_ms must be 10, 20, or 30")
        if not 0 <= self.vad_mode <= 3:
            raise ValueError("webrtcvad mode must be between 0 and 3")
        if self.silence_ms < self.frame_ms:
            raise ValueError("silence_ms must be at least one frame")
        if self.min_speech_ms < self.frame_ms:
            raise ValueError("min_speech_ms must be at least one frame")
        if self.listen_timeout_ms < self.frame_ms:
            raise ValueError("listen_timeout_ms must be at least one frame")
