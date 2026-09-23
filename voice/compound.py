"""保守、可预检的复合命令拆分器。"""
from __future__ import annotations

from dataclasses import dataclass
import re


_ACTION_START = (
    r"(?:打开|访问|启动|搜索|搜|查找|输入|键入|打字|关闭|调高|调低|增大|降低|"
    r"音量|静音|取消静音|播放|暂停|下一首|上一首|截图|截屏|按下|按|切换|停止|取消|"
    r"删除|移除|清空|发送|付款|支付|购买|卸载|格式化|锁定|"
    r"open\b|visit\b|launch\b|search\b|find\b|type\b|enter\b|close\b|"
    r"volume\b|mute\b|unmute\b|play\b|pause\b|next\b|previous\b|press\b|stop\b|cancel\b|"
    r"take\s+(?:a\s+)?screenshot\b|screenshot\b)"
)
_CONNECTOR = r"(?:然后|接着|随后|并且|以及|并|再|and\s+then|then)"
_SPLIT_RE = re.compile(
    rf"(?:\s*(?:[,，;；。]\s*)?{_CONNECTOR}\s*(?={_ACTION_START})|"
    rf"\s*[,，;；。]\s*(?={_ACTION_START}))",
    re.IGNORECASE,
)
_BROAD_CONNECTOR_RE = re.compile(_CONNECTOR, re.IGNORECASE)
_LEADING_CONNECTOR_RE = re.compile(
    rf"^\s*(?:然后|接着|随后|再|and\s+then|then)\s*(?={_ACTION_START})",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION_RE = re.compile(r"^[\s,，;；。.!！?？…]*$")


@dataclass(frozen=True)
class SplitResult:
    parts: tuple[str, ...]
    malformed: bool = False
    reason: str = ""


def _is_word_apostrophe(text: str, index: int) -> bool:
    return (
        text[index] == "'"
        and index > 0
        and index + 1 < len(text)
        and text[index - 1].isalnum()
        and text[index + 1].isalnum()
    )


def _mask_quoted(text: str) -> tuple[str, bool]:
    """Mask only valid quote pairs; return ``balanced=False`` for unclosed quotes."""

    chars = list(text)
    pairs = {'"': '"', "'": "'", "“": "”", "‘": "’"}
    index = 0
    while index < len(text):
        opener = text[index]
        if opener not in pairs or _is_word_apostrophe(text, index):
            index += 1
            continue
        closer = pairs[opener]
        close_at = text.find(closer, index + 1)
        if close_at < 0:
            return text, False
        for cursor in range(index, close_at + 1):
            chars[cursor] = "\0"
        index = close_at + 1
    return "".join(chars), True


def _malformed_connector(masked: str, split_spans: list[tuple[int, int]]) -> str:
    """Find trailing/repeated connectors that cannot be safely treated as payload."""

    valid_ranges = split_spans
    for match in _BROAD_CONNECTOR_RE.finditer(masked):
        if any(left <= match.start() and match.end() <= right for left, right in valid_ranges):
            continue
        suffix = masked[match.end():]
        # A connector at the end (or followed only by punctuation) is incomplete.
        if _TRAILING_PUNCTUATION_RE.fullmatch(suffix):
            return "trailing connector"
        # Repeated connectors are always malformed, even if the second one has an action.
        next_match = _BROAD_CONNECTOR_RE.match(suffix.lstrip(" \t,，;；。"))
        if next_match is not None:
            return "repeated connector"
    return ""


class CommandSplitter:
    def parse(self, command: str) -> SplitResult:
        text = str(command or "").strip()
        if not text:
            return SplitResult(())
        masked, balanced = _mask_quoted(text)
        if not balanced:
            return SplitResult((), True, "unclosed quote")
        leading = _LEADING_CONNECTOR_RE.match(masked)
        if leading is not None:
            text = text[leading.end():].lstrip(" \t,，。;；")
            masked = masked[leading.end():].lstrip(" \t,，。;；")
            if not text:
                return SplitResult((), True, "empty command after connector")
        spans = [match.span() for match in _SPLIT_RE.finditer(masked)]
        malformed = _malformed_connector(masked, spans)
        if malformed:
            return SplitResult((), True, malformed)
        if not spans:
            return SplitResult((text,))

        parts: list[str] = []
        start = 0
        for left, right in spans:
            part = text[start:left].strip(" \t,，。;；")
            if not part:
                return SplitResult((), True, "empty compound step")
            parts.append(part)
            start = right
        tail = text[start:].strip(" \t,，。;；")
        if not tail:
            return SplitResult((), True, "empty compound tail")
        parts.append(tail)
        return SplitResult(tuple(parts))

    def split(self, command: str) -> list[str]:
        result = self.parse(command)
        if result.malformed:
            raise ValueError(result.reason)
        return list(result.parts)
