"""SQLite persistence for the Ghost swarm protocol.

Schema v2 separates the global database sequence from the public run-local event
sequence, records recoverable run metadata, and persists each WorkCapsule and its
receiver ACK atomically.  The store is synchronous by design and can be called
through ``asyncio.to_thread`` by a host that needs non-blocking disk I/O.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator, Mapping, Sequence
import uuid

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


SCHEMA_VERSION = 2

_SCHEMA_V2 = """
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
    session_id TEXT NOT NULL DEFAULT '',
    plan_json TEXT NOT NULL DEFAULT '{}',
    confirmation_json TEXT NOT NULL DEFAULT '{}',
    terminal_event_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, task_id),
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
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events (
    db_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    run_seq INTEGER NOT NULL CHECK (run_seq >= 1),
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    ts REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, run_seq),
    FOREIGN KEY (run_id, task_id)
        REFERENCES runs(run_id, task_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS events_run_seq_idx ON events(run_id, run_seq);

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
    run_id TEXT,
    stage_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    from_bee TEXT NOT NULL,
    to_bee TEXT NOT NULL,
    body_json TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    ack_status TEXT,
    ack_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id, contract_rev)
        REFERENCES contracts(task_id, contract_rev),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS capsules_task_status_idx
    ON capsules(task_id, status);
