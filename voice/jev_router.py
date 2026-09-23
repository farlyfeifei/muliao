"""Jev 语音路由：M0 记事本白名单与 M1 FAST 选择题。"""
from __future__ import annotations

import json
import threading
from typing import Any, Mapping

import httpx

from .contracts import RouteDecision
from .spans import SelectedSpan, select_text_span


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


FAST_QUESTIONS = {
    "addressed": {
        "type": "noul",
        "instructions": "Is `utterance` a direct instruction to this computer voice assistant rather than chatter, praise, reading aloud, or thinking out loud?",
    },
    "kind": {
        "type": "choice",
        "instructions": "Which single deterministic FAST action does `utterance` request? The Chinese transcript may contain homophones or ASR errors. Choose goal for anything that needs looking at the screen or multiple UI steps.",
        "criteria": {
            "open_app": "Open or switch to an allowlisted application",
            "open_url": "Open an explicit HTTP or HTTPS URL present verbatim in the utterance",
            "search": "Search the web for a query present verbatim in the utterance",
            "volume_up": "Increase system volume",
            "volume_down": "Decrease system volume",
            "mute": "Mute system audio",
            "unmute": "Unmute system audio",
            "media": "Control media playback: play/pause, next, previous, or stop",
            "shortcut": "Press one allowlisted keyboard shortcut",
            "screenshot": "Open the Windows screenshot capture UI",
            "type_text": "Type text present verbatim in the utterance into the focused control",
            "stop": "Stop or cancel the current voice operation",
            "goal": "A multi-step task that needs screen inspection or clicking a visible control",
            "none": "Not a supported computer command",
        },
    },
    "app": {
        "type": "choice",
        "instructions": "If `utterance` opens an application, which allowlisted application is it? Answer none otherwise.",
        "criteria": {
            "notepad": "Windows Notepad / 记事本",
            "browser": "Default web browser / 浏览器",
            "explorer": "Windows File Explorer / 文件资源管理器",
            "settings": "Windows Settings / 设置",
            "none": "No allowlisted application",
        },
    },
    "media": {
        "type": "choice",
        "instructions": "If `utterance` controls media, which allowlisted operation is requested? Answer none otherwise.",
        "criteria": {
            "play_pause": "Play, pause, or toggle playback",
            "next": "Next track",
            "previous": "Previous track",
            "stop": "Stop playback",
            "none": "No media operation",
        },
    },
    "shortcut": {
        "type": "choice",
        "instructions": "If `utterance` asks for a keyboard shortcut, which allowlisted shortcut is requested? Answer none otherwise.",
        "criteria": {
            "copy": "Copy",
            "paste": "Paste",
            "cut": "Cut",
            "select_all": "Select all",
            "undo": "Undo",
            "redo": "Redo",
            "save": "Save",
            "find": "Find",
            "new_tab": "New browser tab",
            "close_tab": "Close current tab",
            "refresh": "Refresh",
            "switch_window": "Switch window",
            "show_desktop": "Show desktop",
            "escape": "Escape",
            "enter": "Enter",
            "none": "No shortcut",
        },
    },
    "complete": {
        "type": "noul",
        "instructions": "Is `utterance` a complete command with its required object or text present?",
    },
    "destructive": {
        "type": "noul",
        "instructions": "Would carrying out `utterance` destroy data, close unsaved work, send content, spend money, expose credentials, or otherwise be hard to undo?",
    },
}


def _clean_str(value: Any) -> str:
    return " ".join(str(value or "").split())


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _answer_probability(answer: Any, key: str) -> float:
    if not isinstance(answer, dict):
        return 0.0
    return _safe_float(answer.get(key, 0.0))


def _choice(answers: Mapping[str, Any], key: str) -> tuple[str, float]:
    answer = answers.get(key)
    if not isinstance(answer, dict):
        return "none", 0.0
    return str(answer.get("choice") or "none"), _safe_float(answer.get("confidence"))


