"""幕僚“言出法随”独立语音子系统。

语音代码不依赖聊天会话、缓存账本或蜂群工具注册。共享入口由集成分支接线。
"""

from .contracts import (
    ActionResult,
    AudioSegment,
    RouteDecision,
    Transcript,
    VoiceResult,
)
from .engine import VoiceEngine
from .wake import WakeDetector

__all__ = [
    "ActionResult",
    "AudioSegment",
    "RouteDecision",
    "Transcript",
    "VoiceEngine",
    "VoiceResult",
    "WakeDetector",
]
