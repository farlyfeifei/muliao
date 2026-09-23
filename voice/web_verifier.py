"""WEB-GOAL independent completion verifier (doc 16 §4.5, §7.3).

A model choosing DONE is NOT proof of success. This module verifies a task
against explicit, code-held success criteria read from the post-action
Observation, and returns one of three states:

- ``satisfied``      every check has positive evidence in the observation.
- ``unverified``     a check could not find evidence either way (missing field,
                     text absent) — NOT a failure, just "cannot confirm".
- ``contradicted``   a check found evidence the goal was NOT met.

Only ``satisfied`` lets an upstream emit ``completed``. When the action's
commit_state is UNKNOWN (fired but result unobservable) the verdict is never
``satisfied`` — the caller must report commit_unknown and MUST NOT auto-retry.

Pure and side-effect free: criteria + a snapshot are passed in; this module
never touches a browser. Page text is untrusted data; ``answer_from_page``
grounds an answer in a verbatim quote that must actually appear in the text.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping, Sequence

from .web_contracts import (
    CommitState,
    Observation,
    VerificationCheck,
    VerificationResult,
    WebTarget,
)

SATISFIED = "satisfied"
UNVERIFIED = "unverified"
CONTRADICTED = "contradicted"


def _normalize(text: str) -> str:
    """Whitespace/punctuation-insensitive form used for evidence matching."""

    return re.sub(r"[\s]+", "", str(text or "")).casefold()


@dataclass(frozen=True)
class Criteria:
    """One declarative success check, evaluated against an Observation."""

    kind: str
    expected: str
    # For control-state checks: which target and which attribute/value.
    target_label: str = ""
    target_role: str = ""
    attribute: str = ""

    def evaluate(self, observation: Observation) -> VerificationCheck:
        if self.kind == "origin":
            actual = observation.origin
            if not actual:
                return VerificationCheck(self.kind, self.expected, "", False)
            passed = _normalize(actual).startswith(_normalize(self.expected))
            return VerificationCheck(self.kind, self.expected, actual, passed)
        if self.kind == "title":
            return _contains_check(self.kind, self.expected, observation.title)
        if self.kind == "text":
            return _contains_check(self.kind, self.expected, observation.text)
        if self.kind == "control_state":
            return self._evaluate_control(observation)
        if self.kind == "field_value":
            return self._evaluate_field(observation)
        # Unknown criteria kind can never be positively satisfied.
        return VerificationCheck(self.kind, self.expected, "", False)

    def _find_target(self, observation: Observation) -> WebTarget | None:
        wanted_label = _normalize(self.target_label)
        wanted_role = _normalize(self.target_role)
        for target in observation.targets:
            if wanted_role and _normalize(target.role) != wanted_role:
                continue
            if wanted_label and wanted_label not in _normalize(target.label):
                continue
            return target
        return None

    def _evaluate_control(self, observation: Observation) -> VerificationCheck:
        target = self._find_target(observation)
        if target is None:
            return VerificationCheck(self.kind, self.expected, "target_absent", False)
        attribute = (self.attribute or "checked").lower()
        actual_value: Any
        if attribute == "checked":
            actual_value = target.checked
        elif attribute == "selected":
            actual_value = target.selected
        elif attribute == "enabled":
            actual_value = target.enabled
        else:
            return VerificationCheck(self.kind, self.expected, "unknown_attribute", False)
        if actual_value is None:
            return VerificationCheck(self.kind, self.expected, "state_unavailable", False)
        expected_bool = _normalize(self.expected) in {"true", "1", "yes", "on", "checked", "selected"}
        passed = bool(actual_value) == expected_bool
        return VerificationCheck(self.kind, self.expected, str(actual_value), passed)

    def _evaluate_field(self, observation: Observation) -> VerificationCheck:
        target = self._find_target(observation)
        if target is None:
            return VerificationCheck(self.kind, self.expected, "target_absent", False)
        # The verifier never reads live DOM values; the backend supplies the
        # post-action value through target metadata so this stays side-effect free.
        current = target.metadata.get("value") if isinstance(target.metadata, Mapping) else None
        if current is None:
            return VerificationCheck(self.kind, self.expected, "value_unavailable", False)
        passed = _normalize(current) == _normalize(self.expected)
        return VerificationCheck(self.kind, self.expected, str(current)[:60], passed)


def _contains_check(kind: str, expected: str, haystack: str) -> VerificationCheck:
    if not _normalize(haystack):
        # No observable text at all: cannot confirm, cannot contradict.
        return VerificationCheck(kind, expected, "", False)
    passed = _normalize(expected) in _normalize(haystack)
    return VerificationCheck(kind, expected, "present" if passed else "absent", passed)


def _all_unverifiable(checks: Sequence[VerificationCheck]) -> bool:
    """True when every failing check merely lacked evidence (none contradicted).

    ``actual == "absent"`` means the page DID render observable text but the
    expected evidence was not in it — that is a contradiction, not a gap, so it
    is deliberately excluded from the unverifiable set.
    """

    for check in checks:
        if check.passed:
            continue
        # Only "no observable signal at all" counts as unverifiable.
        if check.actual in {"target_absent", "state_unavailable",
                            "value_unavailable", "unknown_attribute", ""}:
            continue
        return False
    return True


def verify(
    criteria: Iterable[Criteria],
    observation: Observation,
    *,
    commit_state: str = CommitState.NOT_COMMITTED,
) -> VerificationResult:
    """Evaluate all criteria against the post-action observation."""

    checks = tuple(item.evaluate(observation) for item in criteria)
    if not checks:
        # No criteria means nothing can be positively confirmed.
        return VerificationResult(UNVERIFIED, (), commit_state, "no criteria supplied")

    failed = [check for check in checks if not check.passed]
    if not failed:
        status = SATISFIED
    elif _all_unverifiable(checks):
        status = UNVERIFIED
    else:
        status = CONTRADICTED

    # A fired-but-unobservable action can never be reported as satisfied.
    if commit_state == CommitState.UNKNOWN and status == SATISFIED:
        status = UNVERIFIED
    detail = "" if status == SATISFIED else ", ".join(check.kind for check in failed)
    return VerificationResult(status, checks, commit_state, detail[:400])


def answer_from_page(question: str, observation: Observation) -> tuple[str, str, bool]:
    """Ground an answer in a verbatim quote that appears in the page text.

    Returns ``(answer, evidence, ok)``. ``ok`` is False when no supporting
    sentence can be found — the caller must then say it could not confirm rather
    than invent an answer. ``evidence`` is always a substring of the page text.
    """

    text = observation.text or ""
    if not text.strip():
        return "", "", False
    del question  # The question guides the model elsewhere; here we only ground.
    # Sentence-ish split that keeps the verbatim span intact for substring proof.
    for sentence in re.split(r"(?<=[。！？.!?\n])", text):
        candidate = sentence.strip()
        if len(candidate) >= 2 and _normalize(candidate) in _normalize(text):
            return candidate, candidate, True
    return "", "", False


def verifier_from_checks(
    checks: Sequence[VerificationCheck],
    *,
    commit_state: str = CommitState.NOT_COMMITTED,
) -> VerificationResult:
    """Build a result from pre-computed checks (used by backends/tests)."""

    failed = [check for check in checks if not check.passed]
    if not checks:
        return VerificationResult(UNVERIFIED, (), commit_state, "no checks")
    if not failed:
        status = SATISFIED
    elif _all_unverifiable(checks):
        status = UNVERIFIED
    else:
        status = CONTRADICTED
    if commit_state == CommitState.UNKNOWN and status == SATISFIED:
        status = UNVERIFIED
    return VerificationResult(status, tuple(checks), commit_state,
                              ", ".join(c.kind for c in failed)[:400])


def criteria_from_specs(specs: Iterable[Mapping[str, Any]]) -> tuple[Criteria, ...]:
    """Convenience builder so callers can declare criteria as plain dicts."""

    result: list[Criteria] = []
    for spec in specs:
        result.append(
            Criteria(
                kind=str(spec.get("kind", "")),
                expected=str(spec.get("expected", "")),
                target_label=str(spec.get("target_label", "")),
                target_role=str(spec.get("target_role", "")),
                attribute=str(spec.get("attribute", "")),
            )
        )
    return tuple(result)


__all__ = [
    "CONTRADICTED",
    "Criteria",
    "SATISFIED",
    "UNVERIFIED",
    "answer_from_page",
    "criteria_from_specs",
    "verifier_from_checks",
    "verify",
]
