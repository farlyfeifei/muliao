"""Core, provider-neutral data models for the Ghost swarm protocol.

The models intentionally contain only JSON-compatible protocol data.  They do
not depend on a model SDK, web framework, or database implementation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import StrEnum
import json
from pathlib import Path
from typing import Any, ClassVar, Mapping, TypeAlias


JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

GCTX_PROTOCOL = "GCTX/0.1"


class FactState(StrEnum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    VERIFIED = "verified"
    CONFLICTED = "conflicted"
    INVALIDATED = "invalidated"


class AckStatus(StrEnum):
    ACCEPTED = "accepted"
    NEED_CONTEXT = "need_context"
    STALE = "stale"
    FORBIDDEN = "forbidden"
    INCOMPATIBLE = "incompatible"
    CONFLICTED = "conflicted"


FACT_STATES = frozenset(state.value for state in FactState)
ACK_STATUSES = frozenset(status.value for status in AckStatus)


def utc_now() -> str:
    """Return a stable UTC timestamp suitable for protocol JSON."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def json_value(value: Any) -> JSONValue:
    """Convert supported protocol values to JSON-compatible builtins.

    This is deliberately strict about mapping keys so hashes cannot depend on
    an implementation-specific conversion of non-string keys.
    """

    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return json_value(asdict(value))
    if isinstance(value, Mapping):
        converted: dict[str, JSONValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"JSON object keys must be strings, got {type(key)!r}")
            converted[key] = json_value(item)
        return converted
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"value is not JSON serializable: {type(value)!r}")


