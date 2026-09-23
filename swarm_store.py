"""SQLite persistence for the Ghost swarm protocol.

The store owns schema creation, transaction boundaries, immutable contract
revisions, idempotent capsule insertion, append-only events, and the minimal
fact/artifact dependency state needed by GCTX/0.1.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator, Mapping, Sequence

from swarm_models import (
    ArtifactRecord,
    CapsuleAck,
    FactRecord,
    SwarmEvent,
    TaskContract,
    WorkCapsule,
    canonical_json,
    utc_now,
)


_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS contracts (
    task_id TEXT NOT NULL,
    contract_rev INTEGER NOT NULL CHECK (contract_rev >= 1),
    body_json TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, contract_rev)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    recipe_id TEXT NOT NULL,
    status TEXT NOT NULL,
    contract_rev INTEGER NOT NULL CHECK (contract_rev >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (task_id, contract_rev)
        REFERENCES contracts(task_id, contract_rev)
);

CREATE TABLE IF NOT EXISTS bee_runs (
    bee_run_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    bee_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    input_capsule_id TEXT,
    output_capsule_id TEXT,
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    ts REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS events_run_seq_idx ON events(run_id, seq);

CREATE TABLE IF NOT EXISTS facts (
    fact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('observed', 'inferred', 'verified', 'conflicted', 'invalidated')
    ),
    value_json TEXT NOT NULL,
    source_ref TEXT,
    source_sha256 TEXT,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    expires_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS facts_task_state_idx ON facts(task_id, state);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    uri TEXT NOT NULL,
    mime TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'current',
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS artifacts_task_status_idx
    ON artifacts(task_id, status);

CREATE TABLE IF NOT EXISTS capsules (
    capsule_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    contract_rev INTEGER NOT NULL CHECK (contract_rev >= 1),
    from_bee TEXT NOT NULL,
    to_bee TEXT NOT NULL,
    body_json TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    ack_status TEXT,
    ack_json TEXT,
    status TEXT NOT NULL DEFAULT 'current',
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id, contract_rev)
        REFERENCES contracts(task_id, contract_rev)
);

CREATE INDEX IF NOT EXISTS capsules_task_status_idx
    ON capsules(task_id, status);

CREATE TABLE IF NOT EXISTS usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    bee_id TEXT,
    provider TEXT,
    model TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    success INTEGER NOT NULL DEFAULT 1 CHECK (success IN (0, 1)),
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
"""


class DuplicateMessageError(ValueError):
    """Raised when a message_id already belongs to a different capsule body."""


class ImmutableContractError(ValueError):
    """Raised when callers try to overwrite an immutable contract revision."""


