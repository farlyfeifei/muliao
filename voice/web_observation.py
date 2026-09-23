"""WEB-GOAL observation builder: node identity, page_revision, target numbering.

Turns a backend-provided document snapshot plus raw elements into a frozen
``voice.web_contracts.Observation``. Implements the core mechanisms verified
against jev-ultrafast@1231850a and doc 16 sections 2.2 / 4.1:

- The code owns stable node identity. Each raw element is mapped to an integer
  node handle held by the builder; a navigation or document replacement yields
  a new ``page_revision`` so old target ids can be rejected as TARGET_STALE.
- ``target_id`` values (``e001``...) are observation-local and never reused
  across observations.
- Privacy filters run before numbering: password/auth/sensitive elements and
  unsupported surfaces are dropped, never sent to the model.
- Pure and side-effect free: no I/O, no browser, no Jev. Backends inject the
  raw document; M3A uses a fake DOM, M3B a real CDP adapter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Any, Iterable, Mapping, Sequence

from .web_contracts import (
    MAX_TARGETS,
    Observation,
    WebOperation,
    WebTarget,
)

_SENSITIVE_ROLE_RE = re.compile(
    r"(?:password|passwd|secret|otp|one[ _-]?time|passcode|credit[ _-]?card|"
    r"card[ _-]?number|cvv|captcha|验证码|密码|口令|银行卡|信用卡)",
    re.IGNORECASE,
)
_SENSITIVE_INPUT_TYPES = frozenset(
    {"password", "file", "hidden", "otp", "one-time-code"}
)
_UNSUPPORTED_SURFACE_RE = re.compile(
    r"(?:iframe|shadow|canvas|embed|object|applet)", re.IGNORECASE
)

# Role -> operations the executor is willing to attempt for that role.
_ROLE_OPERATIONS: Mapping[str, tuple[str, ...]] = {
    "button": (WebOperation.CLICK,),
    "link": (WebOperation.CLICK,),
    "checkbox": (WebOperation.CLICK,),
    "radio": (WebOperation.CLICK,),
    "switch": (WebOperation.CLICK,),
    "tab": (WebOperation.CLICK,),
    "menuitem": (WebOperation.CLICK,),
    "option": (WebOperation.CLICK,),
    "textbox": (WebOperation.CLICK, WebOperation.TYPE_TEXT),
    "searchbox": (WebOperation.CLICK, WebOperation.TYPE_TEXT),
    "combobox": (WebOperation.CLICK, WebOperation.TYPE_TEXT, WebOperation.SELECT),
    "select": (WebOperation.SELECT,),
    "contenteditable": (WebOperation.CLICK, WebOperation.TYPE_TEXT),
}
_DEFAULT_OPERATIONS = (WebOperation.CLICK,)


def _clean(value: Any, limit: int | None = None) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] if limit is not None else text


def _first(item: Mapping[str, Any], names: Sequence[str], default: Any = None) -> Any:
    for name in names:
        if name in item and item[name] is not None:
            return item[name]
    return default


@dataclass
class UnsupportedSurface:
    """A structure the first version refuses to handle (doc 16: never degrade)."""

    kind: str
    detail: str = ""


@dataclass(frozen=True)
class NodeIdentity:
    """Code-owned stable handle for one raw element within a document."""

    node: int
    backend_id: str = ""


@dataclass
class ObservationBuilder:
    """Assigns code-owned node identity and builds frozen observations.

    ``page_revision`` changes whenever ``begin_document`` is called with a new
    document token (navigation, reload, or SPA document replacement), which is
    how a stale ``target_id`` becomes detectable.
    """

    session_id: str
    tab_id: str
    _next_node: int = field(default=0, init=False)
    _page_revision: str = field(default="", init=False)
    _document_token: str = field(default="", init=False)
    # backend_id -> stable node handle for the CURRENT document. Node identity
    # persists across re-observations of the same document (so the code holds a
    # stable reference to the real element); target_id, by contrast, is
    # observation-local and renumbered on every build().
    _handles: dict[str, int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.session_id = _clean(self.session_id)
        self.tab_id = _clean(self.tab_id)
        if not self.session_id or not self.tab_id:
            raise ValueError("ObservationBuilder requires session_id and tab_id")

    @property
    def page_revision(self) -> str:
        return self._page_revision

    def begin_document(self, document_token: str) -> str:
        """Register a new document; return its page_revision.

        A different token than the current document means the page changed, so a
        fresh revision is minted and all prior node handles are invalidated.
        """

        token = _clean(document_token)
        if not token:
            raise ValueError("document_token must be non-empty")
        if token != self._document_token:
            self._document_token = token
            self._page_revision = hashlib.sha256(
                f"{self.session_id}|{self.tab_id}|{token}".encode("utf-8")
            ).hexdigest()[:16]
            self._handles.clear()
            self._next_node = 0
        return self._page_revision

    def _identity(self, backend_id: str) -> NodeIdentity:
        # Stable per (document, backend_id): re-observing the same document
        # returns the same node handle, so the code's reference to the real
        # element survives across observations until the document is replaced.
        existing = self._handles.get(backend_id)
        if existing is not None:
            return NodeIdentity(node=existing, backend_id=backend_id)
        self._next_node += 1
        node = self._next_node
        self._handles[backend_id] = node
        return NodeIdentity(node=node, backend_id=backend_id)

    def build(
        self,
        *,
        observation_id: str,
        origin: str,
        title: str = "",
        text: str = "",
        loading_state: str = "unknown",
        raw_elements: Iterable[Mapping[str, Any]] = (),
        document_token: str = "",
    ) -> tuple[Observation, tuple[UnsupportedSurface, ...]]:
        """Build a frozen observation plus any unsupported-surface reports.

        ``document_token`` (when given) is passed to :meth:`begin_document` so a
        navigation detected by the backend advances the revision atomically.
        ``target_id`` numbering restarts at ``e001`` for every build so ids are
        observation-local and never reused across observations.
        """

        if document_token:
            self.begin_document(document_token)
        if not self._page_revision:
            raise ValueError("begin_document must be called before build")

        targets: list[WebTarget] = []
        unsupported: list[UnsupportedSurface] = []
        omitted = 0
        for raw in raw_elements:
            element, surface = self._coerce(raw, len(targets) + 1)
            if surface is not None:
                unsupported.append(surface)
                continue
            if element is None:
                continue
            if len(targets) >= MAX_TARGETS:
                omitted += 1
                continue
            targets.append(element)

        observation = Observation(
            observation_id=_clean(observation_id),
            page_revision=self._page_revision,
            session_id=self.session_id,
            tab_id=self.tab_id,
            origin=_clean(origin),
            title=title,
            loading_state=loading_state,
            text=text,
            targets=tuple(targets),
            omitted_target_count=omitted,
        )
        return observation, tuple(unsupported)

    def _coerce(
        self, raw: Mapping[str, Any], sequence: int
    ) -> tuple[WebTarget | None, UnsupportedSurface | None]:
        if not isinstance(raw, Mapping):
            return None, None

        surface_kind = _first(raw, ("unsupported_surface", "surface"))
        if surface_kind and _UNSUPPORTED_SURFACE_RE.search(str(surface_kind)):
            return None, UnsupportedSurface(
                kind=_clean(surface_kind),
                detail=_clean(_first(raw, ("detail", "reason"), ""), 120),
            )

        role = _clean(_first(raw, ("role", "control_type", "type"), "generic")).lower()
        label = _clean(_first(raw, ("label", "name", "text", "aria_label"), ""), 60)
        input_type = _clean(_first(raw, ("input_type", "inputType"), "")).lower()

        # Privacy / safety filters run BEFORE numbering: dropped elements never
        # get a target_id and are never shown to the model.
        if input_type in _SENSITIVE_INPUT_TYPES:
            return None, None
        if _SENSITIVE_ROLE_RE.search(f"{role} {label} {input_type}"):
            return None, None
        if str(_first(raw, ("sensitive", "is_sensitive"), "")).lower() in {"1", "true", "yes"}:
            return None, None
        if not _is_interactive(role, raw):
            return None, None
        if not _is_visible(raw):
            return None, None

        backend_id = _clean(_first(raw, ("backend_id", "node_id", "id"), ""))
        identity = self._identity(backend_id)
        operations = _ROLE_OPERATIONS.get(role, _DEFAULT_OPERATIONS)
        # target_id is observation-local: renumbered from the per-build sequence.
        target_id = f"e{sequence:03d}"
        return WebTarget(
            target_id=target_id,
            role=role,
            label=label,
            enabled=bool(_first(raw, ("enabled", "is_enabled"), True)),
            visible=True,
            editable=WebOperation.TYPE_TEXT in operations,
            checked=_optional_bool(raw, ("checked", "aria_checked")),
            selected=_optional_bool(raw, ("selected", "aria_selected")),
            input_type=input_type,
            destination_origin=_clean(_first(raw, ("destination_origin", "href_origin"), "")),
            operations=operations,
            metadata={"node": identity.node, "backend_id": identity.backend_id},
        ), None


def _is_interactive(role: str, raw: Mapping[str, Any]) -> bool:
    if role in _ROLE_OPERATIONS:
        return True
    for key in ("clickable", "is_clickable", "interactive", "focusable"):
        if str(_first(raw, (key,), "")).lower() in {"1", "true", "yes"}:
            return True
    return False


def _is_visible(raw: Mapping[str, Any]) -> bool:
    visible = _first(raw, ("visible", "is_visible", "on_screen"), True)
    if isinstance(visible, str):
        return visible.strip().lower() not in {"0", "false", "no", "off"}
    return bool(visible)


def _optional_bool(raw: Mapping[str, Any], names: Sequence[str]) -> bool | None:
    value = _first(raw, names, None)
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


__all__ = [
    "NodeIdentity",
    "ObservationBuilder",
    "UnsupportedSurface",
]
