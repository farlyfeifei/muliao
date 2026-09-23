"""Read-only UI perception for the M3 GOAL loop.

The module deliberately exposes text and coordinates only.  It never captures or
returns screenshots, and all platform/OCR dependencies are injectable so tests
can run without Windows automation packages.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import importlib
import re
from typing import Any, Callable, Iterable, Mapping, Protocol, runtime_checkable

MAX_ELEMENTS = 100
MAX_TEXT_CHARS = 60
MAX_STATE_CHARS = 24_000
MIN_UIA_ELEMENTS = 1

_STATE_TRUNCATED_MARKER = "\n...[state truncated]"
_INTERACTIVE_CONTROL_TYPES = frozenset(
    {
        "button",
        "buttoncontrol",
        "checkbox",
        "checkboxcontrol",
        "combobox",
        "comboboxcontrol",
        "dataitem",
        "dataitemcontrol",
        "edit",
        "editcontrol",
        "hyperlink",
        "hyperlinkcontrol",
        "listitem",
        "listitemcontrol",
        "menuitem",
        "menuitemcontrol",
        "radiobutton",
        "radiobuttoncontrol",
        "splitbutton",
        "splitbuttoncontrol",
        "tabitem",
        "tabitemcontrol",
        "treeitem",
        "treeitemcontrol",
    }
)
_SENSITIVE_FLAG_KEYS = frozenset(
    {
        "browser_history",
        "browsing_history",
        "is_password",
        "masked",
        "password",
        "region_sensitive",
        "sensitive",
        "sensitive_region",
    }
)
_SENSITIVE_CONTEXT_TERMS = (
    "autocomplete",
    "browser history",
    "browsing history",
    "credit card",
    "cvv",
    "one-time code",
    "omnibox",
    "otp",
    "passcode",
    "password",
    "passwd",
    "suggestion popup",
    "urlbar",
    "addressbar",
    "信用卡",
    "验证码",
    "密码",
    "浏览历史",
    "浏览记录",
)


def _clean_text(value: Any, *, limit: int | None = MAX_TEXT_CHARS) -> str:
    text = " ".join(str(value or "").split())
    if limit is not None:
        return text[:limit]
    return text


def _normalise_bounds(value: Any) -> tuple[int, int, int, int] | None:
    if value is None:
        return None
    try:
        if isinstance(value, Mapping):
            if all(key in value for key in ("left", "top", "right", "bottom")):
                raw = (value["left"], value["top"], value["right"], value["bottom"])
            elif all(key in value for key in ("x", "y", "width", "height")):
                x, y = value["x"], value["y"]
                raw = (x, y, float(x) + float(value["width"]), float(y) + float(value["height"]))
            else:
                return None
        elif isinstance(value, (tuple, list)) and len(value) >= 4:
            raw = value[:4]
        elif all(hasattr(value, key) for key in ("left", "top", "right", "bottom")):
            raw = (value.left, value.top, value.right, value.bottom)
        elif all(hasattr(value, key) for key in ("x", "y", "width", "height")):
            raw = (value.x, value.y, float(value.x) + float(value.width), float(value.y) + float(value.height))
        else:
            return None
        left, top, right, bottom = (int(round(float(part))) for part in raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if right < left:
        left, right = right, left
    if bottom < top:
        top, bottom = bottom, top
    return left, top, right, bottom


def _mapping_flag(metadata: Mapping[str, Any], key: str) -> bool:
    value = metadata.get(key)
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "sensitive", "password", "history"}
    return bool(value)


def _contains_sensitive_context(value: str) -> bool:
    lowered = value.casefold().replace("_", " ").replace("-", " ")
    if any(term in lowered for term in _SENSITIVE_CONTEXT_TERMS):
        return True
    # PIN must be a token; this avoids false positives such as "shipping".
    return re.search(r"(?:^|\W)pin(?:$|\W)", lowered) is not None


@dataclass(frozen=True)
class UIElement:
    """An immutable, read-only candidate exposed to the goal chooser.

    ``bounds`` uses ``(left, top, right, bottom)`` screen coordinates.  Adapters
    may mark password or otherwise sensitive regions; marked elements are
    removed before a :class:`Snapshot` is produced.
    """

    id: str = ""
    text: str = ""
    role: str = "control"
    bounds: tuple[int, int, int, int] | None = None
    source: str = "uia"
    enabled: bool = True
    password: bool = False
    sensitive: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _clean_text(self.id, limit=None))
        object.__setattr__(self, "text", _clean_text(self.text))
        object.__setattr__(self, "role", _clean_text(self.role, limit=None) or "control")
        object.__setattr__(self, "source", _clean_text(self.source, limit=None) or "uia")
        object.__setattr__(self, "bounds", _normalise_bounds(self.bounds))
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(self, "password", bool(self.password))
        object.__setattr__(self, "sensitive", bool(self.sensitive))
        metadata = self.metadata if isinstance(self.metadata, Mapping) else {}
        object.__setattr__(self, "metadata", dict(metadata))

    @property
    def center(self) -> tuple[int, int] | None:
        if self.bounds is None:
            return None
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2


def _is_safe_element(element: UIElement) -> bool:
    if not element.enabled or not element.text or element.password or element.sensitive:
        return False
    if any(_mapping_flag(element.metadata, key) for key in _SENSITIVE_FLAG_KEYS):
        return False
    descriptor_parts = [element.role, element.text]
    for key in ("automation_id", "class_name", "category", "context", "kind"):
        value = element.metadata.get(key)
        if value:
            descriptor_parts.append(str(value))
    return not _contains_sensitive_context(" ".join(descriptor_parts))


def _render_state(source: str, elements: tuple[UIElement, ...], fallback_used: bool) -> tuple[str, bool]:
    lines = [
        f"source={source}",
        f"fallback={'ocr' if fallback_used else 'none'}",
        f"candidates={len(elements)}",
    ]
    for element in elements:
        line = f"{element.id} | role={element.role} | text={element.text}"
        if element.bounds is not None:
            left, top, right, bottom = element.bounds
            center_x, center_y = element.center or (left, top)
            line += (
                f" | bounds=({left},{top},{right},{bottom})"
                f" | center=({center_x},{center_y})"
            )
        lines.append(line)
    state = "\n".join(lines)
    if len(state) <= MAX_STATE_CHARS:
        return state, False
    keep = MAX_STATE_CHARS - len(_STATE_TRUNCATED_MARKER)
    return state[:keep] + _STATE_TRUNCATED_MARKER, True


@dataclass(frozen=True)
class Snapshot:
    """A bounded, privacy-filtered set of current interaction candidates."""

    elements: tuple[UIElement, ...] = ()
    source: str = "none"
    fallback_used: bool = False
    state: str = field(init=False)
    truncated: bool = field(init=False)

    def __post_init__(self) -> None:
        safe: list[UIElement] = []
        for item in tuple(self.elements):
            element = item if isinstance(item, UIElement) else _coerce_element(item, source=self.source)
            if element is None or not _is_safe_element(element):
                continue
            safe.append(replace(element, id=f"e{len(safe) + 1:03d}"))
            if len(safe) >= MAX_ELEMENTS:
                break
        source = _clean_text(self.source, limit=None) or "none"
        elements = tuple(safe)
        state, truncated = _render_state(source, elements, bool(self.fallback_used))
        object.__setattr__(self, "elements", elements)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "fallback_used", bool(self.fallback_used))
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "truncated", truncated)

    def candidate(self, element_id: str) -> UIElement | None:
        wanted = str(element_id or "").strip()
        return next((element for element in self.elements if element.id == wanted), None)

    @property
    def signature(self) -> tuple[tuple[str, str, tuple[int, int, int, int] | None, str], ...]:
        """Stable comparison data used to detect whether an action changed the UI."""

        return tuple((item.text, item.role, item.bounds, item.source) for item in self.elements)


def _first_value(item: Any, names: tuple[str, ...], default: Any = None) -> Any:
    if isinstance(item, Mapping):
        for name in names:
            if name in item:
                return item[name]
        return default
    for name in names:
        if hasattr(item, name):
            try:
                value = getattr(item, name)
                return value() if callable(value) else value
            except Exception:
                continue
    return default


def _coerce_element(item: Any, *, source: str) -> UIElement | None:
    if isinstance(item, UIElement):
        return replace(item, source=source)
    if item is None:
        return None
    metadata = _first_value(item, ("metadata",), {})
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    if isinstance(item, Mapping):
        for key in _SENSITIVE_FLAG_KEYS | {"automation_id", "category", "class_name", "context", "kind"}:
            if key in item:
                metadata[key] = item[key]
    bounds = _first_value(item, ("bounds", "bbox", "box", "rect", "rectangle"))
    if bounds is None and isinstance(item, Mapping):
        bounds = item
    return UIElement(
        text=_first_value(item, ("text", "name", "label"), ""),
        role=_first_value(item, ("role", "control_type", "type"), "control"),
        bounds=bounds,
        source=source,
        enabled=_first_value(item, ("enabled", "is_enabled"), True),
        password=_first_value(item, ("password", "is_password"), False),
        sensitive=_first_value(item, ("sensitive", "is_sensitive", "sensitive_region"), False),
        metadata=metadata,
    )


@runtime_checkable
class UIAAdapter(Protocol):
    def read_elements(self) -> Iterable[UIElement | Mapping[str, Any]]: ...


@runtime_checkable
class OCRAdapter(Protocol):
    def read_elements(self) -> Iterable[UIElement | Mapping[str, Any]]: ...


class NullOCRAdapter:
    """Default OCR adapter: dependency-free and intentionally returns nothing."""

    def read_elements(self) -> tuple[UIElement, ...]:
        return ()


class WindowsUIAAdapter:
    """Read interactive Windows UI Automation controls.

    ``uiautomation`` is imported only when :meth:`read_elements` is called.  No
    value pattern is read, so edit contents (including passwords) are never
    copied into state.
    """

    def __init__(self, module_loader: Callable[[], Any] | None = None) -> None:
        self._module_loader = module_loader or (lambda: importlib.import_module("uiautomation"))

    @staticmethod
    def _attribute(control: Any, name: str, default: Any = None) -> Any:
        try:
            value = getattr(control, name, default)
            return value() if callable(value) else value
        except Exception:
            return default

    def read_elements(self) -> tuple[UIElement, ...]:
        try:
            module = self._module_loader()
            root = module.GetRootControl()
        except Exception:
            return ()
        if root is None:
            return ()

        found: list[UIElement] = []
        stack: list[tuple[Any, bool]] = [(root, False)]
        visited = 0
        while stack and len(found) < MAX_ELEMENTS:
            control, inherited_sensitive = stack.pop()
            visited += 1
            if visited > 10_000:
                break

            role = _clean_text(self._attribute(control, "ControlTypeName", "control"), limit=None)
            name = _clean_text(self._attribute(control, "Name", ""))
            automation_id = _clean_text(self._attribute(control, "AutomationId", ""), limit=None)
            class_name = _clean_text(self._attribute(control, "ClassName", ""), limit=None)
            descriptor = " ".join((role, name, automation_id, class_name))
            is_password = bool(self._attribute(control, "IsPassword", False))
            sensitive_context = inherited_sensitive or is_password or _contains_sensitive_context(descriptor)

            children = self._attribute(control, "GetChildren", ()) or ()
            try:
                child_items = list(children)
            except TypeError:
                child_items = []
            for child in reversed(child_items):
                stack.append((child, sensitive_context))

            normalised_role = role.casefold().replace(" ", "")
            pattern_available = any(
                bool(self._attribute(control, name, False))
                for name in (
                    "IsInvokePatternAvailable",
                    "IsSelectionItemPatternAvailable",
                    "IsTogglePatternAvailable",
                    "IsValuePatternAvailable",
                )
            )
            if normalised_role not in _INTERACTIVE_CONTROL_TYPES and not pattern_available:
                continue
            if sensitive_context or not name:
                continue
            if not bool(self._attribute(control, "IsEnabled", True)):
                continue
            if bool(self._attribute(control, "IsOffscreen", False)):
                continue

            bounds = _normalise_bounds(self._attribute(control, "BoundingRectangle", None))
            found.append(
                UIElement(
                    text=name,
                    role=role or "control",
                    bounds=bounds,
                    source="uia",
                    metadata={"automation_id": automation_id, "class_name": class_name},
                )
            )
        return tuple(found)


def _read_adapter(adapter: Any) -> Any:
    for name in ("read_elements", "scan", "extract", "capture"):
        method = getattr(adapter, name, None)
        if callable(method):
            return method()
    if callable(adapter):
        return adapter()
    raise TypeError("perception adapter must be callable or expose read_elements()")


def _normalise_adapter_output(raw: Any, *, source: str, require_bounds: bool) -> tuple[UIElement, ...]:
    if isinstance(raw, Snapshot):
        values: Iterable[Any] = raw.elements
    elif isinstance(raw, Mapping):
        nested = raw.get("elements")
        if nested is None:
            nested = raw.get("candidates")
        values = nested if nested is not None else (raw,)
    elif isinstance(raw, UIElement):
        values = (raw,)
    elif raw is None:
        values = ()
    else:
        try:
            values = iter(raw)
        except TypeError:
            values = (raw,)

    normalised: list[UIElement] = []
    for item in values:
        element = _coerce_element(item, source=source)
        if element is None or (require_bounds and element.bounds is None):
            continue
        normalised.append(element)
        if len(normalised) >= MAX_ELEMENTS:
            break
    # Snapshot applies all privacy filters and canonical numbering.
    return Snapshot(tuple(normalised), source=source).elements


class ScreenPerception:
    """Capture a bounded candidate snapshot, preferring UIA over OCR."""

    def __init__(
        self,
        uia_adapter: UIAAdapter | Callable[[], Iterable[Any]] | None = None,
        ocr_adapter: OCRAdapter | Callable[[], Iterable[Any]] | None = None,
        *,
        min_uia_elements: int = MIN_UIA_ELEMENTS,
    ) -> None:
        if min_uia_elements < 1:
            raise ValueError("min_uia_elements must be at least 1")
        self.uia_adapter = uia_adapter or WindowsUIAAdapter()
        self.ocr_adapter = ocr_adapter or NullOCRAdapter()
        self.min_uia_elements = min(int(min_uia_elements), MAX_ELEMENTS)

    def capture(self) -> Snapshot:
        try:
            uia_raw = _read_adapter(self.uia_adapter)
            uia_elements = _normalise_adapter_output(uia_raw, source="uia", require_bounds=False)
        except Exception:
            uia_elements = ()

        if len(uia_elements) >= self.min_uia_elements:
            return Snapshot(uia_elements, source="uia")

        try:
            ocr_raw = _read_adapter(self.ocr_adapter)
            ocr_elements = _normalise_adapter_output(ocr_raw, source="ocr", require_bounds=True)
        except Exception:
            ocr_elements = ()

        if ocr_elements:
            return Snapshot(ocr_elements, source="ocr", fallback_used=True)
        if uia_elements:
            # OCR did not improve the sparse UIA result; retain the safe UIA data.
            return Snapshot(uia_elements, source="uia", fallback_used=True)
        return Snapshot((), source="none", fallback_used=True)


# Short, discoverable name for callers that do not need the platform detail.
Perception = ScreenPerception

__all__ = [
    "MAX_ELEMENTS",
    "MAX_STATE_CHARS",
    "MAX_TEXT_CHARS",
    "MIN_UIA_ELEMENTS",
    "NullOCRAdapter",
    "OCRAdapter",
    "Perception",
    "ScreenPerception",
    "Snapshot",
    "UIAAdapter",
    "UIElement",
    "WindowsUIAAdapter",
]
