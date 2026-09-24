"""Deterministic M1 FAST action executor.

The executor accepts only an explicit action allowlist.  System operations are
provided by an injectable adapter; the default adapter is dry-run so importing
or testing this module never changes the machine.
"""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import os
import re
import subprocess
import webbrowser
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import quote_plus, urlsplit

from .contracts import ActionResult, RouteDecision


MAX_TEXT_LENGTH = 4_096
MAX_URL_LENGTH = 2_048
MAX_SEARCH_LENGTH = 1_024

_ALLOWED_APPS = frozenset({
    "notepad",
    "browser",
    "explorer",
    "settings",
    # Office / document editors — the user's "open Word and type" ask.
    "word",
    "excel",
    "powerpoint",
    "wps",
    # Shells and small utilities.
    "terminal",
    "cmd",
    "calc",
    "paint",
    # Explicit browsers (the generic "browser" token opens the default one).
    "chrome",
    "edge",
})
_ALLOWED_VOLUME_DIRECTIONS = frozenset({"up", "down"})
_ALLOWED_MEDIA_COMMANDS = frozenset({"play_pause", "next", "previous", "stop"})
_ALLOWED_SHORTCUTS: Mapping[str, tuple[str, ...]] = {
    "copy": ("ctrl", "c"),
    "paste": ("ctrl", "v"),
    "cut": ("ctrl", "x"),
    "select_all": ("ctrl", "a"),
    "undo": ("ctrl", "z"),
    "redo": ("ctrl", "y"),
    "save": ("ctrl", "s"),
    "find": ("ctrl", "f"),
    "new_tab": ("ctrl", "t"),
    "close_tab": ("ctrl", "w"),
    "refresh": ("ctrl", "r"),
    "switch_window": ("alt", "tab"),
    "show_desktop": ("win", "d"),
    "escape": ("escape",),
    "enter": ("enter",),
}
_SHORTCUT_KEY_RE = re.compile(r"^[a-z0-9_+\-]{1,64}$", re.IGNORECASE)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_APP_ALIASES = {
    "notepad": "notepad",
    "notepad.exe": "notepad",
    "记事本": "notepad",
    "browser": "browser",
    "浏览器": "browser",
    "explorer": "explorer",
    "explorer.exe": "explorer",
    "file_explorer": "explorer",
    "文件资源管理器": "explorer",
    "资源管理器": "explorer",
    "settings": "settings",
    "设置": "settings",
    # Office / document editors.
    "word": "word",
    "word.exe": "word",
    "winword": "word",
    "winword.exe": "word",
    "microsoft_word": "word",
    "microsoftword": "word",
    "文档": "word",
    "word文档": "word",
    "excel": "excel",
    "excel.exe": "excel",
    "microsoft_excel": "excel",
    "microsoftexcel": "excel",
    "表格": "excel",
    "电子表格": "excel",
    "powerpoint": "powerpoint",
    "powerpoint.exe": "powerpoint",
    "powerpnt": "powerpoint",
    "powerpnt.exe": "powerpoint",
    "microsoft_powerpoint": "powerpoint",
    "演示文稿": "powerpoint",
    "ppt": "powerpoint",
    "幻灯片": "powerpoint",
    "wps": "wps",
    "wps.exe": "wps",
    "wpsoffice": "wps",
    # Shells and small utilities.
    "terminal": "terminal",
    "终端": "terminal",
    "cmd": "cmd",
    "cmd.exe": "cmd",
    "command_prompt": "cmd",
    "命令行": "cmd",
    "命令提示符": "cmd",
    "calc": "calc",
    "calc.exe": "calc",
    "calculator": "calc",
    "计算器": "calc",
    "paint": "paint",
    "paint.exe": "paint",
    "mspaint": "paint",
    "画图": "paint",
    "画笔": "paint",
    # Explicit browsers.
    "chrome": "chrome",
    "chrome.exe": "chrome",
    "google_chrome": "chrome",
    "谷歌浏览器": "chrome",
    "edge": "edge",
    "msedge": "edge",
    "msedge.exe": "edge",
    "microsoft_edge": "edge",
    "微软浏览器": "edge",
}
_MEDIA_ALIASES = {
    "play": "play_pause",
    "pause": "play_pause",
    "play_pause": "play_pause",
    "toggle": "play_pause",
    "播放": "play_pause",
    "暂停": "play_pause",
    "播放暂停": "play_pause",
    "next": "next",
    "next_track": "next",
    "下一首": "next",
    "previous": "previous",
    "prev": "previous",
    "previous_track": "previous",
    "上一首": "previous",
    "stop": "stop",
    "停止": "stop",
}
_SHORTCUT_ALIASES = {
    "全选": "select_all",
    "复制": "copy",
    "粘贴": "paste",
    "剪切": "cut",
    "撤销": "undo",
    "重做": "redo",
    "保存": "save",
    "查找": "find",
    "新标签页": "new_tab",
    "关闭标签页": "close_tab",
    "刷新": "refresh",
    "切换窗口": "switch_window",
    "显示桌面": "show_desktop",
    "回车": "enter",
}