CREATE INDEX IF NOT EXISTS capsules_run_stage_idx
    ON capsules(run_id, stage_id, attempt);

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
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
"""


class DuplicateMessageError(ValueError):
    """Raised when a message_id already belongs to different capsule content."""


class ImmutableContractError(ValueError):
    """Raised when callers try to overwrite an immutable contract revision."""


class EventConflictError(ValueError):
    """Raised when an event id or run-local sequence is reused inconsistently."""


class IdentityConflictError(ValueError):
    """Raised when a fact/artifact id is rebound to another task."""


def _body_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _capsule_status(ack_status: str | None) -> str:
    if ack_status == "accepted":
        return "current"
    if ack_status in {"stale", "conflicted"}:
        return str(ack_status)
    if ack_status:
        return "rejected"
    return "pending"


class SwarmStore:
    """Thread-safe SQLite store with explicit, bounded transactions."""

    def __init__(self, path: str | Path):
        raw_path = str(path)
        self.path = Path(raw_path)
        self._uri = False
        self._anchor: sqlite3.Connection | None = None
        if raw_path == ":memory:":
            self._database = f"file:muliao-swarm-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
            self._anchor = self._open_connection()
        else:
            self._database = str(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._initialize()

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database,
            timeout=5.0,
            uri=self._uri,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _connect(self) -> sqlite3.Connection:
        return self._open_connection()

    def close(self) -> None:
        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

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

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _initialize(self) -> None:
        with self._schema_lock:
            with self.connection() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                tables = {
                    str(row["name"])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                if "events" in tables and "run_seq" not in self._columns(connection, "events"):
                    self._migrate_v1_to_v2(connection)
                connection.executescript(_SCHEMA_V2)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.commit()

    def _migrate_v1_to_v2(self, connection: sqlite3.Connection) -> None:
        """Migrate the original prototype schema without discarding audit rows."""

        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            run_columns = self._columns(connection, "runs")
            additions = {
                "session_id": "TEXT NOT NULL DEFAULT ''",
                "plan_json": "TEXT NOT NULL DEFAULT '{}'",
                "confirmation_json": "TEXT NOT NULL DEFAULT '{}'",
                "terminal_event_id": "TEXT",
            }
            for name, declaration in additions.items():
                if name not in run_columns:
                    connection.execute(f"ALTER TABLE runs ADD COLUMN {name} {declaration}")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS runs_run_task_idx ON runs(run_id, task_id)"
            )

            connection.execute("ALTER TABLE events RENAME TO events_v1")
            connection.execute(
                """
                CREATE TABLE events (
                    db_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    run_seq INTEGER NOT NULL CHECK (run_seq >= 1),
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    ts REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE (run_id, run_seq),
                    FOREIGN KEY (run_id, task_id)
                        REFERENCES runs(run_id, task_id) ON DELETE CASCADE
                )
                """
            )
            counters: dict[str, int] = {}
            for row in connection.execute("SELECT * FROM events_v1 ORDER BY seq").fetchall():
                run_id = str(row["run_id"])
                counters[run_id] = counters.get(run_id, 0) + 1
                connection.execute(
                    """
                    INSERT INTO events(
                        db_seq, event_id, run_id, task_id, run_seq,
                        event_type, payload_json, ts, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(row["seq"]), row["event_id"], run_id, row["task_id"],
                        counters[run_id], row["event_type"], row["payload_json"],
                        row["ts"], row["created_at"],
                    ),
                )
            connection.execute("DROP TABLE events_v1")

            # Rebuild capsules: ALTER TABLE cannot add the v2 CHECK/foreign-key
            # constraints, and upgraded databases must behave like fresh ones.
            connection.execute("ALTER TABLE capsules RENAME TO capsules_v1")
            connection.execute(
                """
                CREATE TABLE capsules (
                    capsule_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    contract_rev INTEGER NOT NULL CHECK (contract_rev >= 1),
                    run_id TEXT,
                    stage_id TEXT,
                    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
                    from_bee TEXT NOT NULL,
                    to_bee TEXT NOT NULL,
                    body_json TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    ack_status TEXT,
                    ack_json TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    depends_on_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (task_id, contract_rev)
                        REFERENCES contracts(task_id, contract_rev),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                INSERT INTO capsules(
                    capsule_id, message_id, task_id, contract_rev, run_id,
                    stage_id, attempt, from_bee, to_bee, body_json, sha256,
                    ack_status, ack_json, status, depends_on_json, created_at
                )
                SELECT
                    capsule_id, message_id, task_id, contract_rev, NULL,
                    NULL, 1, from_bee, to_bee, body_json, sha256,
                    ack_status, ack_json,
                    CASE
                        WHEN ack_status = 'accepted' THEN 'current'
                        WHEN ack_status IN ('stale', 'conflicted') THEN ack_status
                        WHEN ack_status IS NULL THEN 'pending'
                        ELSE 'rejected'
                    END,
                    depends_on_json, created_at
                FROM capsules_v1
                """
            )
            connection.execute("DROP TABLE capsules_v1")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    def schema_version(self) -> int:
        with self.connection() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def table_names(self) -> set[str]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        return {str(row["name"]) for row in rows}

    def put_contract(self, contract: TaskContract, body_sha256: str | None = None) -> bool:
        body_json = contract.to_json()
        actual_hash = _body_sha256(contract)
        if body_sha256 is not None and body_sha256 != actual_hash:
            raise ValueError("contract body_sha256 does not match canonical body")
        if not contract.created_at:
            raise ValueError("contract created_at is required for persistence")
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT body_json, body_sha256 FROM contracts WHERE task_id = ? AND contract_rev = ?",
                (contract.task_id, contract.contract_rev),
            ).fetchone()
            if current is not None:
                if current["body_json"] == body_json and current["body_sha256"] == actual_hash:
                    return False
                raise ImmutableContractError(
                    f"contract {contract.task_id}@{contract.contract_rev} is immutable"
                )
            latest = connection.execute(
                "SELECT MAX(contract_rev) AS rev FROM contracts WHERE task_id = ?",
                (contract.task_id,),
            ).fetchone()["rev"]
            if latest is None and contract.contract_rev != 1:
                raise ImmutableContractError("first contract revision must be 1")
            if latest is not None and contract.contract_rev != int(latest) + 1:
                raise ImmutableContractError(
                    f"contract revision must follow {latest}, got {contract.contract_rev}"
                )
            connection.execute(
                """
                INSERT INTO contracts(task_id, contract_rev, body_json, body_sha256, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    contract.task_id, contract.contract_rev, body_json,
                    actual_hash, contract.created_at,
                ),
            )
        return True

    def get_contract(self, task_id: str, contract_rev: int) -> TaskContract | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT body_json FROM contracts WHERE task_id = ? AND contract_rev = ?",
                (task_id, contract_rev),
            ).fetchone()
        return None if row is None else TaskContract.from_dict(json.loads(row["body_json"]))

    def current_contract(self, task_id: str) -> TaskContract | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT body_json FROM contracts
                WHERE task_id = ? ORDER BY contract_rev DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        return None if row is None else TaskContract.from_dict(json.loads(row["body_json"]))

    def current_contract_rev(self, task_id: str) -> int | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT MAX(contract_rev) AS rev FROM contracts WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return None if row["rev"] is None else int(row["rev"])

    def register_run(
        self,
        contract: TaskContract,
        run_id: str,
        recipe_id: str,
        *,
        status: str = "queued",
        session_id: str = "",
        plan: Mapping[str, Any] | None = None,
        confirmation: Mapping[str, Any] | None = None,
    ) -> bool:
        """Atomically persist an immutable contract and its run identity."""

        body_json = contract.to_json()
        body_hash = _body_sha256(contract)
        if not contract.created_at:
            raise ValueError("contract created_at is required for persistence")
        now = utc_now()
        plan_json = canonical_json(dict(plan or {}))
        confirmation_json = canonical_json(dict(confirmation or {}))
        with self.transaction() as connection:
            existing_run = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing_run is not None:
                same = (
                    existing_run["task_id"] == contract.task_id
                    and existing_run["recipe_id"] == recipe_id
                    and int(existing_run["contract_rev"]) == contract.contract_rev
                    and existing_run["session_id"] == session_id
                    and existing_run["plan_json"] == plan_json
                )
                if not same:
                    raise IdentityConflictError(f"run_id {run_id!r} is already bound")
                stored_contract = connection.execute(
                    """
                    SELECT body_json, body_sha256 FROM contracts
                    WHERE task_id = ? AND contract_rev = ?
                    """,
                    (contract.task_id, contract.contract_rev),
                ).fetchone()
                if (
                    stored_contract is None
                    or stored_contract["body_json"] != body_json
                    or stored_contract["body_sha256"] != body_hash
                ):
                    raise ImmutableContractError(
                        f"contract {contract.task_id}@{contract.contract_rev} differs from run"
                    )
                return False

            current = connection.execute(
                """
                SELECT body_json, body_sha256 FROM contracts
                WHERE task_id = ? AND contract_rev = ?
                """,
                (contract.task_id, contract.contract_rev),
            ).fetchone()
            if current is not None:
                if current["body_json"] != body_json or current["body_sha256"] != body_hash:
                    raise ImmutableContractError(
                        f"contract {contract.task_id}@{contract.contract_rev} is immutable"
                    )
            else:
                latest = connection.execute(
                    "SELECT MAX(contract_rev) AS rev FROM contracts WHERE task_id = ?",
                    (contract.task_id,),
                ).fetchone()["rev"]
                if latest is None and contract.contract_rev != 1:
                    raise ImmutableContractError("first contract revision must be 1")
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
                        contract.task_id, contract.contract_rev, body_json,
                        body_hash, contract.created_at,
                    ),
                )

            connection.execute(
                """
                INSERT INTO runs(
                    run_id, task_id, recipe_id, status, contract_rev, session_id,
                    plan_json, confirmation_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, contract.task_id, recipe_id, status,
                    contract.contract_rev, session_id, plan_json,
                    confirmation_json, now, now,
                ),
            )
        return True

    def confirm_run(
        self,
        run_id: str,
        confirmation: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Advance one waiting run without mutating its immutable plan."""

        confirmation_json = canonical_json(dict(confirmation))
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status, terminal_event_id FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"run not found: {run_id}")
            if row["terminal_event_id"]:
                raise IdentityConflictError(f"run {run_id!r} is already terminal")
            if row["status"] not in {"requires_confirmation", "paused", "interrupted"}:
                raise IdentityConflictError(
                    f"run {run_id!r} cannot be confirmed from {row['status']}"
                )
            connection.execute(
                """
                UPDATE runs
                SET status = 'running', confirmation_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (confirmation_json, utc_now(), run_id),
            )
        state = self.get_run(run_id)
        assert state is not None
        return state

    def next_run_seq(self, run_id: str) -> int:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(run_seq), 0) + 1 AS next FROM events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["next"])

    def create_run(
        self,
        run_id: str,
        task_id: str,
        recipe_id: str,
        contract_rev: int,
        *,
        status: str = "queued",
        session_id: str = "",
        plan: Mapping[str, Any] | None = None,
        confirmation: Mapping[str, Any] | None = None,
    ) -> bool:
        now = utc_now()
        plan_json = canonical_json(dict(plan or {}))
        confirmation_json = canonical_json(dict(confirmation or {}))
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing is not None:
                same = (
                    existing["task_id"] == task_id
                    and existing["recipe_id"] == recipe_id
                    and int(existing["contract_rev"]) == int(contract_rev)
                    and existing["session_id"] == session_id
                    and existing["plan_json"] == plan_json
                )
                if same:
                    return False
                raise IdentityConflictError(f"run_id {run_id!r} is already bound")
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, task_id, recipe_id, status, contract_rev, session_id,
                    plan_json, confirmation_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, task_id, recipe_id, status, contract_rev, session_id,
                    plan_json, confirmation_json, now, now,
                ),
            )
        return True

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["plan"] = json.loads(value.pop("plan_json"))
        value["confirmation"] = json.loads(value.pop("confirmation_json"))
        return value

    def update_run_status(
        self,
        run_id: str,
        status: str,
        *,
        terminal_event_id: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE runs
                SET status = ?, terminal_event_id = COALESCE(?, terminal_event_id), updated_at = ?
                WHERE run_id = ?
                """,
                (status, terminal_event_id, utc_now(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"run not found: {run_id}")

    def recover_interrupted_runs(self) -> list[str]:
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT run_id FROM runs WHERE status IN ('running', 'cancelling')"
            ).fetchall()
            ids = [str(row["run_id"]) for row in rows]
            if ids:
                connection.execute(
                    """
                    UPDATE runs SET status = 'interrupted', updated_at = ?
                    WHERE status IN ('running', 'cancelling')
                    """,
                    (utc_now(),),
                )
        return ids

    def append_event(
        self,
        event: SwarmEvent | None = None,
        *,
        event_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        event_type: str | None = None,
        payload: Mapping[str, Any] | None = None,
        ts: float = 0.0,
        seq: int | None = None,
    ) -> SwarmEvent:
        """Compatibility wrapper around :meth:`record_event`."""

        persisted, _ = self.record_event(
            event,
            event_id=event_id,
            run_id=run_id,
            task_id=task_id,
            event_type=event_type,
            payload=payload,
            ts=ts,
            seq=seq,
        )
        return persisted

    def record_event(
        self,
        event: SwarmEvent | None = None,
        *,
        event_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        event_type: str | None = None,
        payload: Mapping[str, Any] | None = None,
        ts: float = 0.0,
        seq: int | None = None,
        run_status: str | None = None,
    ) -> tuple[SwarmEvent, bool]:
        """Atomically append an event and synchronize its run status.

        The public sequence is contiguous per run. Exact replay is idempotent and
        returns ``inserted=False``; any reused identity or skipped sequence fails.
        """

        with self.transaction() as connection:
            if event is None:
                if not all((event_id, run_id, task_id, event_type)):
                    raise ValueError("event_id, run_id, task_id, and event_type are required")
                if seq is None:
                    row = connection.execute(
                        "SELECT COALESCE(MAX(run_seq), 0) + 1 AS next FROM events WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()
                    seq = int(row["next"])
                event = SwarmEvent(
                    event_id=str(event_id), seq=int(seq), run_id=str(run_id),
                    task_id=str(task_id), type=str(event_type),
                    payload=dict(payload or {}), ts=float(ts),
                )

            payload_json = canonical_json(event.payload)
            run = connection.execute(
                "SELECT task_id, status, terminal_event_id FROM runs WHERE run_id = ?",
                (event.run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(f"run not found: {event.run_id}")
            if str(run["task_id"]) != event.task_id:
                raise EventConflictError("event task_id differs from run task_id")

            by_id = connection.execute(
                "SELECT * FROM events WHERE event_id = ?", (event.event_id,)
            ).fetchone()
            if by_id is not None:
                if self._event_row_matches(by_id, event, payload_json):
                    return self._event_from_row(by_id), False
                raise EventConflictError(f"event_id {event.event_id!r} has different content")
            if run["terminal_event_id"]:
                raise EventConflictError(
                    f"run {event.run_id!r} is already terminal at {run['terminal_event_id']}"
                )

            by_seq = connection.execute(
                "SELECT * FROM events WHERE run_id = ? AND run_seq = ?",
                (event.run_id, event.seq),
            ).fetchone()
            if by_seq is not None:
                raise EventConflictError(
                    f"run {event.run_id!r} sequence {event.seq} is already used"
                )
            latest = connection.execute(
                "SELECT COALESCE(MAX(run_seq), 0) AS latest FROM events WHERE run_id = ?",
                (event.run_id,),
            ).fetchone()
            expected = int(latest["latest"]) + 1
            if event.seq != expected:
                raise EventConflictError(
                    f"run {event.run_id!r} expected sequence {expected}, got {event.seq}"
                )

            connection.execute(
                """
                INSERT INTO events(
                    event_id, run_id, task_id, run_seq, event_type,
                    payload_json, ts, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id, event.run_id, event.task_id, event.seq,
                    event.type, payload_json, event.ts, utc_now(),
                ),
            )
            if run_status is not None:
                terminal_event_id = event.event_id if run_status in {
                    "completed", "cancelled", "failed", "skipped"
                } else None
                connection.execute(
                    """
                    UPDATE runs
                    SET status = ?, terminal_event_id = COALESCE(?, terminal_event_id),
                        updated_at = ?
                    WHERE run_id = ?
                    """,
                    (run_status, terminal_event_id, utc_now(), event.run_id),
                )
        return event, True

    @staticmethod
    def _event_row_matches(row: sqlite3.Row, event: SwarmEvent, payload_json: str) -> bool:
        return (
            row["run_id"] == event.run_id
            and row["task_id"] == event.task_id
            and int(row["run_seq"]) == event.seq
            and row["event_type"] == event.type
            and row["payload_json"] == payload_json
            and float(row["ts"]) == float(event.ts)
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> SwarmEvent:
        return SwarmEvent(
            event_id=str(row["event_id"]),
            seq=int(row["run_seq"]),
            run_id=str(row["run_id"]),
            task_id=str(row["task_id"]),
            type=str(row["event_type"]),
            payload=json.loads(row["payload_json"]),
            ts=float(row["ts"]),
        )

    def list_events(self, run_id: str, *, after_run_seq: int = 0) -> list[SwarmEvent]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM events WHERE run_id = ? AND run_seq > ?
                ORDER BY run_seq
                """,
                (run_id, int(after_run_seq)),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def put_fact(self, fact: FactRecord) -> None:
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT task_id FROM facts WHERE fact_id = ?", (fact.fact_id,)
            ).fetchone()
            if current is not None and current["task_id"] != fact.task_id:
                raise IdentityConflictError(f"fact_id {fact.fact_id!r} belongs to another task")
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
                """,
                (
                    fact.fact_id, fact.task_id, fact.state, canonical_json(fact.value),
                    fact.source_ref, fact.source_sha256, canonical_json(fact.depends_on),
                    fact.expires_at, fact.updated_at,
                ),
            )

    def get_fact(self, fact_id: str) -> sqlite3.Row | None:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()

    def put_artifact(self, artifact: ArtifactRecord) -> None:
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT task_id FROM artifacts WHERE artifact_id = ?", (artifact.artifact_id,)
            ).fetchone()
            if current is not None and current["task_id"] != artifact.task_id:
                raise IdentityConflictError(
                    f"artifact_id {artifact.artifact_id!r} belongs to another task"
                )
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
                """,
                (
                    artifact.artifact_id, artifact.task_id, artifact.uri, artifact.mime,
                    artifact.sha256, artifact.status, canonical_json(artifact.depends_on),
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
        self,
        capsule: WorkCapsule,
        body_sha256: str | None = None,
        *,
        run_id: str | None = None,
        stage_id: str | None = None,
        attempt: int = 1,
    ) -> tuple[bool, sqlite3.Row]:
        """Compatibility insert for callers that have not produced an ACK yet."""

        body_json = capsule.to_json()
        actual_hash = _body_sha256(capsule)
        if body_sha256 is not None and body_sha256 != actual_hash:
            raise ValueError("capsule body_sha256 does not match canonical body")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM capsules WHERE message_id = ?", (capsule.message_id,)
            ).fetchone()
            if existing is not None:
                if existing["sha256"] != actual_hash or existing["body_json"] != body_json:
                    raise DuplicateMessageError(
                        f"message_id {capsule.message_id!r} has different content"
                    )
                return False, existing
            self._insert_capsule(
                connection, capsule, actual_hash,
                run_id=run_id, stage_id=stage_id, attempt=attempt,
                ack=None,
            )
            row = connection.execute(
                "SELECT * FROM capsules WHERE message_id = ?", (capsule.message_id,)
            ).fetchone()
            assert row is not None
            return True, row

    @staticmethod
    def _insert_capsule(
        connection: sqlite3.Connection,
        capsule: WorkCapsule,
        body_hash: str,
        *,
        run_id: str | None,
        stage_id: str | None,
        attempt: int,
        ack: CapsuleAck | None,
    ) -> None:
        ack_status = None if ack is None else ack.status
        connection.execute(
            """
            INSERT INTO capsules(
                capsule_id, message_id, task_id, contract_rev,
                run_id, stage_id, attempt, from_bee, to_bee,
                body_json, sha256, ack_status, ack_json, status,
                depends_on_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                capsule.capsule_id, capsule.message_id, capsule.task_id,
                capsule.contract_rev, run_id, stage_id, int(attempt),
                capsule.from_bee, capsule.to_bee, capsule.to_json(), body_hash,
                ack_status, None if ack is None else ack.to_json(),
                _capsule_status(ack_status), canonical_json(capsule.depends_on),
                capsule.created_at,
            ),
        )

    @staticmethod
    def _promote_capsule_evidence(
        connection: sqlite3.Connection,
        capsule: WorkCapsule,
    ) -> None:
        """Promote receiver-accepted inline evidence in the ACK transaction."""

        for raw in capsule.facts:
            data = dict(raw)
            data.setdefault("task_id", capsule.task_id)
            if str(data["task_id"]) != capsule.task_id:
                raise IdentityConflictError("capsule fact belongs to another task")
            fact = FactRecord.from_dict(data)
            current = connection.execute(
                "SELECT task_id FROM facts WHERE fact_id = ?", (fact.fact_id,)
            ).fetchone()
            if current is not None and str(current["task_id"]) != fact.task_id:
                raise IdentityConflictError(
                    f"fact_id {fact.fact_id!r} belongs to another task"
                )
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
                """,
                (
                    fact.fact_id, fact.task_id, fact.state,
                    canonical_json(fact.value), fact.source_ref,
                    fact.source_sha256, canonical_json(fact.depends_on),
                    fact.expires_at, fact.updated_at,
                ),
            )

        for raw in capsule.artifacts:
            data = dict(raw)
            data.setdefault("task_id", capsule.task_id)
            if str(data["task_id"]) != capsule.task_id:
                raise IdentityConflictError("capsule artifact belongs to another task")
            artifact = ArtifactRecord.from_dict(data)
            current = connection.execute(
                "SELECT task_id FROM artifacts WHERE artifact_id = ?",
                (artifact.artifact_id,),
            ).fetchone()
            if current is not None and str(current["task_id"]) != artifact.task_id:
                raise IdentityConflictError(
                    f"artifact_id {artifact.artifact_id!r} belongs to another task"
                )
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
                """,
                (
                    artifact.artifact_id, artifact.task_id, artifact.uri,
                    artifact.mime, artifact.sha256, artifact.status,
                    canonical_json(artifact.depends_on), artifact.created_at,
                ),
            )

    def record_capsule_ack(
        self,
        capsule: WorkCapsule,
        ack: CapsuleAck,
        *,
        run_id: str | None = None,
        stage_id: str | None = None,
        attempt: int = 1,
    ) -> tuple[bool, CapsuleAck, sqlite3.Row]:
        """Atomically deduplicate, insert the Capsule, and persist its receiver ACK."""

        body_json = capsule.to_json()
        body_hash = _body_sha256(capsule)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM capsules WHERE message_id = ?", (capsule.message_id,)
            ).fetchone()
            if existing is not None:
                if existing["sha256"] != body_hash or existing["body_json"] != body_json:
                    raise DuplicateMessageError(
                        f"message_id {capsule.message_id!r} has different content"
                    )
                if existing["capsule_id"] != capsule.capsule_id:
                    raise DuplicateMessageError(
                        f"message_id {capsule.message_id!r} belongs to another capsule"
                    )
                for field, supplied in (
                    ("run_id", run_id),
                    ("stage_id", stage_id),
                ):
                    stored = existing[field]
                    if supplied is not None and stored is not None and str(stored) != str(supplied):
                        raise DuplicateMessageError(
                            f"message_id {capsule.message_id!r} changed {field}"
                        )
                if int(existing["attempt"]) != int(attempt):
                    raise DuplicateMessageError(
                        f"message_id {capsule.message_id!r} changed attempt"
                    )

                if existing["ack_json"]:
                    persisted = CapsuleAck.from_dict(json.loads(existing["ack_json"]))
                    if persisted.status == ack.status:
                        if persisted.status == "accepted":
                            self._promote_capsule_evidence(connection, capsule)
                        duplicate = CapsuleAck(
                            message_id=persisted.message_id,
                            capsule_id=persisted.capsule_id,
                            status=persisted.status,
                            reasons=tuple(persisted.reasons) + ("duplicate message_id",),
                            missing=persisted.missing,
                            checked_sha256=persisted.checked_sha256,
                            duplicate=True,
                            checked_at=persisted.checked_at,
                        )
                        return False, duplicate, existing
                    # Receiver state changed since the previous delivery. Persist
                    # the fresh verdict; an old accepted ACK must not override a
                    # new permission, expiry, conflict, or dependency rejection.
                    ack = CapsuleAck(
                        message_id=ack.message_id,
                        capsule_id=ack.capsule_id,
                        status=ack.status,
                        reasons=tuple(ack.reasons) + ("duplicate revalidated",),
                        missing=ack.missing,
                        checked_sha256=ack.checked_sha256,
                        duplicate=True,
                        checked_at=ack.checked_at,
                    )
                connection.execute(
                    """
                    UPDATE capsules
                    SET ack_status = ?, ack_json = ?, status = ?,
                        run_id = COALESCE(run_id, ?), stage_id = COALESCE(stage_id, ?),
                        attempt = ?
                    WHERE message_id = ? AND capsule_id = ?
                    """,
                    (
                        ack.status, ack.to_json(), _capsule_status(ack.status),
                        run_id, stage_id, int(attempt),
                        capsule.message_id, capsule.capsule_id,
                    ),
                )
                if ack.status == "accepted":
                    self._promote_capsule_evidence(connection, capsule)
                row = connection.execute(
                    "SELECT * FROM capsules WHERE message_id = ?", (capsule.message_id,)
                ).fetchone()
                assert row is not None
                return False, ack, row

            self._insert_capsule(
                connection, capsule, body_hash,
                run_id=run_id, stage_id=stage_id, attempt=attempt, ack=ack,
            )
            if ack.status == "accepted":
                self._promote_capsule_evidence(connection, capsule)
            row = connection.execute(
                "SELECT * FROM capsules WHERE message_id = ?", (capsule.message_id,)
            ).fetchone()
            assert row is not None
            return True, ack, row

    def set_capsule_ack(self, ack: CapsuleAck) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE capsules SET ack_status = ?, ack_json = ?, status = ?
                WHERE message_id = ? AND capsule_id = ?
                """,
                (
                    ack.status, ack.to_json(), _capsule_status(ack.status),
                    ack.message_id, ack.capsule_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"capsule not found for message {ack.message_id!r}")

    def dependency_states(
        self,
        dependency_ids: Sequence[str],
        *,
        task_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, str | None]:
        states: dict[str, str | None] = {}
        check_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self.connection() as connection:
            for dependency_id in dependency_ids:
                fact = connection.execute(
                    "SELECT state, task_id, expires_at FROM facts WHERE fact_id = ?",
                    (dependency_id,),
                ).fetchone()
                if fact is not None:
                    if task_id is not None and str(fact["task_id"]) != str(task_id):
                        states[dependency_id] = "forbidden"
                        continue
                    expires_at = fact["expires_at"]
                    if expires_at:
                        try:
                            normalized = str(expires_at)
                            if normalized.endswith("Z"):
                                normalized = normalized[:-1] + "+00:00"
                            expiry = datetime.fromisoformat(normalized)
                            if expiry.tzinfo is None:
                                expiry = expiry.replace(tzinfo=timezone.utc)
                            if expiry.astimezone(timezone.utc) <= check_time:
                                states[dependency_id] = "stale"
                                continue
                        except (TypeError, ValueError):
                            states[dependency_id] = "incompatible"
                            continue
                    states[dependency_id] = str(fact["state"])
                    continue
                artifact = connection.execute(
                    "SELECT status, task_id FROM artifacts WHERE artifact_id = ?",
                    (dependency_id,),
                ).fetchone()
                if artifact is not None:
                    if task_id is not None and str(artifact["task_id"]) != str(task_id):
                        states[dependency_id] = "forbidden"
                    else:
                        states[dependency_id] = str(artifact["status"])
                    continue
                capsule = connection.execute(
                    "SELECT status, ack_status, task_id FROM capsules WHERE capsule_id = ?",
                    (dependency_id,),
                ).fetchone()
                if capsule is None:
                    states[dependency_id] = None
                elif task_id is not None and str(capsule["task_id"]) != str(task_id):
                    states[dependency_id] = "forbidden"
                elif capsule["ack_status"] == "accepted":
                    states[dependency_id] = str(capsule["status"])
                else:
                    states[dependency_id] = str(capsule["ack_status"] or "pending")
        return states

    def invalidate_fact(self, fact_id: str) -> dict[str, list[str]]:
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

            invalidated_facts: set[str] = {fact_id}
            stale_artifacts: set[str] = set()
            stale_capsules: set[str] = set()
            frontier = {fact_id}
            while frontier:
                next_frontier: set[str] = set()
                for row in connection.execute(
                    "SELECT fact_id, depends_on_json, state FROM facts"
                ).fetchall():
                    dependent_id = str(row["fact_id"])
                    dependencies = set(json.loads(row["depends_on_json"]))
                    if dependencies & frontier and dependent_id not in invalidated_facts:
                        invalidated_facts.add(dependent_id)
                        next_frontier.add(dependent_id)
                        if row["state"] != "invalidated":
                            connection.execute(
                                "UPDATE facts SET state = 'invalidated', updated_at = ? WHERE fact_id = ?",
                                (utc_now(), dependent_id),
                            )

                for row in connection.execute(
                    "SELECT artifact_id, depends_on_json, status FROM artifacts"
                ).fetchall():
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

                for row in connection.execute(
                    "SELECT capsule_id, depends_on_json, status FROM capsules"
                ).fetchall():
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
            "facts": sorted(invalidated_facts),
            "artifacts": sorted(stale_artifacts),
            "capsules": sorted(stale_capsules),
        }
