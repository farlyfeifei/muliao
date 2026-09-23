"""Jev M0 白名单路由：只允许打开记事本。"""
from __future__ import annotations

import json
import threading
from typing import Any

import httpx

from .contracts import RouteDecision


M0_QUESTIONS = {
    "addressed": {
        "type": "noul",
        "instructions": "Is `utterance` a direct instruction to this computer voice assistant?",
    },
    "kind": {
        "type": "choice",
        "instructions": "Which single action kind does `utterance` request? The transcript may contain Chinese ASR errors.",
        "criteria": {
            "open_app": "Open or launch an installed application",
            "none": "Not an application launch command",
        },
    },
    "app": {
        "type": "choice",
        "instructions": "Which application should be opened for `utterance`?",
        "criteria": {
            "notepad": "Windows Notepad / 记事本",
            "none": "No supported application",
        },
    },
    "complete": {
        "type": "noul",
        "instructions": "Is `utterance` a complete command with both an action and its target?",
    },
    "destructive": {
        "type": "noul",
        "instructions": "Would carrying out `utterance` destroy data, send content, spend money, or be hard to undo?",
    },
}


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _answer_probability(answer: Any, key: str) -> float:
    if not isinstance(answer, dict):
        return 0.0
    return _safe_float(answer.get(key, 0.0))


class JevM0Router:
    """持久连接 Jev 路由器，严格收敛到 ``open_app:notepad``。"""

    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        model: str = "jev-latest",
        timeout: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout, trust_env=False)
        self._owns_client = client is None
        self._lock = threading.Lock()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def route(self, command: str) -> RouteDecision:
        if not self.api_key:
            return RouteDecision(accepted=False, reason="jev_missing_api_key")
        body = {
            "state": json.dumps({"utterance": command}, ensure_ascii=False),
            "model": self.model,
            "questions": M0_QUESTIONS,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            with self._lock:
                response = self._client.post(self.url, json=body, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            return RouteDecision(accepted=False, reason=f"jev_error:{type(exc).__name__}")

        answers = payload.get("answers") if isinstance(payload, dict) else {}
        answers = answers if isinstance(answers, dict) else {}
        addressed = _answer_probability(answers.get("addressed"), "noul")
        complete = _answer_probability(answers.get("complete"), "noul")
        destructive = _answer_probability(answers.get("destructive"), "noul")
        kind_answer = answers.get("kind") if isinstance(answers.get("kind"), dict) else {}
        app_answer = answers.get("app") if isinstance(answers.get("app"), dict) else {}
        kind = str(kind_answer.get("choice") or "none")
        app = str(app_answer.get("choice") or "none")
        confidence = min(
            _safe_float(kind_answer.get("confidence")),
            _safe_float(app_answer.get("confidence")),
        )
        allowed = (
            addressed >= 0.5
            and complete >= 0.6
            and destructive <= 0.5
            and kind == "open_app"
            and app == "notepad"
            and confidence >= 0.5
        )
        reason = "" if allowed else "M0 only allows a confident, complete, non-destructive notepad launch"
        return RouteDecision(
            accepted=allowed,
            kind=kind,
            target=app,
            confidence=confidence,
            destructive=destructive > 0.5,
            complete=complete >= 0.6,
            reason=reason,
            raw={"answers": answers, "model": payload.get("model") if isinstance(payload, dict) else None},
        )
