"""固定唤醒词“幕僚幕僚”的本地文本门控。"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


_WAKE_WORD = "幕僚"
# ASR 常见的空白和标点间隔；M0 只做离线文本门控，真实 0–800ms 时间戳验收留给硬件测试。
_GAP = r"[\s,，。.!！?？、…·:：;；\-—_]*"
_WAKE_RE = re.compile(rf"^\s*{_WAKE_WORD}{_GAP}{_WAKE_WORD}(?P<rest>.*)$")


@dataclass(frozen=True)
class WakeMatch:
    command: str
    normalized_transcript: str


def normalize_transcript(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).strip()


class WakeDetector:
    """检测并剥离固定唤醒词。

    只接受句首的两个“幕僚”。单个“幕僚”、倒序或句中偶然出现都不会通过。
    """

    wake_phrase = "幕僚幕僚"

    def detect(self, transcript: str) -> WakeMatch | None:
        normalized = normalize_transcript(transcript)
        match = _WAKE_RE.match(normalized)
        if not match:
            return None
        command = match.group("rest").lstrip(" \t,，。.!！?？、…·:：;；-—_")
        return WakeMatch(command=command.strip(), normalized_transcript=normalized)