class FastActionAdapter(Protocol):
    """OS boundary for FAST actions; tests inject an in-memory implementation."""

    def open_app(self, app: str) -> None: ...

    def open_url(self, url: str) -> None: ...

    def set_volume(self, direction: str, steps: int) -> None: ...

    def set_mute(self, muted: bool | None) -> None: ...

    def media(self, command: str) -> None: ...

    def shortcut(self, keys: Sequence[str]) -> None: ...

    def screenshot(self) -> None: ...

    def type_unicode(self, text: str) -> None: ...


@dataclass(frozen=True)
class FastAction:
    kind: str
    target: str = ""
    value: Any = None


class DryRunFastAdapter:
    """Record calls without touching the operating system."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def open_app(self, app: str) -> None:
        self.calls.append(("open_app", app))

    def open_url(self, url: str) -> None:
        self.calls.append(("open_url", url))

    def set_volume(self, direction: str, steps: int) -> None:
        self.calls.append(("volume", direction, steps))

    def set_mute(self, muted: bool | None) -> None:
        self.calls.append(("mute", muted))

    def media(self, command: str) -> None:
        self.calls.append(("media", command))

    def shortcut(self, keys: Sequence[str]) -> None:
        self.calls.append(("shortcut", tuple(keys)))

    def screenshot(self) -> None:
        self.calls.append(("screenshot",))

    def type_unicode(self, text: str) -> None:
        self.calls.append(("type_text", text))


# A descriptive alias for integrations that prefer "System" terminology.
DryRunSystemAdapter = DryRunFastAdapter


class WindowsFastAdapter:
    """Windows implementation whose every side-effect boundary is injectable."""

    _APP_COMMANDS = {
        "notepad": ("notepad.exe",),
        "explorer": ("explorer.exe",),
        "settings": ("ms-settings:",),
    }
    _VK = {
        "backspace": 0x08,
        "tab": 0x09,
        "enter": 0x0D,
        "shift": 0x10,
        "ctrl": 0x11,
        "alt": 0x12,
        "pause": 0x13,
        "capslock": 0x14,
        "escape": 0x1B,
        "space": 0x20,
        "pageup": 0x21,
        "pagedown": 0x22,
        "end": 0x23,
        "home": 0x24,
        "left": 0x25,
        "up": 0x26,
        "right": 0x27,
        "down": 0x28,
        "insert": 0x2D,
        "delete": 0x2E,
        "win": 0x5B,
    }
    _MEDIA_VK = {
        "next": 0xB0,
        "previous": 0xB1,
        "stop": 0xB2,
        "play_pause": 0xB3,
    }

    def __init__(
        self,
        *,
        launcher: Callable[[Sequence[str]], object] | None = None,
        url_opener: Callable[[str], object] | None = None,
        send_input: Callable[[Any, int, int], int] | None = None,
        screenshotter: Callable[[], object] | None = None,
        browser_url: str = "https://www.google.com/",
    ) -> None:
        self._launcher = launcher or self._default_launch
        self._url_opener = url_opener or webbrowser.open
        self._send_input = send_input or self._default_send_input
        self._screenshotter = screenshotter or self._default_screenshot
        self.browser_url = browser_url

    @staticmethod
    def _ensure_windows() -> None:
        if os.name != "nt":
            raise OSError("Windows FAST actions are only available on Windows")

    @classmethod
    def _default_launch(cls, command: Sequence[str]) -> object:
        cls._ensure_windows()
        if len(command) == 1 and command[0].endswith(":") and hasattr(os, "startfile"):
            return os.startfile(command[0])  # type: ignore[attr-defined]
        return subprocess.Popen(list(command))

    @classmethod
    def _default_send_input(cls, inputs: Any, count: int, size: int) -> int:
        cls._ensure_windows()
        return int(ctypes.windll.user32.SendInput(count, inputs, size))

    @classmethod
    def _default_screenshot(cls) -> object:
        cls._ensure_windows()
        command = ("explorer.exe", "ms-screenclip:")
        return subprocess.Popen(list(command))

    def open_app(self, app: str) -> None:
        if app == "browser":
            self._url_opener(self.browser_url)
            return
        command = self._APP_COMMANDS.get(app)
        if command is None:
            raise ValueError("application is not allowlisted")
        self._launcher(command)

    def open_url(self, url: str) -> None:
        self._url_opener(url)

    def set_volume(self, direction: str, steps: int) -> None:
        virtual_key = 0xAF if direction == "up" else 0xAE
        for _ in range(steps):
            self._send_virtual_key(virtual_key)

    def set_mute(self, muted: bool | None) -> None:
        # Core Audio state inspection is intentionally outside M1.  ``None`` is
        # a toggle; explicit True/False use the same media key and are named in
        # metadata by the caller rather than claiming state verification.
        self._send_virtual_key(0xAD)

    def media(self, command: str) -> None:
        self._send_virtual_key(self._MEDIA_VK[command])

    def shortcut(self, keys: Sequence[str]) -> None:
        virtual_keys = [self._virtual_key(key) for key in keys]
        self._send_key_sequence(virtual_keys)

    def screenshot(self) -> None:
        self._screenshotter()

    def type_unicode(self, text: str) -> None:
        """Type UTF-16 code units with SendInput KEYEVENTF_UNICODE.

        This does not use ``SendKeys`` and does not touch the clipboard, so no
        clipboard backup/restore path is needed.
        """

        self._ensure_windows()
        if not text:
            return

        ULONG_PTR = wintypes.WPARAM

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        class INPUT_UNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]

        units = text.encode("utf-16-le", errors="strict")
        events: list[INPUT] = []
        for index in range(0, len(units), 2):
            code_unit = units[index] | (units[index + 1] << 8)
            events.append(INPUT(type=1, ki=KEYBDINPUT(0, code_unit, 0x0004, 0, 0)))
            events.append(INPUT(type=1, ki=KEYBDINPUT(0, code_unit, 0x0004 | 0x0002, 0, 0)))
        array = (INPUT * len(events))(*events)
        sent = self._send_input(array, len(events), ctypes.sizeof(INPUT))
        if sent != len(events):
            raise OSError(f"SendInput sent {sent} of {len(events)} Unicode key events")

    def _virtual_key(self, key: str) -> int:
        normalized = key.lower()
        if normalized in self._VK:
            return self._VK[normalized]
        if len(normalized) == 1 and normalized.isascii() and normalized.isalnum():
            return ord(normalized.upper())
        raise ValueError("unsupported shortcut key")

    def _send_key_sequence(self, virtual_keys: Sequence[int]) -> None:
        for key in virtual_keys:
            self._send_virtual_key(key, key_up=False)
        for key in reversed(virtual_keys):
            self._send_virtual_key(key, key_up=True)

    def _send_virtual_key(self, virtual_key: int, *, key_up: bool | None = None) -> None:
        self._ensure_windows()
        ULONG_PTR = wintypes.WPARAM

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        class INPUT_UNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]

        flags = (0,) if key_up is False else ((0x0002,) if key_up is True else (0, 0x0002))
        events = [INPUT(type=1, ki=KEYBDINPUT(virtual_key, 0, flag, 0, 0)) for flag in flags]
        array = (INPUT * len(events))(*events)
        sent = self._send_input(array, len(events), ctypes.sizeof(INPUT))
        if sent != len(events):
            raise OSError(f"SendInput sent {sent} of {len(events)} key events")


# Compatibility alias used by integrations that name the concrete boundary by OS.
WindowsSystemAdapter = WindowsFastAdapter


def _coerce_mapping(raw: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return raw if isinstance(raw, Mapping) else {}


def _first(raw: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in raw:
            return raw[name]
    return None


def _clean_token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _clean_text(value: Any, *, maximum: int, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    if not value:
        raise ValueError(f"{field} is empty")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    if _CONTROL_CHAR_RE.search(value):
        raise ValueError(f"{field} contains control characters")
    return value


def _validate_url(value: Any) -> str:
    url = _clean_text(value, maximum=MAX_URL_LENGTH, field="url").strip()
    if any(character.isspace() for character in url):
        raise ValueError("url contains whitespace")
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("only http and https URLs are allowed")
    if not parts.netloc or parts.username is not None or parts.password is not None:
        raise ValueError("url host is invalid or contains credentials")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("url port is invalid") from exc
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("url port is invalid")
    try:
        host = parts.hostname.encode("idna").decode("ascii") if parts.hostname else ""
    except UnicodeError as exc:
        raise ValueError("url host is invalid") from exc
    if not host or host.lower() == "localhost" or "." not in host:
        raise ValueError("url host is not an authorized public hostname")
    return url


def _extract_text(raw: Mapping[str, Any], target: str, names: Sequence[str]) -> Any:
    selected = _first(raw, names)
    if selected is not None:
        return selected
    span = raw.get("span")
    if isinstance(span, Mapping):
        selected = _first(span, ("text", "value", "selected_text"))
        if selected is not None:
            return selected
    return target


def _normalize_shortcut(value: Any) -> tuple[str, tuple[str, ...]]:
    if isinstance(value, str):
        name = _clean_token(value)
        name = _SHORTCUT_ALIASES.get(value.strip(), name)
        if name in _ALLOWED_SHORTCUTS:
            return name, _ALLOWED_SHORTCUTS[name]
        # Explicit key sequences are accepted only if they exactly match an
        # allowlisted sequence, not because each individual key looks benign.
        pieces = tuple(_clean_token(item) for item in value.split("+"))
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        pieces = tuple(_clean_token(item) for item in value)
    else:
        raise ValueError("shortcut must be an allowlisted name or key sequence")
    if not pieces or any(not _SHORTCUT_KEY_RE.fullmatch(item) for item in pieces):
        raise ValueError("shortcut contains invalid keys")
    for name, allowed in _ALLOWED_SHORTCUTS.items():
        if pieces == allowed:
            return name, allowed
    raise ValueError("shortcut is not allowlisted")


def _parse_steps(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("volume steps must be an integer")
    try:
        steps = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("volume steps must be an integer") from exc
    if not 1 <= steps <= 20:
        raise ValueError("volume steps must be between 1 and 20")
    return steps


class FastActionExecutor:
    """Execute deterministic, non-destructive actions through a strict adapter."""

    def __init__(
        self,
        adapter: FastActionAdapter | None = None,
        *,
        search_url_template: str = "https://www.google.com/search?q={query}",
        allowed_search_templates: Sequence[str] | None = None,
    ) -> None:
        self.adapter = adapter or DryRunFastAdapter()
        self.search_url_template = search_url_template
        templates = tuple(allowed_search_templates or (search_url_template,))
        if search_url_template not in templates:
            raise ValueError("search template is not allowlisted")
        if search_url_template.count("{query}") != 1:
            raise ValueError("search template must contain exactly one {query} placeholder")
        probe = search_url_template.replace("{query}", "safe")
        _validate_url(probe)

    def execute(self, decision: RouteDecision | Mapping[str, Any] | FastAction) -> ActionResult:
        try:
            action = self._normalize(decision)
            self._dispatch(action)
            return ActionResult(
                True,
                self._action_name(action),
                "FAST action completed",
                {"kind": action.kind, "target": action.target},
            )
        except (TypeError, ValueError, PermissionError) as exc:
            kind, target = self._identity(decision)
            return ActionResult(
                False,
                self._safe_action_name(kind, target),
                f"rejected: {exc}",
                {"code": "fast_action_rejected"},
            )
        except Exception as exc:
            kind, target = self._identity(decision)
            return ActionResult(
                False,
                self._safe_action_name(kind, target),
                f"{type(exc).__name__}: {exc}",
                {"code": "fast_action_failed"},
            )

    def execute_action(self, kind: str, target: str = "", **parameters: Any) -> ActionResult:
        """Convenience entry point for callers that already have an action kind."""

        payload = {"accepted": True, "kind": kind, "target": target, **parameters}
        return self.execute(payload)

    def _normalize(self, decision: RouteDecision | Mapping[str, Any] | FastAction) -> FastAction:
        if isinstance(decision, FastAction):
            return self._normalize_fields(decision.kind, decision.target, decision.value, {})
        if isinstance(decision, RouteDecision):
            if not decision.accepted:
                raise PermissionError("route decision is not accepted")
            if decision.destructive:
                raise PermissionError("destructive route is blocked")
            if not decision.complete:
                raise PermissionError("incomplete route is blocked")
            raw = _coerce_mapping(decision.raw)
            return self._normalize_fields(decision.kind, decision.target, None, raw)
        if isinstance(decision, Mapping):
            if decision.get("accepted", True) is not True:
                raise PermissionError("route decision is not accepted")
            if bool(decision.get("destructive", False)):
                raise PermissionError("destructive route is blocked")
            if decision.get("complete", True) is not True:
                raise PermissionError("incomplete route is blocked")
            kind = str(decision.get("kind") or decision.get("action") or "")
            target = str(decision.get("target") or "")
            return self._normalize_fields(kind, target, decision.get("value"), decision)
        raise TypeError("unsupported FAST action decision")

    def _normalize_fields(
        self,
        kind_value: str,
        target_value: str,
        direct_value: Any,
        raw: Mapping[str, Any],
    ) -> FastAction:
        kind = _clean_token(kind_value)
        target = str(target_value or "").strip()
        if kind == "open_app":
            app = _APP_ALIASES.get(target.lower(), _APP_ALIASES.get(target))
            if app not in _ALLOWED_APPS:
                raise ValueError("application target is not allowlisted")
            return FastAction(kind, app)

        if kind == "open_url":
            value = direct_value if direct_value is not None else _extract_text(raw, target, ("url", "text"))
            return FastAction(kind, _validate_url(value))

        if kind == "search":
            value = direct_value if direct_value is not None else _extract_text(
                raw, target, ("query", "text", "search_text")
            )
            query = _clean_text(value, maximum=MAX_SEARCH_LENGTH, field="search query")
            if not query.strip():
                raise ValueError("search query is empty")
            return FastAction(kind, "web", query)

        if kind in {"volume", "volume_up", "volume_down"}:
            if kind == "volume_up":
                direction = "up"
            elif kind == "volume_down":
                direction = "down"
            else:
                direction = _clean_token(_first(raw, ("direction", "operation", "mode")) or target)
            if direction not in _ALLOWED_VOLUME_DIRECTIONS:
                raise ValueError("volume direction is not allowlisted")
            steps_source = direct_value if direct_value is not None else _first(
                raw, ("steps", "amount", "value")
            )
            steps = _parse_steps(steps_source if steps_source is not None else 1)
            return FastAction("volume", direction, steps)

        if kind in {"mute", "unmute", "toggle_mute"}:
            if kind == "mute":
                muted: bool | None = True
            elif kind == "unmute":
                muted = False
            else:
                muted = None
            operation = _clean_token(_first(raw, ("operation", "mode", "state")) or target)
            if operation in {"toggle", "toggle_mute", "切换"}:
                muted = None
            elif operation in {"mute", "on", "true", "1", "静音"}:
                muted = True
            elif operation in {"unmute", "off", "false", "0", "取消静音"}:
                muted = False
            elif operation == "":
                pass
            else:
                raise ValueError("mute operation is not allowlisted")
            return FastAction("mute", "toggle" if muted is None else ("on" if muted else "off"), muted)

        if kind in {"media", "media_control"}:
            operation = _clean_token(_first(raw, ("command", "operation", "mode")) or target)
            operation = _MEDIA_ALIASES.get(target, _MEDIA_ALIASES.get(operation, operation))
            if operation not in _ALLOWED_MEDIA_COMMANDS:
                raise ValueError("media command is not allowlisted")
            return FastAction("media", operation)

        if kind == "shortcut":
            shortcut_source = direct_value if direct_value is not None else _first(
                raw, ("shortcut", "keys", "name")
            )
            name, keys = _normalize_shortcut(shortcut_source if shortcut_source is not None else target)
            return FastAction(kind, name, keys)

        if kind == "screenshot":
            if target and _clean_token(target) not in {"screen", "full_screen", "fullscreen", "屏幕", "全屏"}:
                raise ValueError("screenshot target is not allowlisted")
            return FastAction(kind, "screen")

        if kind in {"type", "type_text", "input_text"}:
            value = direct_value if direct_value is not None else _extract_text(
                raw, target, ("text", "input", "value", "selected_text")
            )
            text = _clean_text(value, maximum=MAX_TEXT_LENGTH, field="text")
            return FastAction("type_text", "active_window", text)

        raise ValueError("action kind is not allowlisted")

    def _dispatch(self, action: FastAction) -> None:
        if action.kind == "open_app":
            self.adapter.open_app(action.target)
        elif action.kind == "open_url":
            self.adapter.open_url(action.target)
        elif action.kind == "search":
            self.adapter.open_url(self.search_url_template.format(query=quote_plus(action.value)))
        elif action.kind == "volume":
            self.adapter.set_volume(action.target, int(action.value))
        elif action.kind == "mute":
            self.adapter.set_mute(action.value)
        elif action.kind == "media":
            self.adapter.media(action.target)
        elif action.kind == "shortcut":
            self.adapter.shortcut(action.value)
        elif action.kind == "screenshot":
            self.adapter.screenshot()
        elif action.kind == "type_text":
            self.adapter.type_unicode(action.value)
        else:  # defensive: normalization owns the allowlist
            raise ValueError("action kind is not allowlisted")

    @staticmethod
    def _action_name(action: FastAction) -> str:
        if action.kind in {"type_text", "search", "open_url"}:
            return action.kind
        return f"{action.kind}:{action.target}" if action.target else action.kind

    @staticmethod
    def _safe_action_name(kind: str, target: str) -> str:
        safe_kind = _clean_token(kind) or "none"
        safe_target = _clean_token(target)
        return f"{safe_kind}:{safe_target}" if safe_target else safe_kind

    @staticmethod
    def _identity(decision: RouteDecision | Mapping[str, Any] | FastAction) -> tuple[str, str]:
        if isinstance(decision, FastAction):
            return decision.kind, decision.target
        if isinstance(decision, RouteDecision):
            return decision.kind, decision.target
        if isinstance(decision, Mapping):
            return str(decision.get("kind") or decision.get("action") or "none"), str(
                decision.get("target") or ""
            )
        return "none", ""


# Short alias for integrations that use the M1 phase name directly.
FastExecutor = FastActionExecutor
