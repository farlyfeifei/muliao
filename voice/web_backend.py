"""WEB-GOAL browser backend abstraction + in-memory fake (M3A).

The backend is the ONLY place a real browser would ever be touched. M3A ships a
FakeDomBackend so the whole observe->route->policy->confirm->act->verify loop is
exercised with zero Chrome, zero CDP, zero network. M3B adds a real CDP backend
behind the same :class:`BrowserBackend` protocol without changing the loop.

Contract notes (doc 16 §4, §7.3):
- observe() returns a RawDocument; the ObservationBuilder turns it into a frozen
  Observation and mints a page_revision from the document token, so a navigation
  is detectable as a new revision (stale targets become TARGET_STALE).
- act() returns an ActResult carrying a CommitState. A fired-but-unobservable
  mutation returns commit_state=UNKNOWN, which the loop must never auto-retry.
- No selectors/coordinates/JS ever cross this boundary from the model: act()
  receives a resolved proposal (target backend id + verbatim text), all of which
  the code derived, never the model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .web_contracts import CommitState, WebErrorCode, WebOperation


@dataclass(frozen=True)
class RawDocument:
    """A backend's raw view of the current document, pre-numbering."""

    document_token: str
    origin: str
    title: str = ""
    text: str = ""
    loading_state: str = "complete"
    raw_elements: Sequence[Mapping[str, Any]] = field(default_factory=tuple)


@dataclass(frozen=True)
class ActResult:
    """Outcome of one executed (or dry-run) browser action."""

    ok: bool
    commit_state: str = CommitState.NOT_COMMITTED
    error_code: str = ""
    navigated_to: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if self.commit_state not in CommitState.ALL:
            raise ValueError(f"unknown commit state: {self.commit_state}")


@runtime_checkable
class BrowserBackend(Protocol):
    """Minimal browser surface the WEB-GOAL loop needs."""

    def observe(self) -> RawDocument: ...

    def act(
        self,
        *,
        operation: str,
        target_backend_id: str = "",
        text: str = "",
        option_id: str = "",
        dry_run: bool = True,
    ) -> ActResult: ...


@dataclass
class _FakePage:
    token: str
    origin: str
    title: str
    text: str
    elements: list[dict[str, Any]]


class FakeDomBackend:
    """In-memory DOM for M3A dry-run tests. Deterministic, no I/O.

    Pages are keyed by a token; ``navigate_to`` swaps the current page so the
    document token changes and the builder mints a new page_revision. Actions
    mutate element state (fill/select) or navigate, letting the verifier see a
    real before/after difference — all in memory.
    """

    def __init__(self, pages: Mapping[str, _FakePage], start: str) -> None:
        self._pages: dict[str, _FakePage] = {name: page for name, page in pages.items()}
        if start not in self._pages:
            raise ValueError(f"unknown start page: {start}")
        self._current = start
        self.act_log: list[dict[str, Any]] = []

    @property
    def current_token(self) -> str:
        return self._current

    def observe(self) -> RawDocument:
        page = self._pages[self._current]
        return RawDocument(
            document_token=page.token,
            origin=page.origin,
            title=page.title,
            text=page.text,
            loading_state="complete",
            raw_elements=tuple(dict(item) for item in page.elements),
        )

    def navigate(self, token: str) -> None:
        if token not in self._pages:
            raise ValueError(f"unknown page token: {token}")
        self._current = token

    def act(
        self,
        *,
        operation: str,
        target_backend_id: str = "",
        text: str = "",
        option_id: str = "",
        dry_run: bool = True,
    ) -> ActResult:
        record = {
            "operation": operation,
            "target": target_backend_id,
            "dry_run": dry_run,
        }
        self.act_log.append(record)
        if dry_run:
            # Dry-run never mutates and never commits a side effect.
            return ActResult(ok=True, commit_state=CommitState.NOT_COMMITTED, detail="dry-run")

        page = self._pages[self._current]
        if operation == WebOperation.NAVIGATE:
            # The navigation destination is a code-selected text candidate (a URL
            # or a fake page token), never a model-produced string.
            destination = text or target_backend_id
            if destination in self._pages:
                self._current = destination
                return ActResult(True, CommitState.COMMITTED, navigated_to=destination)
            return ActResult(False, CommitState.UNKNOWN, WebErrorCode.COMMIT_UNKNOWN,
                             detail="navigation destination unresolved")
        if operation == WebOperation.TYPE_TEXT:
            element = self._find(page, target_backend_id)
            if element is None:
                return ActResult(False, CommitState.NOT_COMMITTED, WebErrorCode.TARGET_STALE)
            element["value"] = text
            return ActResult(True, CommitState.COMMITTED)
        if operation == WebOperation.SELECT:
            element = self._find(page, target_backend_id)
            if element is None:
                return ActResult(False, CommitState.NOT_COMMITTED, WebErrorCode.TARGET_STALE)
            element["selected_option"] = option_id
            return ActResult(True, CommitState.COMMITTED)
        if operation == WebOperation.CLICK:
            element = self._find(page, target_backend_id)
            if element is None:
                return ActResult(False, CommitState.NOT_COMMITTED, WebErrorCode.TARGET_STALE)
            # A click on a link/button with a destination navigates.
            destination = element.get("navigates_to")
            if destination and destination in self._pages:
                self._current = destination
                return ActResult(True, CommitState.COMMITTED, navigated_to=destination)
            element["clicked"] = True
            return ActResult(True, CommitState.COMMITTED)
        if operation in {WebOperation.SCROLL_UP, WebOperation.SCROLL_DOWN}:
            return ActResult(True, CommitState.COMMITTED, detail="scrolled")
        if operation == WebOperation.WAIT:
            return ActResult(True, CommitState.COMMITTED, detail="waited")
        return ActResult(False, CommitState.NOT_COMMITTED, WebErrorCode.INVALID_PROPOSAL,
                         detail="unsupported operation")

    @staticmethod
    def _find(page: _FakePage, backend_id: str) -> dict[str, Any] | None:
        for element in page.elements:
            if str(element.get("backend_id", "")) == backend_id:
                return element
        return None


def page(token: str, origin: str, title: str = "", text: str = "",
         elements: Sequence[Mapping[str, Any]] = ()) -> _FakePage:
    """Convenience constructor for a fake page."""

    return _FakePage(
        token=token,
        origin=origin,
        title=title,
        text=text,
        elements=[dict(item) for item in elements],
    )


__all__ = [
    "ActResult",
    "BrowserBackend",
    "FakeDomBackend",
    "RawDocument",
    "page",
]
