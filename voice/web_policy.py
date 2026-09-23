"""WEB-GOAL policy engine: ALLOW / REQUIRE_CONFIRMATION / NEEDS_INPUT / BLOCK.

Pure and side-effect free. Implements doc 16 §5 (decision matrix), §5.1
(confirm the FINAL structured plan, not the user's words), §5.3 (page text is
untrusted data and can only tighten policy, never relax it).

Design invariants enforced here and covered by tests:
- The decision depends only on structured fields (operation, effect class,
  role, scheme, sensitive flags). Page text / target labels can make a verdict
  STRICTER but can never downgrade it to ALLOW because they say so.
- Blocked schemes: file:, javascript:, data:, localhost, 127.0.0.1, ::1, and
  private IP ranges. A blocked reason never echoes the full URL.
- TYPE_TEXT / NAVIGATE without a text candidate is NEEDS_INPUT, never a
  fabricated value (select-not-generate).
"""
from __future__ import annotations

import ipaddress
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from .web_contracts import (
    ActionProposal,
    EffectClass,
    Observation,
    PolicyDecision,
    PolicyVerdict,
    WebErrorCode,
    WebOperation,
    WebTarget,
)

# Sensitive terms that force a BLOCK regardless of the operation. Mirrors the
# filter style in perception.py/context.py but is self-contained (no import).
_SENSITIVE_RE = re.compile(
    r"(?:password|passwd|pwd|secret|token|cookie|api[ _-]?key|credential|"
    r"otp|one[ _-]?time[ _-]?code|passcode|captcha|credit[ _-]?card|"
    r"card[ _-]?number|cvv|social[ _-]?security|identity[ _-]?card|"
    r"密码|口令|密钥|令牌|验证码|银行卡|信用卡|身份证)",
    re.IGNORECASE,
)

# Verbs / nouns that mean "change remote state" -> REQUIRE_CONFIRMATION.
_SUBMIT_RE = re.compile(
    r"(?:\bsubmit\b|\bsend\b|\bpost\b|\bpublish\b|\bsave\b|\bedit\b|\bshare\b|"
    r"\bcomment\b|\breply\b|\blike\b|\bfollow\b|\bsubscribe\b|\bcheckout\b|"
    r"\border\b|\bbuy\b|\bpurchase\b|\breserve\b|\bbook\b|\bapply\b|\bconfirm\b|"
    r"\bdelete\b|\bremove\b|\b提交\b|\b发送\b|\b发布\b|\b保存\b|\b编辑\b|\b分享\b|"
    r"\b评论\b|\b回复\b|\b点赞\b|\b关注\b|\b下单\b|\b购买\b|\b预约\b|\b删除\b)",
    re.IGNORECASE,
)

_BLOCKED_SCHEMES = frozenset({"file", "javascript", "data", "vbscript", "about", "chrome"})
_PRIVATE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"})
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")

# Operations that only read or move the viewport: safe by default.
_READ_OPERATIONS = frozenset({WebOperation.SCROLL_UP, WebOperation.SCROLL_DOWN, WebOperation.WAIT})


def _clean(value: Any, limit: int | None = None) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] if limit is not None else text


def _is_private_host(host: str) -> bool:
    host = host.strip().strip("[]").lower()
    if not host:
        return False
    if host in _PRIVATE_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A hostname like "intranet.local" is not an IP; only localhost names
        # and literal private IPs are blocked here.
        return host.endswith(".local") or host.endswith(".internal")
    return address.is_private or address.is_loopback or address.is_link_local


def classify_origin(origin_or_url: str) -> tuple[str | None, bool]:
    """Return (scheme, blocked) for an origin or destination URL.

    ``blocked`` is True for non-http(s) schemes and private/loopback hosts.
    A scheme is detected even without ``://`` (``javascript:``, ``data:``,
    ``file:/``), so those cannot slip through as bare hosts. The returned reason
    is never the full URL.
    """

    raw = _clean(origin_or_url)
    if not raw:
        return None, False
    scheme_match = _SCHEME_RE.match(raw)
    scheme = scheme_match.group(1).lower() if scheme_match else None
    if scheme is not None and scheme not in {"http", "https"}:
        return scheme, True
    try:
        parts = urlsplit(raw if scheme is not None else f"//{raw}")
    except ValueError:
        return scheme, True
    host = (parts.hostname or "").lower()
    if _is_private_host(host):
        return scheme, True
    return scheme, False