def canonical_json(value: Any) -> str:
    """Serialize protocol data deterministically for hashing and storage."""

    return json.dumps(
        json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class JsonModel:
    """Mixin shared by the protocol dataclasses."""

    def to_dict(self) -> dict[str, JSONValue]:
        value = json_value(self)
        if not isinstance(value, dict):  # pragma: no cover - dataclasses are objects
            raise TypeError("model did not serialize to a JSON object")
        return value

    def to_json(self) -> str:
        return canonical_json(self)


@dataclass(frozen=True, slots=True)
class TaskContract(JsonModel):
    """An immutable revision of the task's control-plane contract."""

    task_id: str
    contract_rev: int
    goal: str
    scope: dict[str, JSONValue] = field(
        default_factory=lambda: {"included": [], "excluded": []}
    )
    required_constraints: tuple[dict[str, JSONValue], ...] = ()
    acceptance_tests: tuple[JSONValue, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    permission_snapshot: JSONValue = None
    risk_gate: str = "review"
    budgets: dict[str, JSONValue] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id is required")
        if self.contract_rev < 1:
            raise ValueError("contract_rev must be at least 1")
        if not self.goal:
            raise ValueError("goal is required")

    @property
    def required_constraint_ids(self) -> tuple[str, ...]:
        ids: list[str] = []
        for item in self.required_constraints:
            constraint_id = item.get("id") if isinstance(item, Mapping) else None
            if isinstance(constraint_id, str) and constraint_id:
                ids.append(constraint_id)
        return tuple(ids)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskContract":
        data = dict(value)
        data["required_constraints"] = tuple(data.get("required_constraints", ()))
        data["acceptance_tests"] = tuple(data.get("acceptance_tests", ()))
        data["allowed_tools"] = tuple(data.get("allowed_tools", ()))
        return cls(**data)


@dataclass(frozen=True, slots=True)
class BeeSpec(JsonModel):
    """A versionable runtime capability/role description for one bee."""

    bee_id: str
    role_prompt: str
    allowed_tools: tuple[str, ...] = ()
    model: str = ""
    input_schema: JSONValue = None
    output_schema: JSONValue = None
    budget: dict[str, JSONValue] = field(default_factory=dict)
    version: str = "1"

    def __post_init__(self) -> None:
        if not self.bee_id:
            raise ValueError("bee_id is required")
        if not self.role_prompt:
            raise ValueError("role_prompt is required")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BeeSpec":
        data = dict(value)
        data["allowed_tools"] = tuple(data.get("allowed_tools", ()))
        return cls(**data)


@dataclass(frozen=True, slots=True)
class SwarmRecipe(JsonModel):
    """A finite, inspectable swarm topology recipe."""

    recipe_id: str
    stages: tuple[tuple[str, ...], ...]
    description: str = ""
    requires_confirmation: bool = False
    max_parallel: int = 1

    def __post_init__(self) -> None:
        if not self.recipe_id:
            raise ValueError("recipe_id is required")
        if self.max_parallel < 1:
            raise ValueError("max_parallel must be at least 1")
        if any(not stage for stage in self.stages):
            raise ValueError("recipe stages cannot be empty")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SwarmRecipe":
        data = dict(value)
        data["stages"] = tuple(tuple(stage) for stage in data.get("stages", ()))
        return cls(**data)


# Backward-friendly name used in the construction plan.
Recipe = SwarmRecipe


@dataclass(frozen=True, slots=True)
class SwarmEvent(JsonModel):
    """An append-only event emitted by a swarm run."""

    event_id: str
    seq: int
    run_id: str
    task_id: str
    type: str
    payload: dict[str, JSONValue] = field(default_factory=dict)
    ts: float = 0.0

    def __post_init__(self) -> None:
        if not self.event_id or not self.run_id or not self.task_id or not self.type:
            raise ValueError("event_id, run_id, task_id, and type are required")
        if self.seq < 0:
            raise ValueError("seq cannot be negative")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SwarmEvent":
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class FactRecord(JsonModel):
    fact_id: str
    task_id: str
    state: str
    value: JSONValue
    source_ref: str | None = None
    source_sha256: str | None = None
    depends_on: tuple[str, ...] = ()
    expires_at: str | None = None
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.fact_id or not self.task_id:
            raise ValueError("fact_id and task_id are required")
        if self.state not in FACT_STATES:
            raise ValueError(f"unsupported fact state: {self.state}")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FactRecord":
        data = dict(value)
        data["depends_on"] = tuple(data.get("depends_on", ()))
        return cls(**data)


@dataclass(frozen=True, slots=True)
class ArtifactRecord(JsonModel):
    artifact_id: str
    task_id: str
    uri: str
    mime: str
    sha256: str
    status: str = "current"
    depends_on: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.artifact_id or not self.task_id or not self.uri:
            raise ValueError("artifact_id, task_id, and uri are required")
        if not self.sha256:
            raise ValueError("artifact sha256 is required")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactRecord":
        data = dict(value)
        data["depends_on"] = tuple(data.get("depends_on", ()))
        return cls(**data)


@dataclass(frozen=True, slots=True)
class WorkCapsule(JsonModel):
    """GCTX/0.1 handoff package exchanged between independent bees."""

    message_id: str
    capsule_id: str
    task_id: str
    contract_rev: int
    from_bee: str
    to_bee: str
    required_constraints: tuple[str, ...] = ()
    facts: tuple[dict[str, JSONValue], ...] = ()
    artifacts: tuple[dict[str, JSONValue], ...] = ()
    open_questions: tuple[JSONValue, ...] = ()
    side_effects: tuple[JSONValue, ...] = ()
    loss_manifest: tuple[JSONValue, ...] = ()
    depends_on: tuple[str, ...] = ()
    sender_claims: dict[str, JSONValue] = field(default_factory=dict)
    permission_snapshot: JSONValue = None
    required_permissions: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now)
    expires_at: str | None = None
    protocol: str = GCTX_PROTOCOL

    SUPPORTED_PROTOCOL: ClassVar[str] = GCTX_PROTOCOL

    def __post_init__(self) -> None:
        required = (
            self.message_id,
            self.capsule_id,
            self.task_id,
            self.from_bee,
            self.to_bee,
        )
        if any(not value for value in required):
            raise ValueError(
                "message_id, capsule_id, task_id, from_bee, and to_bee are required"
            )
        if self.contract_rev < 1:
            raise ValueError("contract_rev must be at least 1")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkCapsule":
        data = dict(value)
        # Accept the long-form names from the full architecture document too.
        if "schema_version" in data and "protocol" not in data:
            schema = str(data.pop("schema_version"))
            data["protocol"] = (
                GCTX_PROTOCOL if schema.lower() == "gctx/0.1" else schema
            )
        if "sender" in data and "from_bee" not in data:
            data["from_bee"] = data.pop("sender")
        if "recipient" in data and "to_bee" not in data:
            data["to_bee"] = data.pop("recipient")
        if "required_constraint_ids" in data and "required_constraints" not in data:
            data["required_constraints"] = data.pop("required_constraint_ids")
        tuple_fields = (
            "required_constraints",
            "facts",
            "artifacts",
            "open_questions",
            "side_effects",
            "loss_manifest",
            "depends_on",
            "required_permissions",
        )
        for name in tuple_fields:
            data[name] = tuple(data.get(name, ()))
        return cls(**data)


@dataclass(frozen=True, slots=True)
class CapsuleAck(JsonModel):
    """Receiver-generated acknowledgement; never a sender assertion."""

    message_id: str
    capsule_id: str
    status: str
    reasons: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    checked_sha256: str = ""
    duplicate: bool = False
    checked_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.status not in ACK_STATUSES:
            raise ValueError(f"unsupported capsule acknowledgement: {self.status}")

    @property
    def accepted(self) -> bool:
        return self.status == AckStatus.ACCEPTED.value
