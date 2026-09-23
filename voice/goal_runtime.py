"""Engine-compatible adapter that drives the VoiceOrchestrator (M3 wiring).

The standalone voice service expects an engine exposing ``process_transcript``,
``process_audio``, ``run_once``, ``cancel_current``, ``request_cancel`` and
``stop_current_playback`` (see voice/service.py and voice/engine.py). The
:class:`voice.orchestrator.VoiceOrchestrator` instead exposes ``process`` and
returns an ``OrchestratorResult``.

:class:`GoalEngineAdapter` bridges the two so the FAST/GOAL three-channel
orchestrator becomes reachable from a production runtime WITHOUT changing the
service or the frozen voice.* event contract:

- transcript commands are routed by the orchestrator (FAST or GOAL);
- ``OrchestratorResult`` is mapped back to a :class:`voice.contracts.VoiceResult`
  so downstream status handling (executed / rejected / cancelled /
  permission_denied / wake_miss) is unchanged;
- the wake gate, echo guard, permission linearization and cancellation are still
  owned by the orchestrator and the injected permission gate — this adapter adds
  no new side effects of its own.

It is deliberately thin: no ASR, no TTS, no browser. Those stay in the runtime
that constructs it.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping

import httpx

from .cancellation import VoiceCancelled
from .contracts import ActionResult, AudioSegment, RouteDecision, Transcript, VoiceResult
from .orchestrator import FAST, GOAL, OrchestratorResult


def make_goal_ask(
    url: str,
    api_key: str,
    model: str,
    *,
    client: httpx.Client,
) -> Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]:
    """Build the (state, questions) -> answers transport for the GOAL chooser.

    Reuses the same direct, certificate-validated httpx client as the FAST
    router (``trust_env=False`` avoids the local proxy that truncates TypeSafe
    TLS). With no api_key the callable raises before any network I/O, so the
    chooser degrades to a terminal ``stuck`` step and issues zero requests.
    """

    def ask(state: Mapping[str, Any], questions: Mapping[str, Any]) -> Mapping[str, Any]:
        if not api_key:
            raise RuntimeError("jev_missing_api_key")
        body = {
            "state": json.dumps(dict(state), ensure_ascii=False),
            "model": model,
            "questions": dict(questions),
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        response = client.post(url, json=body, headers=headers)
        response.raise_for_status()
        payload = response.json()
        answers = payload.get("answers") if isinstance(payload, dict) else None
        return answers if isinstance(answers, Mapping) else {}

    return ask


# Orchestrator status -> VoiceResult status. Anything unmapped falls through to
# a rejected/failed VoiceResult carrying the original status as detail.
_STATUS_MAP = {
    "completed": "executed",
    "dry_run": "executed",
    "rejected": "rejected",
    "cancelled": "cancelled",
    "permission_denied": "permission_denied",
    "permission_error": "permission_denied",
    "action_failed": "rejected",
    "goal_failed": "rejected",
    "routing_failed": "rejected",
    "orchestrator_error": "rejected",
    "blocked": "rejected",
    "stuck": "rejected",
    "stalled": "rejected",
    "step_limit": "rejected",
    "invalid_choice": "rejected",
    "no_candidates": "rejected",
    "needs_input": "rejected",
    "confirmation_required": "confirmation_required",
    "commit_unknown": "executed",
    "unverified": "executed",
}


def _decision_from(result: OrchestratorResult) -> RouteDecision | None:
    decision = result.decision
    if isinstance(decision, RouteDecision):
        return decision
    if isinstance(decision, Mapping):
        return RouteDecision(
            accepted=bool(decision.get("accepted", False)),
            kind=str(decision.get("kind", "none")),
            target=str(decision.get("target", "none")),
            confidence=float(decision.get("confidence", 0.0) or 0.0),
            destructive=bool(decision.get("destructive", False)),
            complete=bool(decision.get("complete", True)),
            reason=str(decision.get("reason", "") or ""),
            raw=dict(decision.get("raw") or {}),
        )
    return None


def _action_from(result: OrchestratorResult) -> ActionResult | None:
    action = result.action
    if isinstance(action, ActionResult):
        return action
    if action is None and result.goal is not None:
        # GOAL results carry steps rather than a single ActionResult; summarise.
        steps = getattr(result.goal, "steps", ()) or ()
        if steps:
            last = steps[-1]
            return ActionResult(
                ok=bool(getattr(last, "changed", False)),
                action=f"goal:{getattr(last, 'action', 'step')}",
                detail=str(getattr(last, "detail", "") or ""),
                metadata={"channel": "desktop", "steps": len(steps)},
            )
    if isinstance(action, Mapping):
        return ActionResult(
            bool(action.get("ok", True)),
            str(action.get("action", "")),
            str(action.get("detail", "") or ""),
            dict(action.get("metadata", {}) or {}),
        )
    return None


@dataclass
class GoalEngineAdapter:
    """Adapt a VoiceOrchestrator to the voice engine interface."""

    orchestrator: Any
    recognizer: Any
    permission: Any
    speaker: Any = None
    events: Any = None

    # -- engine interface -------------------------------------------------

    def process_transcript(self, transcript: Any, **_: Any) -> VoiceResult:
        item = transcript if isinstance(transcript, Transcript) else Transcript(text=str(transcript))
        if not self._allowed():
            return VoiceResult(status="permission_denied", detail="voice_control is not granted")
        return self._run(item.text)

    def process_audio(self, audio: AudioSegment, **_: Any) -> VoiceResult:
        # Permission is checked BEFORE transcription so an unauthorized call
        # performs zero ASR, zero Jev and zero actions.
        if not self._allowed():
            return VoiceResult(status="permission_denied", detail="voice_control is not granted")
        transcript = self.recognizer.transcribe(audio)
        return self._run(getattr(transcript, "text", str(transcript)))

    def run_once(self, capture: Any) -> VoiceResult:
        if not self._allowed():
            return VoiceResult(status="permission_denied", detail="voice_control is not granted")
        audio = self._capture(capture)
        return self.process_audio(audio, permission_checked=True)

    def cancel_current(self) -> bool:
        return self._call_bool("cancel_current")

    def request_cancel(self) -> bool:
        return self._call_bool("request_cancel")

    def stop_current_playback(self) -> bool:
        return self._call_bool("stop_current_playback")

    # -- internals --------------------------------------------------------

    def _allowed(self) -> bool:
        allowed = getattr(self.permission, "allowed", None)
        if not callable(allowed):
            return True
        try:
            return bool(allowed())
        except Exception:
            return False

    def _capture(self, capture: Any) -> AudioSegment:
        capture_once = getattr(capture, "capture_utterance", None)
        if not callable(capture_once):
            raise TypeError("goal runtime capture must implement capture_utterance")
        return capture_once()

    def _call_bool(self, name: str) -> bool:
        method = getattr(self.orchestrator, name, None)
        if not callable(method):
            return False
        try:
            return bool(method())
        except Exception:
            return False

    def _run(self, command: str) -> VoiceResult:
        text = " ".join(str(command or "").split())
        if not text:
            return VoiceResult(status="wake_miss")
        try:
            result = self.orchestrator.process(text)
        except VoiceCancelled:
            return VoiceResult(status="cancelled", command=text)
        if not isinstance(result, OrchestratorResult):
            return VoiceResult(
                status="rejected",
                command=text,
                detail="orchestrator returned an invalid result",
            )
        return self._to_voice_result(text, result)

    def _to_voice_result(self, command: str, result: OrchestratorResult) -> VoiceResult:
        status = _STATUS_MAP.get(result.status, "rejected")
        decision = _decision_from(result)
        action = _action_from(result)
        channel = "desktop" if result.mode == GOAL else ("fast" if result.mode == FAST else "none")
        detail = result.detail or result.status
        if channel != "none":
            detail = f"[{channel}] {detail}" if detail else f"[{channel}]"
        return VoiceResult(
            status=status,
            command=command if status not in {"wake_miss", "cancelled"} else "",
            decision=decision,
            action=action,
            detail=detail,
            decisions=(decision,) if decision is not None else (),
            actions=(action,) if action is not None else (),
        )


__all__ = ["GoalEngineAdapter"]
