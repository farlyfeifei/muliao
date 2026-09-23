"""Construction, hashing, verification, and acknowledgement for GCTX/0.1.

Verification is receiver-side and conservative: sender claims are never used as
proof, unknown or unreadable evidence cannot silently pass, and a stale or
conflicted dependency cannot be promoted into downstream work.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

from swarm_models import (
    FACT_STATES,
    AckStatus,
    CapsuleAck,
    GCTX_PROTOCOL,
    JSONValue,
    TaskContract,
    WorkCapsule,
    canonical_json,
    json_value,
)
from swarm_store import DuplicateMessageError, SwarmStore


ArtifactLoader = Callable[[Mapping[str, Any]], bytes]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_value(value: bytes | str | Path) -> str:
    """Hash bytes, UTF-8 text, or the contents of a filesystem path."""

    if isinstance(value, Path):
        return sha256_bytes(value.read_bytes())
    if isinstance(value, bytes):
        return sha256_bytes(value)
    return sha256_bytes(value.encode("utf-8"))


def json_sha256(value: Any) -> str:
    return sha256_value(canonical_json(value))


def capsule_sha256(capsule: WorkCapsule | Mapping[str, Any]) -> str:
    return json_sha256(capsule)


def contract_sha256(contract: TaskContract | Mapping[str, Any]) -> str:
    return json_sha256(contract)


def build_capsule(
    *,
    task_id: str,
    contract_rev: int,
    from_bee: str,
    to_bee: str,
    required_constraints: Sequence[str] = (),
    facts: Sequence[Mapping[str, JSONValue]] = (),
    artifacts: Sequence[Mapping[str, JSONValue]] = (),
    open_questions: Sequence[JSONValue] = (),
    side_effects: Sequence[JSONValue] = (),
    loss_manifest: Sequence[JSONValue] = (),
    depends_on: Sequence[str] = (),
    sender_claims: Mapping[str, JSONValue] | None = None,
    payload: JSONValue = None,
    permission_snapshot: JSONValue = None,
    required_permissions: Sequence[str] = (),
    created_at: str | None = None,
    expires_at: str | None = None,
    message_id: str | None = None,
    capsule_id: str | None = None,
) -> WorkCapsule:
    """Construct a JSON-serializable WorkCapsule with generated stable IDs."""

    return WorkCapsule(
        message_id=message_id or f"msg_{uuid4().hex}",
        capsule_id=capsule_id or f"cap_{uuid4().hex}",
        task_id=task_id,
        contract_rev=contract_rev,
        from_bee=from_bee,
        to_bee=to_bee,
        required_constraints=tuple(required_constraints),
        facts=tuple(dict(item) for item in facts),
        artifacts=tuple(dict(item) for item in artifacts),
        open_questions=tuple(open_questions),
        side_effects=tuple(side_effects),
        loss_manifest=tuple(loss_manifest),
        depends_on=tuple(depends_on),
        sender_claims=dict(sender_claims or {}),
        payload=payload,
        permission_snapshot=permission_snapshot,
        required_permissions=tuple(required_permissions),
        **({"created_at": created_at} if created_at is not None else {}),
        expires_at=expires_at,
    )


def _handoff_items(
    result: Mapping[str, Any], name: str, *, mappings: bool = False
) -> tuple[Any, ...]:
    value = result.get(name, ())
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"result {name} must be a sequence")
    if mappings:
        if any(not isinstance(item, Mapping) for item in value):
            raise TypeError(f"result {name} entries must be mappings")
        return tuple(dict(item) for item in value)
    return tuple(value)


def _constraint_identifier(constraint: Any) -> str:
    if isinstance(constraint, str) and constraint:
        return constraint
    if isinstance(constraint, Mapping):
        for name in ("id", "text", "string"):
            value = constraint.get(name)
            if isinstance(value, str) and value:
                return value
    return canonical_json(constraint)


def _handoff_created_at(created_at: Any, identity: Mapping[str, Any]) -> str:
    if isinstance(created_at, str) and created_at:
        return created_at
    digest = hashlib.sha256(canonical_json(identity).encode("utf-8")).digest()
    seconds = int.from_bytes(digest[:4], "big") % (200 * 365 * 24 * 60 * 60)
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def build_handoff_capsule(
    *,
    run_id: str,
    stage_id: str,
    contract: TaskContract | Mapping[str, Any],
    from_bee: str,
    to_bee: str,
    result: Any,
    attempt: int = 1,
) -> WorkCapsule:
    """Build a deterministic, lossless capsule for one stage handoff."""

    if not run_id or not stage_id or not from_bee or not to_bee:
        raise ValueError("run_id, stage_id, from_bee, and to_bee are required")
    if attempt < 1:
        raise ValueError("attempt must be at least 1")

    contract_created_at = (
        contract.created_at
        if isinstance(contract, TaskContract)
        else contract.get("created_at")
    )
    task_contract = (
        contract if isinstance(contract, TaskContract) else TaskContract.from_dict(contract)
    )
    safe_result = json_value(result)
    structured = safe_result if isinstance(safe_result, Mapping) else {}
    identity = {
        "run_id": run_id,
        "task_id": task_contract.task_id,
        "contract_rev": task_contract.contract_rev,
        "stage_id": stage_id,
        "from_bee": from_bee,
        "to_bee": to_bee,
        "attempt": attempt,
    }
    digest = json_sha256(identity)[:20]
    required_constraints = tuple(
        _constraint_identifier(item) for item in task_contract.required_constraints
    )

    return build_capsule(
        task_id=task_contract.task_id,
        contract_rev=task_contract.contract_rev,
        from_bee=from_bee,
        to_bee=to_bee,
        required_constraints=required_constraints,
        facts=_handoff_items(structured, "facts", mappings=True),
        artifacts=_handoff_items(structured, "artifacts", mappings=True),
        open_questions=_handoff_items(structured, "open_questions"),
        side_effects=_handoff_items(structured, "side_effects"),
        loss_manifest=_handoff_items(structured, "loss_manifest"),
        depends_on=_handoff_items(structured, "depends_on"),
        sender_claims={"required_payload_complete": True},
        payload=safe_result,
        permission_snapshot=task_contract.permission_snapshot,
        required_permissions=task_contract.allowed_tools,
        created_at=_handoff_created_at(
            contract_created_at,
            identity,
        ),
        message_id=f"msg_{digest}",
        capsule_id=f"cap_{digest}",
    )


def _parse_timestamp(value: str, *, require_timezone: bool = False) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be non-empty text")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        if require_timezone:
            raise ValueError("timestamp must include a timezone")
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _local_artifact_loader(artifact: Mapping[str, Any]) -> bytes:
    uri = artifact.get("uri")
    if not isinstance(uri, str) or not uri:
        raise FileNotFoundError("artifact URI is missing")

    if uri.startswith("file://"):
        parsed = urlparse(uri)
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            # UNC paths retain the server component.
            path = Path(f"//{parsed.netloc}{unquote(parsed.path)}")
        else:
            raw_path = unquote(parsed.path)
            if len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
                raw_path = raw_path[1:]
            path = Path(raw_path)
    elif "://" in uri:
        raise FileNotFoundError(f"no reader configured for artifact URI: {uri}")
    else:
        path = Path(uri)
    return path.read_bytes()


def _row_artifact(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "artifact_id": row["artifact_id"],
        "uri": row["uri"],
        "mime": row["mime"],
        "sha256": row["sha256"],
    }


def _source_artifact_id(source_ref: str) -> str | None:
    if not source_ref.startswith("artifact://"):
        return None
    identifier = source_ref[len("artifact://") :].split("#", 1)[0]
    return identifier or None


def _same_json(left: Any, right: Any) -> bool:
    return canonical_json(left) == canonical_json(right)


def _ack(
    capsule: WorkCapsule,
    status: AckStatus,
    *,
    reasons: Sequence[str] = (),
    missing: Sequence[str] = (),
    duplicate: bool = False,
) -> CapsuleAck:
    return CapsuleAck(
        message_id=capsule.message_id,
        capsule_id=capsule.capsule_id,
        status=status.value,
        reasons=tuple(dict.fromkeys(reasons)),
        missing=tuple(dict.fromkeys(missing)),
        checked_sha256=capsule_sha256(capsule),
        duplicate=duplicate,
    )


def verify_capsule(
    capsule: WorkCapsule,
    store: SwarmStore,
    *,
    current_permission_snapshot: JSONValue,
    receiver_permissions: Collection[str] = (),
    receiver_bee: str | None = None,
    artifact_loader: ArtifactLoader | None = None,
    now: datetime | None = None,
) -> CapsuleAck:
    """Independently verify a capsule and return one of the six GCTX ACKs.

    The receiver checks protocol compatibility, the current contract revision,
    every required constraint, permission state, evidence references, actual
    artifact bytes, fact states, and dependency states.  The sender's
    ``required_payload_complete`` assertion is intentionally ignored.
    """

    if capsule.protocol != GCTX_PROTOCOL:
        return _ack(
            capsule,
            AckStatus.INCOMPATIBLE,
            reasons=(f"unsupported protocol: {capsule.protocol}",),
        )

    if receiver_bee is not None and receiver_bee != capsule.to_bee:
        return _ack(
            capsule,
            AckStatus.FORBIDDEN,
            reasons=(
                f"receiver {receiver_bee!r} does not match capsule recipient "
                f"{capsule.to_bee!r}",
            ),
        )

    current_contract = store.current_contract(capsule.task_id)
    if current_contract is None:
        return _ack(
            capsule,
            AckStatus.STALE,
            reasons=("task has no current contract",),
        )
    if capsule.contract_rev != current_contract.contract_rev:
        return _ack(
            capsule,
            AckStatus.STALE,
            reasons=(
                f"contract_rev {capsule.contract_rev} is not current "
                f"({current_contract.contract_rev})",
            ),
        )

    check_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if capsule.expires_at is not None:
        try:
            expires_at = _parse_timestamp(capsule.expires_at)
        except (TypeError, ValueError):
            return _ack(
                capsule,
                AckStatus.INCOMPATIBLE,
                reasons=("expires_at is not a valid ISO-8601 timestamp",),
            )
        if expires_at <= check_time:
            return _ack(
                capsule,
                AckStatus.STALE,
                reasons=("capsule has expired",),
            )

    if not _same_json(capsule.permission_snapshot, current_contract.permission_snapshot):
        return _ack(
            capsule,
            AckStatus.FORBIDDEN,
            reasons=("capsule permission snapshot differs from contract",),
        )
    if not _same_json(current_permission_snapshot, current_contract.permission_snapshot):
        return _ack(
            capsule,
            AckStatus.FORBIDDEN,
            reasons=("receiver permission snapshot is no longer current",),
        )

    available_permissions = set(receiver_permissions)
    missing_permissions = sorted(
        set(capsule.required_permissions) - available_permissions
    )
    if missing_permissions:
        return _ack(
            capsule,
            AckStatus.FORBIDDEN,
            reasons=("receiver lacks required permissions",),
            missing=tuple(f"permission:{item}" for item in missing_permissions),
        )

    contract_constraints = {
        _constraint_identifier(item) for item in current_contract.required_constraints
    }
    capsule_constraints = set(capsule.required_constraints)
    missing_constraints = sorted(contract_constraints - capsule_constraints)
    if missing_constraints:
        return _ack(
            capsule,
            AckStatus.NEED_CONTEXT,
            reasons=("required constraints are missing",),
            missing=tuple(f"constraint:{item}" for item in missing_constraints),
        )

    artifact_loader = artifact_loader or _local_artifact_loader
    artifacts: dict[str, Mapping[str, Any]] = {}
    for artifact in capsule.artifacts:
        artifact_id = artifact.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            return _ack(
                capsule,
                AckStatus.NEED_CONTEXT,
                reasons=("artifact is missing artifact_id",),
                missing=("artifact_id",),
            )
        artifact_status = artifact.get("status", "current")
        if artifact_status == "stale":
            return _ack(
                capsule,
                AckStatus.STALE,
                reasons=(f"artifact {artifact_id} is stale",),
            )
        if artifact_status != "current":
            return _ack(
                capsule,
                AckStatus.INCOMPATIBLE,
                reasons=(
                    f"artifact {artifact_id} has unsupported status: "
                    f"{artifact_status}",
                ),
            )
        if artifact_id in artifacts and not _same_json(artifacts[artifact_id], artifact):
            return _ack(
                capsule,
                AckStatus.CONFLICTED,
                reasons=(f"artifact {artifact_id} has conflicting manifests",),
            )
        stored_artifact = store.get_artifact(artifact_id)
        if stored_artifact is not None and stored_artifact["task_id"] != capsule.task_id:
            return _ack(
                capsule,
                AckStatus.CONFLICTED,
                reasons=(
                    f"artifact {artifact_id} belongs to task "
                    f"{stored_artifact['task_id']!r}, not {capsule.task_id!r}",
                ),
            )
        artifacts[artifact_id] = artifact

    verified_hashes: dict[str, str] = {}

    def verify_artifact(artifact_id: str) -> tuple[str | None, CapsuleAck | None]:
        if artifact_id in verified_hashes:
            return verified_hashes[artifact_id], None
        manifest = artifacts.get(artifact_id)
        if manifest is None:
            stored = store.get_artifact(artifact_id)
            if stored is None:
                return None, _ack(
                    capsule,
                    AckStatus.NEED_CONTEXT,
                    reasons=(f"referenced artifact {artifact_id} is unavailable",),
                    missing=(f"artifact:{artifact_id}",),
                )
            if stored["task_id"] != capsule.task_id:
                return None, _ack(
                    capsule,
                    AckStatus.CONFLICTED,
                    reasons=(
                        f"artifact {artifact_id} belongs to task {stored['task_id']!r}, "
                        f"not {capsule.task_id!r}",
                    ),
                )
            stored_status = stored["status"]
            if stored_status == "stale":
                return None, _ack(
                    capsule,
                    AckStatus.STALE,
                    reasons=(f"artifact {artifact_id} is stale",),
                )
            if stored_status != "current":
                return None, _ack(
                    capsule,
                    AckStatus.INCOMPATIBLE,
                    reasons=(
                        f"artifact {artifact_id} has unsupported status: "
                        f"{stored_status}",
                    ),
                )
            manifest = _row_artifact(stored)
        expected_hash = manifest.get("sha256")
        if not isinstance(expected_hash, str) or not expected_hash:
            return None, _ack(
                capsule,
                AckStatus.NEED_CONTEXT,
                reasons=(f"artifact {artifact_id} has no sha256",),
                missing=(f"artifact_hash:{artifact_id}",),
            )
        try:
            actual_hash = sha256_bytes(artifact_loader(manifest))
        except Exception as exc:  # External loaders may raise provider-specific errors.
            return None, _ack(
                capsule,
                AckStatus.NEED_CONTEXT,
                reasons=(f"artifact {artifact_id} cannot be read: {exc}",),
                missing=(f"artifact_bytes:{artifact_id}",),
            )
        if actual_hash != expected_hash:
            return None, _ack(
                capsule,
                AckStatus.CONFLICTED,
                reasons=(f"artifact {artifact_id} sha256 mismatch",),
            )
        verified_hashes[artifact_id] = actual_hash
        return actual_hash, None

    for artifact_id in artifacts:
        _, artifact_error = verify_artifact(artifact_id)
        if artifact_error is not None:
            return artifact_error

    inline_facts: dict[str, Mapping[str, Any]] = {}
    for fact in capsule.facts:
        fact_id = fact.get("fact_id")
        state = fact.get("state")
        if not isinstance(fact_id, str) or not fact_id:
            return _ack(
                capsule,
                AckStatus.NEED_CONTEXT,
                reasons=("fact is missing fact_id",),
                missing=("fact_id",),
            )
        if not isinstance(state, str) or state not in FACT_STATES:
            return _ack(
                capsule,
                AckStatus.INCOMPATIBLE,
                reasons=(f"fact {fact_id} has unsupported state: {state}",),
            )
        previous = inline_facts.get(fact_id)
        if previous is not None and not _same_json(previous, fact):
            return _ack(
                capsule,
                AckStatus.CONFLICTED,
                reasons=(f"fact {fact_id} has conflicting values",),
            )
        inline_facts[fact_id] = fact

        fact_expires_at = fact.get("expires_at")
        if fact_expires_at is not None:
            try:
                parsed_fact_expiry = _parse_timestamp(
                    fact_expires_at, require_timezone=True
                )
            except (TypeError, ValueError):
                return _ack(
                    capsule,
                    AckStatus.INCOMPATIBLE,
                    reasons=(
                        f"fact {fact_id} expires_at is not a valid ISO-8601 "
                        "timestamp with timezone",
                    ),
                )
            if parsed_fact_expiry <= check_time:
                return _ack(
                    capsule,
                    AckStatus.STALE,
                    reasons=(f"fact {fact_id} has expired",),
                )

        if state == "invalidated":
            return _ack(
                capsule,
                AckStatus.STALE,
                reasons=(f"fact {fact_id} is invalidated",),
            )
        if state == "conflicted":
            return _ack(
                capsule,
                AckStatus.CONFLICTED,
                reasons=(f"fact {fact_id} is conflicted",),
            )

        stored_fact = store.get_fact(fact_id)
        if stored_fact is not None:
            if stored_fact["task_id"] != capsule.task_id:
                return _ack(
                    capsule,
                    AckStatus.CONFLICTED,
                    reasons=(
                        f"fact {fact_id} belongs to task {stored_fact['task_id']!r}, "
                        f"not {capsule.task_id!r}",
                    ),
                )
            stored_expires_at = stored_fact["expires_at"]
            if stored_expires_at is not None:
                try:
                    parsed_stored_expiry = _parse_timestamp(
                        stored_expires_at, require_timezone=True
                    )
                except (TypeError, ValueError):
                    return _ack(
                        capsule,
                        AckStatus.INCOMPATIBLE,
                        reasons=(
                            f"stored fact {fact_id} expires_at is not a valid "
                            "ISO-8601 timestamp with timezone",
                        ),
                    )
                if parsed_stored_expiry <= check_time:
                    return _ack(
                        capsule,
                        AckStatus.STALE,
                        reasons=(f"stored fact {fact_id} has expired",),
                    )
            if stored_fact["state"] == "invalidated":
                return _ack(
                    capsule,
                    AckStatus.STALE,
                    reasons=(f"stored fact {fact_id} is invalidated",),
                )
            if stored_fact["state"] == "conflicted":
                return _ack(
                    capsule,
                    AckStatus.CONFLICTED,
                    reasons=(f"stored fact {fact_id} is conflicted",),
                )
            if stored_fact["value_json"] != canonical_json(fact.get("value")):
                return _ack(
                    capsule,
                    AckStatus.CONFLICTED,
                    reasons=(f"fact {fact_id} differs from stored value",),
                )

        if state in ("observed", "verified"):
            source_ref = fact.get("source_ref")
            if not isinstance(source_ref, str) or not source_ref:
                return _ack(
                    capsule,
                    AckStatus.NEED_CONTEXT,
                    reasons=(f"fact {fact_id} has no source reference",),
                    missing=(f"source_ref:{fact_id}",),
                )
            source_artifact_id = _source_artifact_id(source_ref)
            if source_artifact_id is None:
                return _ack(
                    capsule,
                    AckStatus.NEED_CONTEXT,
                    reasons=(f"fact {fact_id} source is not an artifact reference",),
                    missing=(f"source_artifact:{fact_id}",),
                )
            actual_source_hash, source_error = verify_artifact(source_artifact_id)
            if source_error is not None:
                return source_error
            claimed_source_hash = fact.get("source_sha256")
            if claimed_source_hash is not None:
                if not isinstance(claimed_source_hash, str):
                    return _ack(
                        capsule,
                        AckStatus.INCOMPATIBLE,
                        reasons=(f"fact {fact_id} source_sha256 is not text",),
                    )
                if claimed_source_hash != actual_source_hash:
                    return _ack(
                        capsule,
                        AckStatus.CONFLICTED,
                        reasons=(f"fact {fact_id} source sha256 mismatch",),
                    )

    dependency_ids: list[str] = list(capsule.depends_on)
    for fact in capsule.facts:
        raw_dependencies = fact.get("depends_on", ())
        if raw_dependencies is None:
            continue
        if not isinstance(raw_dependencies, Sequence) or isinstance(
            raw_dependencies, (str, bytes, bytearray)
        ):
            return _ack(
                capsule,
                AckStatus.INCOMPATIBLE,
                reasons=(
                    f"fact {fact.get('fact_id') or '<unknown>'} depends_on "
                    "is not a sequence",
                ),
            )
        dependency_ids.extend(str(item) for item in raw_dependencies if str(item))

    dependency_states = store.dependency_states(
        tuple(dict.fromkeys(dependency_ids)),
        task_id=capsule.task_id,
        now=check_time,
    )
    missing_dependencies = sorted(
        dependency_id
        for dependency_id, state in dependency_states.items()
        if state is None
    )
    if missing_dependencies:
        return _ack(
            capsule,
            AckStatus.NEED_CONTEXT,
            reasons=("capsule dependencies are unavailable",),
            missing=tuple(f"dependency:{item}" for item in missing_dependencies),
        )
    stale_dependencies = sorted(
        dependency_id
        for dependency_id, state in dependency_states.items()
        if state in ("invalidated", "stale")
    )
    if stale_dependencies:
        return _ack(
            capsule,
            AckStatus.STALE,
            reasons=("capsule depends on stale or invalidated state",),
            missing=tuple(f"stale:{item}" for item in stale_dependencies),
        )
    conflicted_dependencies = sorted(
        dependency_id
        for dependency_id, state in dependency_states.items()
        if state == "conflicted"
    )
    if conflicted_dependencies:
        return _ack(
            capsule,
            AckStatus.CONFLICTED,
            reasons=("capsule depends on conflicted facts",),
            missing=tuple(f"conflict:{item}" for item in conflicted_dependencies),
        )
    rejected_dependencies = sorted(
        dependency_id
        for dependency_id, state in dependency_states.items()
        if state not in (None, "current", "verified", "observed", "inferred")
        and state not in ("invalidated", "stale", "conflicted")
    )
    if rejected_dependencies:
        return _ack(
            capsule,
            AckStatus.NEED_CONTEXT,
            reasons=("capsule depends on unaccepted state",),
            missing=tuple(f"dependency:{item}" for item in rejected_dependencies),
        )

    return _ack(capsule, AckStatus.ACCEPTED)


def ack_capsule(
    capsule: WorkCapsule,
    store: SwarmStore,
    *,
    current_permission_snapshot: JSONValue,
    receiver_permissions: Collection[str] = (),
    receiver_bee: str | None = None,
    artifact_loader: ArtifactLoader | None = None,
    now: datetime | None = None,
    run_id: str | None = None,
    stage_id: str | None = None,
    attempt: int = 1,
) -> CapsuleAck:
    """Idempotently verify and atomically persist a receiver ACK."""

    ack = verify_capsule(
        capsule,
        store,
        current_permission_snapshot=current_permission_snapshot,
        receiver_permissions=receiver_permissions,
        receiver_bee=receiver_bee,
        artifact_loader=artifact_loader,
        now=now,
    )

    # A capsule with no known contract cannot satisfy the table's immutable
    # contract reference.  Return the stale ACK without manufacturing a row.
    if store.get_contract(capsule.task_id, capsule.contract_rev) is None:
        return ack

    try:
        _, persisted_ack, _ = store.record_capsule_ack(
            capsule,
            ack,
            run_id=run_id,
            stage_id=stage_id,
            attempt=attempt,
        )
        return persisted_ack
    except DuplicateMessageError as exc:
        return _ack(
            capsule,
            AckStatus.CONFLICTED,
            reasons=(str(exc) or "message_id was reused with different content",),
            duplicate=True,
        )
    except sqlite3.IntegrityError as exc:
        return _ack(
            capsule,
            AckStatus.CONFLICTED,
            reasons=(f"capsule identity conflicts with stored data: {exc}",),
        )


# Concise API aliases used by callers that treat verification and acknowledgement
# as protocol operations rather than implementation functions.
verify = verify_capsule
ack = ack_capsule
