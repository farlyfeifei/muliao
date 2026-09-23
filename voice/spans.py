"""Deterministic free-text span selection for FAST voice actions.

The helpers in this module only select character ranges that already exist in the
transcript.  They never summarize, translate, normalize, or otherwise generate
replacement text.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


_QUOTE_PAIRS = {"\"": "\"", "'": "'", "“": "”", "‘": "’"}

# A boundary is only accepted when the connector is followed by another command
# starter.  This keeps natural phrases such as ``搜索先做 A 然后做 B`` intact.
_ACTION_START = (
    r"(?:打开|访问|启动|搜索|搜|查找|输入|键入|打字|关闭|调高|调低|增大|降低|"
    r"音量|静音|取消静音|播放|暂停|下一首|上一首|截图|截屏|按下|按|切换|"
    r"open\b|visit\b|launch\b|search\b|find\b|type\b|enter\b|close\b|"
    r"volume\b|mute\b|unmute\b|play\b|pause\b|next\b|previous\b|press\b|"
    r"take\s+(?:a\s+)?screenshot\b|screenshot\b)"
)
_COMPOUND_BOUNDARY_RE = re.compile(
    rf"(?:"
    rf"[ \t]*(?:[,，;；。][ \t]*)?(?:"
    rf"(?:然后|接着|随后|并且|以及|并|再)[ \t]*(?:再[ \t]*)?(?={_ACTION_START})|"
    rf"(?:and[ \t]+then|then)[ \t]+(?={_ACTION_START})"
    rf")|"
    rf"[ \t]*[,，;；。][ \t]*(?={_ACTION_START})"
    rf")",
    re.IGNORECASE,
)

_SEARCH_PREFIX_RE = re.compile(
    r"(?:搜索(?:一下|下)?|搜(?:一下|下)?|查找|\bsearch(?:\s+for)?\b|\bfind\b)"
    r"\s*(?:[:：]\s*)?",
    re.IGNORECASE,
)
_TYPE_PREFIX_RE = re.compile(
    r"(?:输入|键入|打字(?:输入)?|\btype\b|\benter\b)\s*(?:[:：]\s*)?",
    re.IGNORECASE,
)
_OPEN_PREFIX_RE = re.compile(
    r"(?:打开|访问|\bopen\b|\bvisit\b|\bgo\s+to\b)\s*(?:[:：]\s*)?",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>\"'“”‘’，。；！]+", re.IGNORECASE)

_INTENT_ALIASES = {
    "search": "search",
    "web_search": "search",
    "type": "type_text",
    "type_text": "type_text",
    "input": "type_text",
    "input_text": "type_text",
    "open_url": "open_url",
    "url": "open_url",
    "quote": "quoted",
    "quoted": "quoted",
}


@dataclass(frozen=True)
class SelectedSpan:
    """A verbatim half-open character range selected from a command."""

    kind: str
    text: str
    start: int
    end: int
    quoted: bool = False

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError("invalid span offsets")

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "quoted": self.quoted,
        }


# Descriptive aliases for callers that use a shorter name.
TextSpan = SelectedSpan
Span = SelectedSpan


def _paired_quotes(text: str, start: int, end: int) -> Iterable[tuple[int, int]]:
    """Yield complete quote pairs, ignoring apostrophes without a closing mate."""

    index = max(0, start)
    end = min(len(text), end)
    while index < end:
        opener = text[index]
        closer = _QUOTE_PAIRS.get(opener)
        if closer is None:
            index += 1
            continue
        close_at = text.find(closer, index + 1, end)
        if close_at < 0:
            index += 1
            continue
        yield index, close_at
        index = close_at + 1


def _mask_complete_quotes(text: str) -> str:
    chars = list(text)
    for left, right in _paired_quotes(text, 0, len(text)):
        for index in range(left, right + 1):
            chars[index] = "\0"
    return "".join(chars)


def _compound_end(text: str, start: int) -> int:
    masked = _mask_complete_quotes(text)
    match = _COMPOUND_BOUNDARY_RE.search(masked, start)
    return match.start() if match is not None else len(text)


def _quoted_after(text: str, start: int, end: int, kind: str) -> SelectedSpan | None:
    for left, right in _paired_quotes(text, start, end):
        inner_start = left + 1
        return SelectedSpan(
            kind=kind,
            text=text[inner_start:right],
            start=inner_start,
            end=right,
            quoted=True,
        )
    return None


def _trim_unquoted(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    # Sentence punctuation is syntax rather than selected free text.  Question
    # marks are deliberately retained because they are often part of a query.
    while end > start and text[end - 1] in ",，。;；":
        end -= 1
        while end > start and text[end - 1].isspace():
            end -= 1
    return start, end


def _from_prefix(text: str, match: re.Match[str], kind: str) -> SelectedSpan | None:
    value_start = match.end()
    value_end = _compound_end(text, value_start)
    quoted = _quoted_after(text, value_start, value_end, kind)
    if quoted is not None:
        return quoted
    value_start, value_end = _trim_unquoted(text, value_start, value_end)
    if value_start >= value_end:
        return None
    return SelectedSpan(kind, text[value_start:value_end], value_start, value_end)


def _url_after(text: str, start: int) -> SelectedSpan | None:
    boundary = _compound_end(text, start)
    match = _URL_RE.search(text, start, boundary)
    if match is None:
        return None
    url_start, url_end = match.span()
    # These ASCII characters commonly terminate a spoken sentence but are not
    # excluded by the URL regex because some are legal inside URLs.
    while url_end > url_start and text[url_end - 1] in ".,;!":
        url_end -= 1
    if url_start >= url_end:
        return None
    return SelectedSpan("open_url", text[url_start:url_end], url_start, url_end)


def select_text_span(command: str, intent: str | None = None) -> SelectedSpan | None:
    """Select one free-text span from ``command`` without rewriting it.

    Supported forms include Chinese/English quotes, ``搜索 X`` / ``search for
    X``, ``输入 X`` / ``type X``, and ``打开 https://...`` / ``open
    https://...``.  If ``intent`` is supplied, only that action is considered.
    """

    text = str(command or "")
    if not text:
        return None
    normalized_intent = _INTENT_ALIASES.get(str(intent).strip().lower()) if intent else None
    if intent is not None and normalized_intent is None:
        return None

    if normalized_intent == "quoted":
        return _quoted_after(text, 0, len(text), "quoted")

    if normalized_intent == "search":
        match = _SEARCH_PREFIX_RE.search(text)
        return _from_prefix(text, match, "search") if match is not None else None

    if normalized_intent == "type_text":
        match = _TYPE_PREFIX_RE.search(text)
        return _from_prefix(text, match, "type_text") if match is not None else None

    if normalized_intent == "open_url":
        match = _OPEN_PREFIX_RE.search(text)
        start = match.end() if match is not None else 0
        return _url_after(text, start)

    candidates: list[tuple[int, str, re.Match[str]]] = []
    for kind, pattern in (
        ("search", _SEARCH_PREFIX_RE),
        ("type_text", _TYPE_PREFIX_RE),
        ("open_url", _OPEN_PREFIX_RE),
    ):
        match = pattern.search(text)
        if match is not None:
            candidates.append((match.start(), kind, match))

    if candidates:
        _, kind, match = min(candidates, key=lambda item: item[0])
        if kind == "open_url":
            span = _url_after(text, match.end())
            if span is not None:
                return span
        else:
            return _from_prefix(text, match, kind)

    # A bare URL or quote is still a select-not-generate source.
    url = _url_after(text, 0)
    if url is not None:
        return url
    return _quoted_after(text, 0, len(text), "quoted")


def extract_free_text_span(command: str, intent: str | None = None) -> SelectedSpan | None:
    """Compatibility name for :func:`select_text_span`."""

    return select_text_span(command, intent)


def extract_span(command: str, intent: str | None = None) -> SelectedSpan | None:
    """Short compatibility name for :func:`select_text_span`."""

    return select_text_span(command, intent)


def extract_free_text(command: str, intent: str | None = None) -> str | None:
    """Return only the selected verbatim text, or ``None``."""

    span = select_text_span(command, intent)
    return span.text if span is not None else None


class SpanExtractor:
    """Small injectable wrapper used by routers and command pipelines."""

    def extract(self, command: str, intent: str | None = None) -> SelectedSpan | None:
        return select_text_span(command, intent)
