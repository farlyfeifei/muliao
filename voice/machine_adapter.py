r"""Real-desktop FAST adapter backed by the mainline :mod:`machine_control` layer.

The voice FAST channel historically drove Windows through :class:`WindowsFastAdapter`
(ctypes ``SendInput``). That path can type and send keys, but it cannot locate a
window/control, and it never reused the mainline pywinauto control layer that the
rest of 幕僚 already trusts (``machine_control.py``).

:class:`MachineControlFastAdapter` closes that gap. It implements the same
:class:`~voice.fast_actions.FastActionAdapter` protocol, so it drops into
:class:`~voice.fast_actions.FastActionExecutor` unchanged, but routes the actions
``machine_control`` does best — launching an app, typing text, pressing keys —
through pywinauto/UIA. The remaining actions (volume, mute, media, screenshot)
have no ``machine_control`` equivalent, so they are inherited verbatim from
:class:`WindowsFastAdapter` and still use ``SendInput``.

Design rules (mirror ``machine_control``):
  1) ``machine_control`` returns a dict ``{"ok": bool, ...}`` and never raises for
     ordinary failures. This adapter translates ``ok=False`` into an exception so
     the FAST executor reports an honest ``ActionResult(ok=False)``.
  2) When ``machine_control`` reports ``control_unavailable`` (non-Windows, or
     pywinauto/win32 missing), the adapter degrades to the inherited ``SendInput``
     / subprocess path rather than failing, so a machine without pywinauto still
     works for the actions that do not need control location.
  3) The control module and every side-effect boundary are injectable, so tests
     run offline with a fake control object and never touch the desktop.
"""
from __future__ import annotations

from typing import Any, Callable, Sequence

from .fast_actions import WindowsFastAdapter


class MachineControlError(RuntimeError):
    """Raised when ``machine_control`` reports a structured failure.

    Carries the short ``error`` code (``target_not_found`` / ``action_failed`` /
    ``invalid_args`` / ``control_unavailable``) plus the model-facing ``hint`` so
    the FAST executor surfaces a truthful reason instead of a bare success.
    """

    def __init__(self, code: str, hint: str = "", **extra: Any) -> None:
        self.code = code
        self.hint = hint
        self.extra = extra
        detail = hint or code
        super().__init__(f"{code}: {detail}" if hint else code)


# Logical FAST app token -> the executable name ``machine_control.open_application``
# should launch. pywinauto's ``Application.start`` resolves these through the
# normal Windows app-path lookup (PATH + App Paths registry), the same way typing
# the name into Win+R does, so short names like ``winword.exe`` work when Office
# is installed. ``browser`` and ``settings`` are NOT here: they are URI/default-
# handler launches that ``machine_control`` cannot express, and stay on the
# inherited WindowsFastAdapter path.
_APP_EXECUTABLES = {
    "notepad": "notepad.exe",
    "explorer": "explorer.exe",
    "word": "winword.exe",
    "excel": "excel.exe",
    "powerpoint": "powerpnt.exe",
    "wps": "wps.exe",
    "terminal": "wt.exe",
    "cmd": "cmd.exe",
    "calc": "calc.exe",
    "paint": "mspaint.exe",
    "chrome": "chrome.exe",
    "edge": "msedge.exe",
}


def _default_control() -> Any:
    """Import the mainline control layer lazily so a missing module degrades cleanly."""

    import machine_control

    return machine_control


class MachineControlFastAdapter(WindowsFastAdapter):
    """FAST adapter that drives the desktop through ``machine_control`` (pywinauto).

    ``open_app`` / ``type_unicode`` / ``shortcut`` go through pywinauto when it is
    available and fall back to the inherited ``SendInput``/subprocess path when the
    control layer reports ``control_unavailable``. ``set_volume`` / ``set_mute`` /
    ``media`` / ``screenshot`` are inherited unchanged.
    """

    def __init__(
        self,
        *,
        control: Any | None = None,
        launcher: Callable[[Sequence[str]], object] | None = None,
        url_opener: Callable[[str], object] | None = None,
        send_input: Callable[[Any, int, int], int] | None = None,
        screenshotter: Callable[[], object] | None = None,
        browser_url: str = "https://www.google.com/",
    ) -> None:
        super().__init__(
            launcher=launcher,
            url_opener=url_opener,
            send_input=send_input,
            screenshotter=screenshotter,
            browser_url=browser_url,
        )
        self._control = control if control is not None else _default_control()

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _check(result: Any, action: str) -> None:
        """Raise :class:`MachineControlError` for a structured ``ok=False`` result.

        Returns silently on success. Callers check ``_is_unavailable`` first so a
        missing pywinauto degrades to ``SendInput`` rather than raising here.
        """

        if isinstance(result, dict) and result.get("ok"):
            return
        code = str((result or {}).get("error") or "action_failed") if isinstance(result, dict) else "action_failed"
        hint = str((result or {}).get("hint") or "") if isinstance(result, dict) else ""
        raise MachineControlError(code, hint or f"{action} failed")

    def _is_unavailable(self, result: Any) -> bool:
        return isinstance(result, dict) and result.get("error") == "control_unavailable"

    # -- FastActionAdapter protocol --------------------------------------

    def open_app(self, app: str) -> None:
        # ``browser`` (default handler) and ``settings`` (ms-settings: URI) cannot
        # be expressed as a pywinauto start(); keep the inherited launch path.
        if app in {"browser", "settings"}:
            super().open_app(app)
            return
        executable = _APP_EXECUTABLES.get(app)
        if executable is None:
            raise ValueError(f"application is not supported: {app}")
        result = self._control.open_application(executable)
        if isinstance(result, dict) and result.get("ok"):
            return
        if self._is_unavailable(result):
            # No pywinauto/win32: degrade to the injectable subprocess launcher.
            self._launcher((executable,))
            return
        self._check(result, f"open_application({executable})")

    def type_unicode(self, text: str) -> None:
        if not text:
            return
        result = self._control.type_text(text)
        if isinstance(result, dict) and result.get("ok"):
            return
        if self._is_unavailable(result):
            super().type_unicode(text)
            return
        self._check(result, "type_text")

    def shortcut(self, keys: Sequence[str]) -> None:
        combo = "+".join(str(key) for key in keys)
        if not combo.strip():
            raise ValueError("shortcut is empty")
        result = self._control.press_keys(combo)
        if isinstance(result, dict) and result.get("ok"):
            return
        if self._is_unavailable(result):
            super().shortcut(keys)
            return
        self._check(result, f"press_keys({combo})")

    def open_url(self, url: str) -> None:
        # Opening a URL is a default-browser handoff; the inherited injectable
        # url_opener (webbrowser.open) already does exactly this.
        super().open_url(url)


