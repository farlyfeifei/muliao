"""WEB-GOAL dry-run-first loop wiring the M3A modules into one flow.

    observe -> route (2nd Jev) -> policy -> [confirm] -> act -> re-observe -> verify

Design rules honoured here (doc 16):
- Dry-run is the default; ``act=True`` is required to touch the backend.
- The model never produces selectors/coordinates/JS/text: the loop resolves the
  target's code-owned backend id from the Observation and passes verbatim
  candidate text — both derived by code.
- Cancellation is checked at every stage boundary; after a cancel no new side
  effect is committed.
- An action whose result is unobservable returns commit_state=UNKNOWN and is
  NEVER auto-retried; the loop reports it and stops.
- Only a ``satisfied`` verification produces status "completed".

The loop is backend-agnostic: M3A injects FakeDomBackend, M3B a CDP backend.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .cancellation import VoiceCancelled
from .web_backend import ActResult, BrowserBackend, RawDocument
from .web_confirm import ConfirmationStore
from .web_contracts import (
    ActionProposal,
    CommitState,
    Observation,
    PolicyDecision,
    WebErrorCode,
    WebOperation,
)
from .web_goal_router import WebGoalDecision, WebGoalRouter
from .web_observation import ObservationBuilder, UnsupportedSurface
from .web_policy import classify_proposal
from .web_verifier import Criteria, verify

MAX_STEPS = 8


@dataclass(frozen=True)
class WebGoalStep:
    index: int
    operation: str
    target_id: str = ""
    policy_decision: str = ""
    effect_class: str = ""
    dry_run: bool = True
    commit_state: str = CommitState.NOT_COMMITTED
    detail: str = ""


@dataclass(frozen=True)
class WebGoalResult:
    status: str
    goal: str
    steps: tuple[WebGoalStep, ...] = ()
    observation: Observation | None = None
    unsupported: tuple[UnsupportedSurface, ...] = ()
    error_code: str = ""
    commit_state: str = CommitState.NOT_COMMITTED
    detail: str = ""

    @property
    def completed(self) -> bool:
        return self.status == "completed"


def _raise_if_cancelled(cancellation: Any) -> None:
    if cancellation is None:
        return
    raiser = getattr(cancellation, "raise_if_cancelled", None)
    if callable(raiser):
        raiser()
        return
    value = getattr(cancellation, "cancelled", False)
    if callable(value):
        value = value()
    if value:
        raise VoiceCancelled("web goal cancelled")


@dataclass
class WebGoalLoop:
    """Bounded, cancellable, dry-run-first WEB-GOAL executor."""

    backend: BrowserBackend
    router: WebGoalRouter
    builder: ObservationBuilder
    confirm_store: ConfirmationStore | None = None
    dry_run: bool = True
    max_steps: int = MAX_STEPS
    candidates: Mapping[str, str] = field(default_factory=dict)
    clock: Callable[[], float] | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.max_steps <= MAX_STEPS:
            raise ValueError(f"max_steps must be between 1 and {MAX_STEPS}")

    def _observe(self) -> tuple[Observation, tuple[UnsupportedSurface, ...]]:
        document: RawDocument = self.backend.observe()
        self.builder.begin_document(document.document_token)
        return self.builder.build(
            observation_id=f"obs-{document.document_token}",
            origin=document.origin,
            title=document.title,
            text=document.text,
            loading_state=document.loading_state,
            raw_elements=document.raw_elements,
            document_token=document.document_token,
        )

    def run(
        self,
        goal: str,
        *,
        criteria: Sequence[Criteria] = (),
        cancellation: Any = None,
        confirm_provider: Callable[[ActionProposal, Observation, str], str | None] | None = None,
    ) -> WebGoalResult:
        if not goal.strip():
            raise ValueError("goal cannot be empty")

        steps: list[WebGoalStep] = []
        observation, unsupported = self._observe()
        _raise_if_cancelled(cancellation)

        for index in range(1, self.max_steps + 1):
            decision: WebGoalDecision = self.router.route(
                goal, observation, self.candidates
            )
            _raise_if_cancelled(cancellation)

            if decision.operation == WebOperation.DONE and decision.ok:
                final_observation = observation
                result = verify(criteria, final_observation) if criteria else None
                if criteria and result is not None and not result.satisfied:
                    return WebGoalResult(
                        "unverified", goal, tuple(steps), final_observation,
                        unsupported, WebErrorCode.VERIFICATION_UNSATISFIED,
                        detail="model reported done but criteria not satisfied",
                    )
                return WebGoalResult("completed", goal, tuple(steps), final_observation, unsupported)

            if not decision.ok or decision.proposal is None:
                return WebGoalResult(
                    "blocked" if decision.operation == WebOperation.BLOCKED else "needs_input",
                    goal, tuple(steps), observation, unsupported,
                    decision.error_code or WebErrorCode.CAPABILITY_UNAVAILABLE,
                    detail=decision.reason,
                )

            proposal = decision.proposal
            # Resolve the code-owned backend id; the model only ever saw target_id.
            target = observation.target(proposal.target_id)
            backend_id = ""
            if target is not None and isinstance(target.metadata, Mapping):
                backend_id = str(target.metadata.get("backend_id", ""))
            candidate_text = self.candidates.get(proposal.text_candidate_id, "")

            verdict = classify_proposal(proposal, observation, candidate_text or None)
            _raise_if_cancelled(cancellation)

            if verdict.decision == PolicyDecision.BLOCK:
                return WebGoalResult(
                    "blocked", goal, tuple(steps), observation, unsupported,
                    WebErrorCode.POLICY_BLOCKED, detail=verdict.reason,
                )
            if verdict.decision == PolicyDecision.NEEDS_INPUT:
                return WebGoalResult(
                    "needs_input", goal, tuple(steps), observation, unsupported,
                    WebErrorCode.NEEDS_INPUT, detail=verdict.reason,
                )
            if verdict.decision == PolicyDecision.REQUIRE_CONFIRMATION:
                if confirm_provider is None or self.confirm_store is None:
                    return WebGoalResult(
                        "confirmation_required", goal, tuple(steps), observation,
                        unsupported, WebErrorCode.CONFIRM_REQUIRED, detail=verdict.reason,
                    )
                token = self.confirm_store.issue(
                    operation_run_id=f"run-{index}",
                    observation=observation,
                    proposal=proposal,
                    effect_class=verdict.effect_class,
                    input_text=candidate_text or None,
                )
                granted_token_id = confirm_provider(proposal, observation, token.token_id)
                _raise_if_cancelled(cancellation)
                # Re-observe after confirmation, then re-run policy (doc 16 §5.2).
                fresh_observation, fresh_unsupported = self._observe()
                unsupported = unsupported + tuple(
                    item for item in fresh_unsupported if item not in unsupported
                )
                fresh_target = fresh_observation.target(proposal.target_id)
                if fresh_target is None:
                    return WebGoalResult(
                        "blocked", goal, tuple(steps), fresh_observation, unsupported,
                        WebErrorCode.TARGET_STALE, detail="confirmed target vanished",
                    )
                fresh_verdict = classify_proposal(proposal, fresh_observation, candidate_text or None)
                # Re-running policy on the SAME confirmed plan will still say
                # REQUIRE_CONFIRMATION for a mutating action; that is unchanged,
                # not a revocation. Only an ESCALATION to BLOCK/NEEDS_INPUT (the
                # page changed underneath us) voids the grant (doc 16 §5.2).
                if fresh_verdict.decision in {
                    PolicyDecision.BLOCK,
                    PolicyDecision.NEEDS_INPUT,
                }:
                    return WebGoalResult(
                        "blocked", goal, tuple(steps), fresh_observation, unsupported,
                        WebErrorCode.POLICY_BLOCKED, detail="policy escalated after confirmation",
                    )
                if granted_token_id is None:
                    return WebGoalResult(
                        "confirmation_required", goal, tuple(steps), fresh_observation,
                        unsupported, WebErrorCode.CONFIRM_REQUIRED, detail="user did not confirm",
                    )
                ok, code = self.confirm_store.consume(
                    granted_token_id, fresh_observation, proposal,
                    input_text=candidate_text or None,
                )
                if not ok:
                    return WebGoalResult(
                        "blocked", goal, tuple(steps), fresh_observation, unsupported,
                        code or WebErrorCode.CONFIRM_MISMATCH, detail="confirmation invalid",
                    )
                observation = fresh_observation
                target = fresh_target
                backend_id = str(target.metadata.get("backend_id", "")) if isinstance(target.metadata, Mapping) else ""

            act_result: ActResult = self.backend.act(
                operation=proposal.operation,
                target_backend_id=backend_id,
                text=candidate_text,
                option_id=proposal.select_option_id,
                dry_run=self.dry_run,
            )
            _raise_if_cancelled(cancellation)

            steps.append(
                WebGoalStep(
                    index=index,
                    operation=proposal.operation,
                    target_id=proposal.target_id,
                    policy_decision=verdict.decision,
                    effect_class=verdict.effect_class,
                    dry_run=self.dry_run,
                    commit_state=act_result.commit_state,
                    detail=act_result.detail,
                )
            )

            if act_result.commit_state == CommitState.UNKNOWN:
                # Fired but unobservable: report and stop. NEVER auto-retry.
                return WebGoalResult(
                    "commit_unknown", goal, tuple(steps), observation, unsupported,
                    WebErrorCode.COMMIT_UNKNOWN, CommitState.UNKNOWN,
                    detail="action may have taken effect; not retried",
                )
            if not act_result.ok:
                return WebGoalResult(
                    "action_failed", goal, tuple(steps), observation, unsupported,
                    act_result.error_code or WebErrorCode.INVALID_PROPOSAL,
                    act_result.commit_state, detail=act_result.detail,
                )

            if self.dry_run:
                # Dry-run does not change the page; stop after planning one step.
                return WebGoalResult(
                    "dry_run", goal, tuple(steps), observation, unsupported,
                    detail="dry-run: no side effects",
                )

            # Re-observe after a real action and continue the loop.
            observation, more_unsupported = self._observe()
            unsupported = unsupported + tuple(
                item for item in more_unsupported if item not in unsupported
            )
            _raise_if_cancelled(cancellation)

        return WebGoalResult(
            "step_limit", goal, tuple(steps), observation, unsupported,
            WebErrorCode.BUDGET_EXHAUSTED, detail=f"reached {self.max_steps} steps",
        )


__all__ = [
    "MAX_STEPS",
    "WebGoalLoop",
    "WebGoalResult",
    "WebGoalStep",
]