class _JevRouterBase:
    questions: Mapping[str, Any]

    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        model: str = "jev-latest",
        timeout: float = 15.0,
        client: httpx.Client | None = None,
        cache: Any | None = None,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        # 本机系统代理会截断 TypeSafe TLS；直连仍保持正常证书校验。
        self._client = client or httpx.Client(timeout=timeout, trust_env=False)
        self._owns_client = client is None
        self._lock = threading.Lock()
        # 可选 JevResponseCache（doc 13 §7.4）：相同 utterance/state 在 TTL 内不
        # 重复请求。只缓存成功响应；缺密钥与网络错误路径不进缓存。
        self._cache = cache

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _ask(
        self,
        command: str,
        state: Mapping[str, Any] | None = None,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]] | RouteDecision:
        if not self.api_key:
            return RouteDecision(accepted=False, reason="jev_missing_api_key")
        if state is None:
            request_state: dict[str, Any] = {"utterance": command}
        else:
            request_state = dict(state)
            if not _clean_str(request_state.get("utterance")):
                request_state["utterance"] = command

        cache_key_str: str | None = None
        if self._cache is not None:
            from .jev_cache import cache_key as _make_key

            cache_key_str = _make_key(self.model, request_state, self.questions)
            hit = self._cache.get(cache_key_str)
            if isinstance(hit, Mapping):
                answers = hit.get("answers")
                if not isinstance(answers, dict):
                    answers = {}
                return answers, hit

        body = {
            "state": json.dumps(request_state, ensure_ascii=False),
            "model": self.model,
            "questions": self.questions,
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
        if not isinstance(answers, dict):
            answers = {}
        # Cache only a well-formed successful payload; errors already returned.
        if self._cache is not None and cache_key_str is not None and isinstance(payload, dict):
            self._cache.put(cache_key_str, payload)
        return answers, payload if isinstance(payload, dict) else {}


class JevM0Router(_JevRouterBase):
    """M0 持久连接路由器，严格收敛到 ``open_app:notepad``。"""

    questions = M0_QUESTIONS

    def route(self, command: str, state: Mapping[str, Any] | None = None) -> RouteDecision:
        result = self._ask(command, state)
        if isinstance(result, RouteDecision):
            return result
        answers, payload = result
        addressed = _answer_probability(answers.get("addressed"), "noul")
        complete = _answer_probability(answers.get("complete"), "noul")
        destructive = _answer_probability(answers.get("destructive"), "noul")
        kind, kind_conf = _choice(answers, "kind")
        app, app_conf = _choice(answers, "app")
        confidence = min(kind_conf, app_conf)
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
            raw={"answers": answers, "model": payload.get("model")},
        )


class JevFastRouter(_JevRouterBase):
    """M1 FAST 路由器；自由文本由代码原样截取，不由 Jev 生成。"""

    questions = FAST_QUESTIONS
    _SUPPORTED_KINDS = {
        "open_app",
        "open_url",
        "search",
        "volume_up",
        "volume_down",
        "mute",
        "unmute",
        "media",
        "shortcut",
        "screenshot",
        "type_text",
        "stop",
    }

    def route(self, command: str, state: Mapping[str, Any] | None = None) -> RouteDecision:
        result = self._ask(command, state)
        if isinstance(result, RouteDecision):
            return result
        answers, payload = result
        addressed = _answer_probability(answers.get("addressed"), "noul")
        complete = _answer_probability(answers.get("complete"), "noul")
        destructive = _answer_probability(answers.get("destructive"), "noul")
        kind, kind_conf = _choice(answers, "kind")
        target, target_conf, span = self._target_for(kind, answers, command)
        confidence = min(kind_conf, target_conf) if target_conf is not None else kind_conf
        # kind=goal 需要看屏多步执行，由 orchestrator 分流到 GOAL 通道；它不属于
        # FAST allowlist，因此这里 accepted 恒为 False，但把 needs_screen 提为稳定
        # 字段供上层判断，避免长期从 raw.answers 偷读。
        needs_screen = kind == "goal"
        allowed = (
            addressed >= 0.5
            and complete >= 0.6
            and destructive <= 0.5
            and kind in self._SUPPORTED_KINDS
            and target != "none"
            and confidence >= 0.5
        )
        raw: dict[str, Any] = {
            "answers": answers,
            "model": payload.get("model"),
            "needs_screen": needs_screen,
            "addressed": addressed,
            "complete": complete,
            "destructive_probability": destructive,
        }
        if span is not None:
            raw["span"] = span.as_dict()
            if kind == "search":
                raw["query"] = span.text
            elif kind == "open_url":
                raw["url"] = span.text
            elif kind == "type_text":
                raw["text"] = span.text
        if kind in {"volume_up", "volume_down"}:
            raw["steps"] = 2
        reason = "" if allowed else "FAST route is incomplete, unsafe, low-confidence, or outside the allowlist"
        return RouteDecision(
            accepted=allowed,
            kind=self._normalized_kind(kind),
            target=target,
            confidence=confidence,
            destructive=destructive > 0.5,
            complete=complete >= 0.6,
            reason=reason,
            raw=raw,
        )

    @staticmethod
    def _normalized_kind(kind: str) -> str:
        if kind in {"volume_up", "volume_down"}:
            return "volume"
        return kind

    @staticmethod
    def _target_for(
        kind: str,
        answers: Mapping[str, Any],
        command: str,
    ) -> tuple[str, float | None, SelectedSpan | None]:
        if kind == "open_app":
            app, confidence = _choice(answers, "app")
            return app, confidence, None
        if kind in {"open_url", "search", "type_text"}:
            span = select_text_span(command, kind)
            return (span.text if span is not None else "none"), None, span
        if kind == "volume_up":
            return "up", None, None
        if kind == "volume_down":
            return "down", None, None
        if kind in {"mute", "unmute"}:
            return "on" if kind == "mute" else "off", None, None
        if kind == "media":
            target, confidence = _choice(answers, "media")
            return target, confidence, None
        if kind == "shortcut":
            target, confidence = _choice(answers, "shortcut")
            return target, confidence, None
        if kind == "screenshot":
            return "screen", None, None
        if kind == "stop":
            return "current", None, None
        return "none", 0.0, None
