"""Windows M0 动作执行器。"""
from __future__ import annotations

import os
import subprocess
from typing import Callable

from .contracts import ActionResult, RouteDecision


class DryRunExecutor:
    """返回将执行的动作，不产生系统副作用。"""

    def execute(self, decision: RouteDecision) -> ActionResult:
        if decision.destructive:
            return ActionResult(False, "none", "destructive route blocked")
        if not decision.accepted or decision.kind != "open_app" or decision.target != "notepad":
            return ActionResult(False, "none", "decision is outside the M0 allowlist")
        return ActionResult(True, "open_app:notepad", "dry-run: would launch notepad.exe")


class WindowsNotepadExecutor:
    def __init__(self, launcher: Callable[[str], object] | None = None) -> None:
        self._launcher = launcher or self._default_launch

    @staticmethod
    def _default_launch(executable: str) -> object:
        if os.name != "nt":
            raise OSError("notepad action is only available on Windows")
        if hasattr(os, "startfile"):
            return os.startfile(executable)  # type: ignore[attr-defined]
        return subprocess.Popen([executable])

    def execute(self, decision: RouteDecision) -> ActionResult:
        if decision.destructive:
            return ActionResult(False, "open_app:notepad", "destructive route blocked")
        if not decision.accepted or decision.kind != "open_app" or decision.target != "notepad":
            return ActionResult(False, "none", "decision is outside the M0 allowlist")
        try:
            self._launcher("notepad.exe")
            return ActionResult(True, "open_app:notepad", "notepad launch requested")
        except Exception as exc:
            return ActionResult(False, "open_app:notepad", f"{type(exc).__name__}: {exc}")
