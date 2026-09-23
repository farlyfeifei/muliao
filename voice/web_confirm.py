"""WEB-GOAL confirmation token store: one-time grants for the final plan.

Implements doc 16 §5.1-5.2. The confirmation object is the FINAL STRUCTURED
PLAN (operation + bound observation + target + input hash + effect class), not
the user's spoken words. A grant is one-time, expires, and is invalidated the
moment any bound field changes — in practice this means a re-observed page
(new page_revision) or a different origin/target/action voids the old grant.

Thread-safe; the clock is injectable so expiry is deterministic in tests. No
I/O, no browser. Input plaintext is never stored — only its SHA-256.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import secrets
import threading
import time
from typing import Any, Callable

from .web_contracts import (
    ActionProposal,
    ConfirmationToken,
    DEFAULT_CONFIRM_TTL_SECONDS,
    EffectClass,
    Observation,
    WebErrorCode,
    WebOperation,
    text_hash,
)

_CONFIRMABLE_OPERATIONS = frozenset(
    {
        WebOperation.CLICK,
        WebOperation.TYPE_TEXT,
        WebOperation.SELECT,
        WebOperation.NAVIGATE,
    }
)


@dataclass
class _Record:
    token: ConfirmationToken
    fingerprint: str
    issued_at: float
    used: bool = False


class ConfirmationStore:
    """Issue and consume one-time confirmation tokens bound to a plan."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._clock = clock
        self._id_factory = id_factory or (lambda: secrets.token_hex(16))
        self._lock = threading.RLock()
        self._records: dict[str, _Record] = {}

    def issue(
        self,
        *,
        operation_run_id: str,
        observation: Observation,
        proposal: ActionProposal,
        effect_class: str = EffectClass.UNKNOWN,
        input_text: str | None = None,
        ttl: float | None = None,
    ) -> ConfirmationToken:
        """Create a pending confirmation for one structured plan."""

        if proposal.operation not in _CONFIRMABLE_OPERATIONS:
            raise ValueError("only mutating operations require confirmation tokens")
        expires = DEFAULT_CONFIRM_TTL_SECONDS if ttl is None else float(ttl)
        if expires <= 0:
            raise ValueError("ttl must be positive")
        input_hash = text_hash(input_text) if input_text is not None else ""
        token = ConfirmationToken(
            token_id=self._id_factory(),
            operation_run_id=str(operation_run_id),
            session_id=observation.session_id,
            tab_id=observation.tab_id,
            origin=observation.origin,
            observation_id=observation.observation_id,
            page_revision=observation.page_revision,
            target_id=proposal.target_id,
            operation=proposal.operation,
            input_text_hash=input_hash,
            effect_class=effect_class,
            expires_after_seconds=expires,
        )
        record = _Record(
            token=token,
            fingerprint=token.plan_fingerprint(),
            issued_at=self._clock(),
        )
        with self._lock:
            self._records[token.token_id] = record
        return token

    def consume(
        self,
        token_id: str,
        observation: Observation,
        proposal: ActionProposal,
        *,
        input_text: str | None = None,
    ) -> tuple[bool, str]:
        """Validate and burn one grant; return (ok, error_code).

        ``observation``/``proposal`` are the FRESH values obtained by
        re-observing after the user confirmed (doc 16 §5.2). The ephemeral
        ``observation_id`` legitimately differs after re-observation, so
        staleness is judged by the document identity ``page_revision`` and the
        page identity (origin/session/tab), not by ``observation_id``.
        """

        now = self._clock()
        with self._lock:
            record = self._records.get(token_id)
            if record is None:
                return False, WebErrorCode.CONFIRM_MISMATCH
            if record.used:
                return False, WebErrorCode.CONFIRM_USED
            token = record.token
            if now - record.issued_at > token.expires_after_seconds:
                record.used = True
                return False, WebErrorCode.CONFIRM_EXPIRED

            # Page identity must be unchanged since the plan was confirmed.
            if observation.origin != token.origin:
                return False, WebErrorCode.ORIGIN_CHANGED
            if (
                observation.session_id != token.session_id
                or observation.tab_id != token.tab_id
            ):
                return False, WebErrorCode.CONFIRM_MISMATCH
            # A replaced document (new page_revision) voids the old grant.
            if observation.page_revision != token.page_revision:
                return False, WebErrorCode.TARGET_STALE

            # The plan itself must match field-for-field.
            if proposal.operation != token.operation:
                return False, WebErrorCode.CONFIRM_MISMATCH
            if proposal.target_id != token.target_id:
                return False, WebErrorCode.CONFIRM_MISMATCH
            input_hash = text_hash(input_text) if input_text is not None else ""
            if input_hash != token.input_text_hash:
                return False, WebErrorCode.CONFIRM_MISMATCH
            # The confirmed target must still exist in the fresh observation.
            if not proposal.binds(observation):
                return False, WebErrorCode.TARGET_STALE

            record.used = True
            return True, ""

    def revoke(self, operation_run_id: str | None = None) -> int:
        """Drop pending grants for one run (or all). Returns count removed."""

        with self._lock:
            if operation_run_id is None:
                count = len(self._records)
                self._records.clear()
                return count
            doomed = [
                token_id
                for token_id, record in self._records.items()
                if record.token.operation_run_id == operation_run_id
            ]
            for token_id in doomed:
                del self._records[token_id]
            return len(doomed)

    def pending(self, operation_run_id: str) -> ConfirmationToken | None:
        """Return the open token for a run, for UI display (no plaintext)."""

        with self._lock:
            for record in self._records.values():
                if (
                    record.token.operation_run_id == operation_run_id
                    and not record.used
                ):
                    return record.token
            return None

    def describe_for_user(self, token: ConfirmationToken, observation: Observation) -> str:
        """Confirmation copy for the FINAL plan; never a full URL or plaintext."""

        target = observation.target(token.target_id)
        label = target.label if target is not None else token.target_id
        role = target.role if target is not None else "element"
        verb = {
            WebOperation.CLICK: "点击",
            WebOperation.TYPE_TEXT: "填写",
            WebOperation.SELECT: "选择",
            WebOperation.NAVIGATE: "跳转",
        }.get(token.operation, token.operation)
        return f"将在 {token.origin} 对『{label}』({role}) 执行 {verb}"


__all__ = ["ConfirmationStore"]