def infer_effect_class(
    proposal: ActionProposal,
    target: WebTarget | None,
    candidate_text: str | None = None,
) -> EffectClass:
    """Map a proposal to an effect class using structured fields only."""

    operation = proposal.operation
    if operation in _READ_OPERATIONS:
        return EffectClass.SCROLL if operation != WebOperation.WAIT else EffectClass.READ
    if operation == WebOperation.DONE:
        return EffectClass.READ
    if operation == WebOperation.BLOCKED:
        return EffectClass.UNKNOWN

    label = f"{target.label if target else ''} {target.role if target else ''}"
    text = _clean(candidate_text)

    if operation == WebOperation.NAVIGATE:
        # Navigation to an external URL can start a purchase/login flow; the
        # scheme/origin gate handles the hard blocks, this stays NAVIGATE.
        return EffectClass.NAVIGATE

    if operation in {WebOperation.TYPE_TEXT, WebOperation.SELECT}:
        # Typing into a field is only a state change once submitted; the fill
        # itself is INPUT. A submit button click is handled below.
        return EffectClass.INPUT

    if operation == WebOperation.CLICK:
        if _SUBMIT_RE.search(label):
            return EffectClass.SUBMIT
        if _SENSITIVE_RE.search(f"{label} {text}"):
            return EffectClass.AUTH
        return EffectClass.READ

    return EffectClass.UNKNOWN


def _scheme_block_verdict(proposal: ActionProposal, target: WebTarget | None) -> PolicyVerdict | None:
    del proposal  # scheme screening keys off the resolved target destination only.
    value = target.destination_origin if target else ""
    if value:
        scheme, blocked = classify_origin(value)
        if blocked:
            return PolicyVerdict(
                decision=PolicyDecision.BLOCK,
                effect_class=EffectClass.NAVIGATE,
                reason=f"blocked_navigation_scheme:{scheme or 'private_host'}",
                sensitive_flags=("blocked_scheme",),
            )
    return None


def classify_text_candidate(candidate_text: str | None) -> PolicyVerdict | None:
    """BLOCK when the text a user wants to navigate to / type is a forbidden URL."""

    text = _clean(candidate_text)
    if not text:
        return None
    if "://" in text or text.lower().startswith(("www.", "file:", "javascript:", "data:")):
        scheme, blocked = classify_origin(text)
        if blocked:
            return PolicyVerdict(
                decision=PolicyDecision.BLOCK,
                effect_class=EffectClass.NAVIGATE,
                reason=f"blocked_navigation_scheme:{scheme or 'private_host'}",
                sensitive_flags=("blocked_scheme",),
            )
    return None