# GOAL actions that write a value into the focused/located control. They need the
# verbatim ``text`` the chooser resolved from a code-selected span.
_GOAL_TEXT_ACTIONS = frozenset({"type", "type_text", "input", "input_text", "fill", "set_value"})
# GOAL actions that press a single named key rather than click a control.
_GOAL_KEY_ACTIONS = {
    "enter": "enter",
    "return": "enter",
    "esc": "esc",
    "escape": "esc",
}


class MachineControlActionExecutor:
    """A real :class:`~voice.goal.GoalActionExecutor` backed by ``machine_control``.

    The GOAL loop's default executor is :class:`~voice.goal.DryRunActionExecutor`,
    which only records intent. This executor performs the chosen step on the real
    desktop through the mainline pywinauto control layer:

    - ``click`` / ``activate`` → ``machine_control.click_element`` (locates the
      control by ``automation_id`` when perception captured one, else by name);
    - ``type``/``type_text``/``input``/``fill``/``set_value`` → ``machine_control.type_text``
      with the code-selected verbatim ``text``;
    - ``enter`` / ``esc`` → ``machine_control.press_keys``.

    ``machine_control`` returns ``{"ok": bool, ...}`` and never raises for ordinary
    failures, so this executor translates that into the ``{"ok", "changed", "detail"}``
    mapping the GOAL loop expects. On a real, successful side effect it declares
    ``changed=True`` (perception cannot always observe an edit control's new value,
    so a snapshot diff alone would wrongly report a stall). Any failure — including
    ``control_unavailable`` on a machine without pywinauto — returns ``ok=False`` so
    the loop reports an honest ``action_failed`` step rather than pretending it ran.
    """

    def __init__(self, *, control: Any | None = None, button: str = "left") -> None:
        self._control = control if control is not None else _default_control()
        self._button = button

    @staticmethod
    def _element_identity(element: Any) -> tuple[str | None, str | None]:
        """Return ``(automation_id, element_name)`` for control location."""

        metadata = getattr(element, "metadata", None) or {}
        automation_id = None
        if isinstance(metadata, dict):
            raw = metadata.get("automation_id")
            if isinstance(raw, str) and raw.strip():
                automation_id = raw.strip()
        name = getattr(element, "text", "") or ""
        name = name.strip() or None
        return automation_id, name

    def execute(self, element: Any, action: str = "activate", text: str = "") -> dict:
        normalized = str(action or "activate").strip().lower().replace("_", "")
        automation_id, name = self._element_identity(element)

        if normalized in {a.replace("_", "") for a in _GOAL_TEXT_ACTIONS}:
            if not text:
                return {"ok": False, "changed": False, "detail": "type action has no text"}
            result = self._control.type_text(text, element_name=name)
            return self._result(result, f"type_text {name or automation_id or 'focus'}")

        if normalized in _GOAL_KEY_ACTIONS:
            key = _GOAL_KEY_ACTIONS[normalized]
            result = self._control.press_keys(key)
            return self._result(result, f"press_keys {key}")

        # Default: click / activate a located control.
        if automation_id is None and name is None:
            return {"ok": False, "changed": False, "detail": "no element identity to act on"}
        result = self._control.click_element(
            element_name=name, automation_id=automation_id, button=self._button
        )
        return self._result(result, f"click {name or automation_id}")

    def _result(self, result: Any, action: str) -> dict:
        if isinstance(result, dict) and result.get("ok"):
            detail = str(result.get("detail") or "")
            return {"ok": True, "changed": True, "detail": detail or f"executed {action}"}
        code = str((result or {}).get("error") or "action_failed") if isinstance(result, dict) else "action_failed"
        hint = str((result or {}).get("hint") or "") if isinstance(result, dict) else ""
        return {"ok": False, "changed": False, "detail": f"{code}: {hint}".strip(": ") or action}


__all__ = [
    "MachineControlActionExecutor",
    "MachineControlError",
    "MachineControlFastAdapter",
]
