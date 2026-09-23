"""保守的复合命令拆分器。"""
from __future__ import annotations

import re


_SPLIT_RE = re.compile(
    r"(?:\s*(?:然后|接着|随后|并且)\s*|\s+(?:and\s+then|then)\s+|"
    r"\s*再(?=(?:打开|搜索|输入|关闭|调|截|切换|按|播放|暂停|启动|访问))\s*)",
    re.IGNORECASE,
)


def _mask_quoted(text: str) -> str:
    chars = list(text)
    quote_end: str | None = None
    pairs = {'"': '"', "'": "'", "“": "”", "‘": "’"}
    for index, char in enumerate(chars):
        if quote_end is not None:
            if char == quote_end:
                quote_end = None
            else:
                chars[index] = "\0"
            continue
        if char in pairs:
            quote_end = pairs[char]
    return "".join(chars)


class CommandSplitter:
    def split(self, command: str) -> list[str]:
        text = str(command or "").strip()
        if not text:
            return []
        masked = _mask_quoted(text)
        spans = [match.span() for match in _SPLIT_RE.finditer(masked)]
        if not spans:
            return [text]
        parts: list[str] = []
        start = 0
        for left, right in spans:
            part = text[start:left].strip(" \t,，。;；")
            if part:
                parts.append(part)
            start = right
        tail = text[start:].strip(" \t,，。;；")
        if tail:
            parts.append(tail)
        return parts or [text]
