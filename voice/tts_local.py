"""Windows SAPI 本地播报。"""
from __future__ import annotations

from typing import Callable, Any


class NullSpeaker:
    def speak(self, text: str) -> None:
        return None


class SapiSpeaker:
    def __init__(self, dispatch: Callable[[str], Any] | None = None) -> None:
        self._dispatch = dispatch
        self._voice = None

    def _get_voice(self):
        if self._voice is not None:
            return self._voice
        if self._dispatch is None:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            dispatch = win32com.client.Dispatch
        else:
            dispatch = self._dispatch
        self._voice = dispatch("SAPI.SpVoice")
        return self._voice

    def speak(self, text: str) -> None:
        self._get_voice().Speak(str(text))