class SwarmStore:
    """Small synchronous SQLite store with explicit transactions.

    A connection is opened for each operation, so the object can safely be
    shared by asyncio workers through ``asyncio.to_thread`` if needed later.
    SQLite WAL and a busy timeout keep the single-process prototype robust
    without pretending to provide distributed consistency.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection and always close it, including on Windows."""

        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._schema_lock:
            with self.connection() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(_SCHEMA)

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def table_names(self) -> set[str]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        return {str(row["name"]) for row in rows}

    def put_contract(self, contract: TaskContract, body_sha256: str) -> bool:
        body_json = contract.to_json()
        with self.transaction() as connection:
            current = connection.execute(
                """
                SELECT body_json, body_sha256
                FROM contracts
                WHERE task_id = ? AND contract_rev = ?
                """,
                (contract.task_id, contract.contract_rev),
            ).fetchone()
            if current is not None:
                if (
                    current["body_json"] == body_json
                    and current["body_sha256"] == body_sha256
                ):
                    return False
                raise ImmutableContractError(
                    f"contract {contract.task_id}@{contract.contract_rev} is immutable"
                )
            latest = connection.execute(
                "SELECT MAX(contract_rev) AS rev FROM contracts WHERE task_id = ?",
                (contract.task_id,),
            ).fetchone()["rev"]
            if latest is not None and contract.contract_rev != int(latest) + 1:
                raise ImmutableContractError(
                    f"contract revision must follow {latest}, got {contract.contract_rev}"
                )
            connection.execute(
                """
                INSERT INTO contracts(
                    task_id, contract_rev, body_json, body_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    contract.task_id,
                    contract.contract_rev,
                    body_json,
                    body_sha256,
                    contract.created_at,
                ),
            )
        return True

    def get_contract(self, task_id: str, contract_rev: int) -> TaskContract | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT body_json FROM contracts
                WHERE task_id = ? AND contract_rev = ?
                """,
                (task_id, contract_rev),
            ).fetchone()
        if row is None:
            return None
        import json

        return TaskContract.from_dict(json.loads(row["body_json"]))

    def current_contract(self, task_id: str) -> TaskContract | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT body_json FROM contracts
                WHERE task_id = ?
                ORDER BY contract_rev DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        import json

        return TaskContract.from_dict(json.loads(row["body_json"]))

    def current_contract_rev(self, task_id: str) -> int | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT MAX(contract_rev) AS rev FROM contracts WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return None if row["rev"] is None else int(row["rev"])

    def create_run(
        self,
        run_id: str,
        task_id: str,
        recipe_id: str,
        contract_rev: int,
        *,
        status: str = "queued",
    ) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, task_id, recipe_id, status, contract_rev,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, task_id, recipe_id, status, contract_rev, now, now),
            )

    def append_event(
        self,
        *,
        event_id: str,
        run_id: str,
        task_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        ts: float = 0.0,
    ) -> SwarmEvent:
        payload_json = canonical_json(dict(payload or {}))
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO events(
                    event_id, run_id, task_id, event_type,
                    payload_json, ts, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    run_id,
                    task_id,
                    event_type,
                    payload_json,
                    ts,
                    utc_now(),
                ),
            )
            seq = int(cursor.lastrowid)
        return SwarmEvent(
            event_id=event_id,
            seq=seq,
            run_id=run_id,
            task_id=task_id,
            type=event_type,
            payload=dict(payload or {}),
            ts=ts,
        )

    def put_fact(self, fact: FactRecord) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO facts(
                    fact_id, task_id, state, value_json, source_ref,
                    source_sha256, depends_on_json, expires_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fact_id) DO UPDATE SET
                    state = excluded.state,
                    value_json = excluded.value_json,
                    source_ref = excluded.source_ref,
                    source_sha256 = excluded.source_sha256,
                    depends_on_json = excluded.depends_on_json,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                WHERE facts.task_id = excluded.task_id
                """,
                (
                    fact.fact_id,
                    fact.task_id,
                    fact.state,
                    canonical_json(fact.value),
                    fact.source_ref,
                    fact.source_sha256,
                    canonical_json(fact.depends_on),
                    fact.expires_at,
                    fact.updated_at,
                ),
            )

    def get_fact(self, fact_id: str) -> sqlite3.Row | None:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()

    def put_artifact(self, artifact: ArtifactRecord) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, task_id, uri, mime, sha256,
                    status, depends_on_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET
                    uri = excluded.uri,
                    mime = excluded.mime,
                    sha256 = excluded.sha256,
                    status = excluded.status,
                    depends_on_json = excluded.depends_on_json
                WHERE artifacts.task_id = excluded.task_id
                """,
                (
                    artifact.artifact_id,
                    artifact.task_id,
                    artifact.uri,
                    artifact.mime,
                    artifact.sha256,
                    artifact.status,
                    canonical_json(artifact.depends_on),
                    artifact.created_at,
                ),
            )

    def get_artifact(self, artifact_id: str) -> sqlite3.Row | None:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()

    def find_capsule_by_message(self, message_id: str) -> sqlite3.Row | None:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM capsules WHERE message_id = ?", (message_id,)
            ).fetchone()

    def get_capsule(self, capsule_id: str) -> sqlite3.Row | None:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM capsules WHERE capsule_id = ?", (capsule_id,)
            ).fetchone()

    def put_capsule(
        self, capsule: WorkCapsule, body_sha256: str
    ) -> tuple[bool, sqlite3.Row]:
        body_json = capsule.to_json()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM capsules WHERE message_id = ?",
                (capsule.message_id,),
            ).fetchone()
            if existing is not None:
                if existing["sha256"] != body_sha256 or existing["body_json"] != body_json:
                    raise DuplicateMessageError(
                        f"message_id {capsule.message_id!r} has different content"
                    )
                return False, existing
            connection.execute(
                """
                INSERT INTO capsules(
                    capsule_id, message_id, task_id, contract_rev,
                    from_bee, to_bee, body_json, sha256, status,
                    depends_on_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'current', ?, ?)
                """,
                (
                    capsule.capsule_id,
                    capsule.message_id,
                    capsule.task_id,
                    capsule.contract_rev,
                    capsule.from_bee,
                    capsule.to_bee,
                    body_json,
                    body_sha256,
                    canonical_json(capsule.depends_on),
                    capsule.created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM capsules WHERE message_id = ?",
                (capsule.message_id,),
            ).fetchone()
            assert row is not None
            return True, row

    def set_capsule_ack(self, ack: CapsuleAck) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE capsules
                SET ack_status = ?, ack_json = ?
                WHERE message_id = ? AND capsule_id = ?
                """,
                (ack.status, ack.to_json(), ack.message_id, ack.capsule_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"capsule not found for message {ack.message_id!r}")

    def dependency_states(self, dependency_ids: Sequence[str]) -> dict[str, str | None]:
        states: dict[str, str | None] = {}
        with self.connection() as connection:
            for dependency_id in dependency_ids:
                fact = connection.execute(
                    "SELECT state FROM facts WHERE fact_id = ?", (dependency_id,)
                ).fetchone()
                if fact is not None:
                    states[dependency_id] = str(fact["state"])
                    continue
                artifact = connection.execute(
                    "SELECT status FROM artifacts WHERE artifact_id = ?",
                    (dependency_id,),
                ).fetchone()
                if artifact is not None:
                    states[dependency_id] = str(artifact["status"])
                    continue
                capsule = connection.execute(
                    "SELECT status FROM capsules WHERE capsule_id = ?",
                    (dependency_id,),
                ).fetchone()
                states[dependency_id] = (
                    None if capsule is None else str(capsule["status"])
                )
        return states

    def invalidate_fact(self, fact_id: str) -> dict[str, list[str]]:
        """Invalidate a fact and mark direct/transitive dependents stale.

        The graph is intentionally minimal: dependency IDs are stored as JSON
        arrays and scanned in one transaction.  It preserves audit history and
        marks only reachable artifacts/capsules stale; it never deletes data.
        """

        import json

        with self.transaction() as connection:
            fact = connection.execute(
                "SELECT fact_id FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if fact is None:
                raise KeyError(f"fact not found: {fact_id}")
            connection.execute(
                "UPDATE facts SET state = 'invalidated', updated_at = ? WHERE fact_id = ?",
                (utc_now(), fact_id),
            )

            stale_artifacts: set[str] = set()
            stale_capsules: set[str] = set()
            frontier = {fact_id}
            while frontier:
                next_frontier: set[str] = set()
                artifact_rows = connection.execute(
                    "SELECT artifact_id, depends_on_json, status FROM artifacts"
                ).fetchall()
                for row in artifact_rows:
                    artifact_id = str(row["artifact_id"])
                    dependencies = set(json.loads(row["depends_on_json"]))
                    if dependencies & frontier and artifact_id not in stale_artifacts:
                        stale_artifacts.add(artifact_id)
                        next_frontier.add(artifact_id)
                        if row["status"] != "stale":
                            connection.execute(
                                "UPDATE artifacts SET status = 'stale' WHERE artifact_id = ?",
                                (artifact_id,),
                            )

                capsule_rows = connection.execute(
                    "SELECT capsule_id, depends_on_json, status FROM capsules"
                ).fetchall()
                for row in capsule_rows:
                    capsule_id = str(row["capsule_id"])
                    dependencies = set(json.loads(row["depends_on_json"]))
                    if dependencies & frontier and capsule_id not in stale_capsules:
                        stale_capsules.add(capsule_id)
                        next_frontier.add(capsule_id)
                        if row["status"] != "stale":
                            connection.execute(
                                "UPDATE capsules SET status = 'stale' WHERE capsule_id = ?",
                                (capsule_id,),
                            )
                frontier = next_frontier

        return {
            "facts": [fact_id],
            "artifacts": sorted(stale_artifacts),
            "capsules": sorted(stale_capsules),
        }