def classify_proposal(
    proposal: ActionProposal,
    observation: Observation,
    candidate_text: str | None = None,
) -> PolicyVerdict:
    """Decide one proposal against doc 16 §5.

    ``candidate_text`` is the verbatim span the code selected for a
    text_candidate_id (never a model-generated string). It is used only for
    sensitive/scheme screening, never echoed into the reason.
    """

    code = proposal.validate()
    if code == WebErrorCode.NEEDS_INPUT:
        return PolicyVerdict(
            decision=PolicyDecision.NEEDS_INPUT,
            effect_class=EffectClass.INPUT,
            reason="missing_text_candidate",
        )
    if code is not None:
        return PolicyVerdict(
            decision=PolicyDecision.BLOCK,
            effect_class=EffectClass.UNKNOWN,
            reason=f"invalid_proposal:{code}",
            sensitive_flags=("invalid",),
        )

    target = observation.target(proposal.target_id) if proposal.target_id else None

    # A candidate that targets an element absent from THIS observation is stale;
    # policy defers to the executor's TARGET_STALE, but never allows it.
    if proposal.operation in WebOperation.TARGETED and target is None:
        return PolicyVerdict(
            decision=PolicyDecision.BLOCK,
            effect_class=EffectClass.UNKNOWN,
            reason="target_stale",
            sensitive_flags=("stale",),
        )

    # 1) Hard scheme/origin blocks (destination on the target, or typed URL).
    scheme_block = _scheme_block_verdict(proposal, target)
    if scheme_block is not None:
        return scheme_block
    text_block = classify_text_candidate(candidate_text)
    if text_block is not None:
        return text_block

    # 2) Sensitive content always blocks (credentials, payment, PII).
    sensitive_hits = tuple(
        flag
        for flag, haystack in (
            ("target_sensitive", f"{target.label if target else ''} {target.role if target else ''}"),
            ("candidate_sensitive", _clean(candidate_text)),
        )
        if _SENSITIVE_RE.search(haystack)
    )
    if (target is not None and target.sensitive) or sensitive_hits:
        return PolicyVerdict(
            decision=PolicyDecision.BLOCK,
            effect_class=EffectClass.AUTH,
            reason="sensitive_content",
            sensitive_flags=sensitive_hits or ("target_sensitive",),
        )

    effect_class = infer_effect_class(proposal, target, candidate_text)

    # 3) Effect-class matrix (doc 16 §5).
    if effect_class in {
        EffectClass.DOWNLOAD,
        EffectClass.UPLOAD,
        EffectClass.AUTH,
        EffectClass.PAYMENT,
        EffectClass.ACCOUNT,
    }:
        return PolicyVerdict(
            decision=PolicyDecision.BLOCK,
            effect_class=effect_class,
            reason=f"blocked_effect:{effect_class}",
            sensitive_flags=(effect_class,),
        )
    if effect_class == EffectClass.UNKNOWN:
        # Unknown effect on a mutating operation is blocked; on a read it is fine.
        if proposal.operation in WebOperation.TARGETED:
            return PolicyVerdict(
                decision=PolicyDecision.BLOCK,
                effect_class=EffectClass.UNKNOWN,
                reason="unknown_effect",
                sensitive_flags=("unknown",),
            )
        return PolicyVerdict(decision=PolicyDecision.ALLOW, effect_class=EffectClass.READ, reason="read")

    if effect_class == EffectClass.SUBMIT:
        return PolicyVerdict(
            decision=PolicyDecision.REQUIRE_CONFIRMATION,
            effect_class=EffectClass.SUBMIT,
            reason="remote_state_change",
        )
    if effect_class == EffectClass.INPUT:
        # Filling a third-party form field is a confirmation-worthy state change.
        return PolicyVerdict(
            decision=PolicyDecision.REQUIRE_CONFIRMATION,
            effect_class=EffectClass.INPUT,
            reason="third_party_input",
        )

    # READ / SCROLL / NAVIGATE (already scheme-cleared) are allowed.
    return PolicyVerdict(decision=PolicyDecision.ALLOW, effect_class=effect_class, reason="safe")


def needs_confirmation(verdict: PolicyVerdict) -> bool:
    return verdict.decision == PolicyDecision.REQUIRE_CONFIRMATION


def is_blocked(verdict: PolicyVerdict) -> bool:
    return verdict.decision == PolicyDecision.BLOCK


def is_allowed(verdict: PolicyVerdict) -> bool:
    return verdict.decision == PolicyDecision.ALLOW


def needs_input(verdict: PolicyVerdict) -> bool:
    return verdict.decision == PolicyDecision.NEEDS_INPUT


__all__ = [
    "classify_origin",
    "classify_proposal",
    "classify_text_candidate",
    "infer_effect_class",
    "is_allowed",
    "is_blocked",
    "needs_confirmation",
    "needs_input",
]
