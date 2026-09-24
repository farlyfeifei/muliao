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


def _bool_setting(name: str, default: bool) -> bool:
    value = _setting(name, "1" if default else "0").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _float_setting(name: str, default: float) -> float:
    try:
        return float(_setting(name, str(default)))
    except (TypeError, ValueError):
        return default


def _non_negative_float_setting(name: str, default: float) -> float:
    """Clamp a negative value to 0.0 so an operator's -1 means 'disable', not crash."""

    return max(0.0, _float_setting(name, default))


@dataclass(frozen=True)
class VoiceSettings:
    # 保留 M0 的前四个必填字段和其后位置参数顺序；新增云配置全部追加并提供安全默认值。
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
    mimo_base_url: str = "https://api.xiaomimimo.com/v1"
    mimo_api_key: str = ""
    mimo_tts_model: str = "mimo-v2.5-tts"
    mimo_tts_voice: str = "冰糖"
    mimo_tts_enabled: bool = False
    mimo_asr_model: str = "mimo-v2.5-asr"
    mimo_asr_enabled: bool = False
    vits_dir: Path = Path("C:/ProgramData/Muliao/models/vits-aishell3")
    vits_tts_enabled: bool = False
    jev_cache_enabled: bool = True
    jev_cache_seconds: float = 300.0
    # Desktop GOAL real execution is OFF by default. Even with act=True the GOAL
    # channel stays dry-run unless this explicit opt-in is set, so a multi-step
    # desktop task never touches the machine without the user turning it on.
    voice_goal_act_enabled: bool = False
    # WEB-GOAL 浏览器通道：默认关（安全优先），显式开启才用真实 Chrome/Edge。
    web_goal_enabled: bool = False
    web_backend: str = "fake"          # "fake"=内存假后端 | "chrome"=真实 CDP
    web_browser_path: str = ""         # 留空则自动探测 Edge/Chrome
    web_headless: bool = True
    web_nav_timeout_seconds: float = 20.0

    @classmethod
    def load(cls) -> "VoiceSettings":
        # 与 voice-models.json 清单里 sensevoice 的 default_path 保持一致
        # （C:/ProgramData/Muliao/models/sensevoice）。此前这里默认指向 LOCALAPPDATA，
        # 与清单分歧，导致模型按清单下载好后运行时仍报 missing file。
        default_model = Path(
            _setting(
                "MULIAO_VOICE_MODELS_ROOT",
                os.path.join(os.environ.get("PROGRAMDATA") or "C:/ProgramData", "Muliao", "models"),
            )
        ) / "sensevoice"
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
            mimo_base_url=_setting("MULIAO_MIMO_BASE_URL", "https://api.xiaomimimo.com/v1"),
            mimo_api_key=_setting("MULIAO_MIMO_API_KEY"),
            mimo_tts_model=_setting("MULIAO_MIMO_TTS_MODEL", "mimo-v2.5-tts"),
            mimo_tts_voice=_setting("MULIAO_MIMO_TTS_VOICE", "冰糖"),
            mimo_tts_enabled=_bool_setting("MULIAO_MIMO_TTS_ENABLED", True),
            mimo_asr_model=_setting("MULIAO_MIMO_ASR_MODEL", "mimo-v2.5-asr"),
            mimo_asr_enabled=_bool_setting("MULIAO_MIMO_ASR_ENABLED", False),
            vits_dir=Path(
                os.path.expandvars(
                    os.path.expanduser(
                        _setting(
                            "MULIAO_VOICE_VITS_AISHELL3_DIR",
                            "C:/ProgramData/Muliao/models/vits-aishell3",
                        )
                    )
                )
            ),
            vits_tts_enabled=_bool_setting("MULIAO_VOICE_VITS_TTS_ENABLED", False),
            jev_cache_enabled=_bool_setting("MULIAO_JEV_CACHE_ENABLED", True),
            jev_cache_seconds=_non_negative_float_setting("MULIAO_JEV_CACHE_SECONDS", 300.0),
            voice_goal_act_enabled=_bool_setting("MULIAO_VOICE_GOAL_ACT_ENABLED", False),
            web_goal_enabled=_bool_setting("MULIAO_VOICE_WEB_GOAL_ENABLED", False),
            web_backend=_setting("MULIAO_VOICE_WEB_BACKEND", "fake").strip().lower(),
            web_browser_path=_setting("MULIAO_VOICE_WEB_BROWSER_PATH", "").strip(),
            web_headless=_bool_setting("MULIAO_VOICE_WEB_HEADLESS", True),
            web_nav_timeout_seconds=_non_negative_float_setting(
                "MULIAO_VOICE_WEB_NAV_TIMEOUT", 20.0
            ),
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
