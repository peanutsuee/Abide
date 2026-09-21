"""Persistent local state and write-freeze gate for Host asset migration."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import secrets
import sqlite3
from typing import Callable

from maintenance_write_gate import (
    DEFAULT_WRITE_COORDINATOR,
    guarded_mutation,
)


MIGRATION_SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 5_000
_ASSET_ID_LENGTH = 32
_CHECKPOINT_TRANSITIONS = {
    "ready": {"running", "blocked", "failed", "completed"},
    "running": {"running", "paused", "blocked", "failed", "completed"},
    "paused": {"running", "blocked", "failed"},
    "blocked": {"blocked"},
    "failed": {"failed"},
    "completed": {"completed"},
}


class HostMigrationStateError(RuntimeError):
    """Stable fail-closed error raised by the local migration state store."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class MigrationCheckpoint:
    migration_key: str
    migration_version: int
    source_identity: str
    target_identity: str
    snapshot_generation: int
    upper_bound_asset_id: str | None
    last_completed_asset_id: str | None
    status: str
    initial_asset_count: int
    processed_count: int
    imported_count: int
    skipped_idempotent_count: int
    blocked_asset_id: str | None
    error_code: str | None
    created_at: str
    updated_at: str
    completed_at: str | None


@dataclass(frozen=True)
class MigrationStateInspection:
    """Read-only state summary that never exposes lease or writer tokens."""

    generation: int
    write_uncertain: bool
    lease_state: str
    checkpoint: MigrationCheckpoint | None


class AssetWriteGuard(AbstractContextManager):
    """A serialized write permission with a persistent uncertainty marker."""

    def __init__(self, state: "HostMigrationState") -> None:
        self._state = state
        self._connection: sqlite3.Connection | None = None
        self._owner_token: str | None = None
        self._outcome: str | None = None
        self._maintenance_scope = None

    def __enter__(self) -> "AssetWriteGuard":
        self._maintenance_scope = self._state.write_coordinator.writer_scope(
            "legacy_asset_guard"
        )
        self._maintenance_scope.__enter__()
        owner_token = secrets.token_hex(32)
        try:
            connection = self._state._connect()
        except BaseException:
            self._maintenance_scope.__exit__(None, None, None)
            self._maintenance_scope = None
            raise
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._state._assert_runtime_schema(connection)
            generation = connection.execute(
                """
                SELECT legacy_generation, write_uncertain
                FROM migration_meta
                WHERE singleton = 1
                """
            ).fetchone()
            if (
                generation is None
                or int(generation["legacy_generation"]) < 0
                or int(generation["write_uncertain"]) != 0
            ):
                raise HostMigrationStateError(
                    "asset_write_gate_unavailable"
                )
            lease = connection.execute(
                """
                SELECT owner_token, expires_at
                FROM freeze_lease
                WHERE singleton = 1
                """
            ).fetchone()
            if lease is not None and self._state._is_future(lease["expires_at"]):
                raise HostMigrationStateError("asset_write_frozen")
            cursor = connection.execute(
                """
                UPDATE migration_meta
                SET write_uncertain = 1,
                    write_owner_token = ?,
                    write_started_at = ?
                WHERE singleton = 1 AND write_uncertain = 0
                """,
                (owner_token, self._state._timestamp()),
            )
            if cursor.rowcount != 1:
                raise HostMigrationStateError(
                    "asset_write_gate_unavailable"
            )
            connection.commit()

            # Reacquire and retain the coordination lock through the complete
            # legacy write. If this process crashes, the first committed
            # transaction leaves write_uncertain set for future fail-closed
            # freeze attempts.
            connection.execute("BEGIN IMMEDIATE")
            self._state._assert_runtime_schema(connection)
            marker = connection.execute(
                """
                SELECT write_uncertain, write_owner_token
                FROM migration_meta
                WHERE singleton = 1
                """
            ).fetchone()
            if (
                marker is None
                or int(marker["write_uncertain"]) != 1
                or marker["write_owner_token"] != owner_token
            ):
                raise HostMigrationStateError(
                    "asset_write_gate_unavailable"
                )
            self._connection = connection
            self._owner_token = owner_token
            return self
        except HostMigrationStateError:
            connection.rollback()
            connection.close()
            self._maintenance_scope.__exit__(None, None, None)
            self._maintenance_scope = None
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            connection.rollback()
            connection.close()
            self._maintenance_scope.__exit__(None, None, None)
            self._maintenance_scope = None
            raise HostMigrationStateError(
                "asset_write_gate_unavailable"
            ) from exc
        except BaseException:
            connection.rollback()
            connection.close()
            self._maintenance_scope.__exit__(None, None, None)
            self._maintenance_scope = None
            raise

    def mark_changed(self) -> None:
        self._set_outcome("changed")

    def mark_unchanged(self) -> None:
        self._set_outcome("unchanged")

    def mark_rolled_back(self) -> None:
        self._set_outcome("rolled_back")

    def _set_outcome(self, outcome: str) -> None:
        if (
            self._connection is None
            or self._owner_token is None
            or (
                self._outcome is not None
                and self._outcome != outcome
            )
        ):
            raise HostMigrationStateError("asset_write_gate_unavailable")
        self._outcome = outcome

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        connection = self._connection
        owner_token = self._owner_token
        outcome = self._outcome
        self._connection = None
        self._owner_token = None
        if connection is None or owner_token is None:
            return
        try:
            if outcome is None:
                connection.rollback()
                if exc_type is None:
                    raise HostMigrationStateError(
                        "asset_write_gate_unavailable"
                    )
                return
            self._state._finalize_write(
                connection,
                owner_token,
                outcome,
            )
            connection.commit()
        except HostMigrationStateError:
            connection.rollback()
            raise
        except (OSError, sqlite3.Error) as exc:
            connection.rollback()
            raise HostMigrationStateError(
                "asset_write_gate_unavailable"
            ) from exc
        finally:
            try:
                connection.close()
            finally:
                if self._maintenance_scope is not None:
                    self._maintenance_scope.__exit__(exc_type, exc_value, traceback)
                    self._maintenance_scope = None


def canonical_path_identity(path: str | Path) -> str:
    """Return a stable, non-reversible identity for one canonical local path."""
    try:
        canonical = os.path.normcase(str(Path(path).resolve(strict=False)))
    except (OSError, RuntimeError, TypeError) as exc:
        raise HostMigrationStateError("migration_identity_invalid") from exc
    return "path-sha256:" + hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()


class HostMigrationState:
    """Own migration.sqlite3, its lease, generation, and checkpoint."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        legacy_root: str | Path,
        target_root: str | Path,
        clock: Callable[[], datetime] | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        write_coordinator=None,
    ) -> None:
        self.write_coordinator = write_coordinator or DEFAULT_WRITE_COORDINATOR
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or not 1 <= busy_timeout_ms <= 60_000
        ):
            raise HostMigrationStateError("migration_busy_timeout_invalid")
        self.db_path = Path(db_path).resolve(strict=False)
        self.legacy_root = Path(legacy_root).resolve(strict=False)
        self.target_root = Path(target_root).resolve(strict=False)
        self.source_identity = canonical_path_identity(self.legacy_root)
        self.target_identity = canonical_path_identity(self.target_root)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._busy_timeout_ms = busy_timeout_ms
        self._validate_paths()
        self._initialize()

    def _validate_paths(self) -> None:
        if (
            self.legacy_root == self.target_root
            or _is_within(self.legacy_root, self.target_root)
            or _is_within(self.target_root, self.legacy_root)
        ):
            raise HostMigrationStateError("migration_roots_overlap")
        for content_root in (
            self.legacy_root / "assets",
            self.target_root / "assets",
        ):
            if _is_within(content_root, self.db_path):
                raise HostMigrationStateError(
                    "migration_state_path_unsafe"
                )
        if self.db_path in {
            self.legacy_root / "assets.sqlite3",
            self.target_root / "assets.sqlite3",
        }:
            raise HostMigrationStateError("migration_state_path_unsafe")

    def _now(self) -> datetime:
        try:
            value = self._clock()
        except Exception as exc:
            raise HostMigrationStateError("migration_clock_unavailable") from exc
        if not isinstance(value, datetime):
            raise HostMigrationStateError("migration_clock_unavailable")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _timestamp(self, value: datetime | None = None) -> str:
        return (value or self._now()).isoformat(timespec="microseconds")

    def _is_future(self, value: str) -> bool:
        return _parse_timestamp(value) > self._now()

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self.db_path,
                timeout=self._busy_timeout_ms / 1000,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                f"PRAGMA busy_timeout = {self._busy_timeout_ms}"
            )
            return connection
        except (OSError, sqlite3.Error) as exc:
            raise HostMigrationStateError(
                "migration_state_unavailable"
            ) from exc

    @staticmethod
    def _assert_runtime_schema(connection: sqlite3.Connection) -> None:
        integrity = connection.execute("PRAGMA quick_check(1)").fetchone()
        row = connection.execute(
            """
            SELECT schema_version
            FROM migration_schema
            WHERE singleton = 1
            """
        ).fetchone()
        if integrity is None or integrity[0] != "ok" or row is None:
            raise HostMigrationStateError("migration_state_unavailable")
        if row["schema_version"] != MIGRATION_SCHEMA_VERSION:
            raise HostMigrationStateError("migration_schema_incompatible")
        meta = connection.execute(
            """
            SELECT legacy_generation, write_uncertain,
                   write_owner_token, write_started_at
            FROM migration_meta
            WHERE singleton = 1
            """
        ).fetchone()
        if meta is None or int(meta["legacy_generation"]) < 0:
            raise HostMigrationStateError("migration_state_unavailable")
        uncertain = int(meta["write_uncertain"])
        owner = meta["write_owner_token"]
        started_at = meta["write_started_at"]
        if (
            uncertain not in {0, 1}
            or (
                uncertain == 0
                and (owner is not None or started_at is not None)
            )
            or (
                uncertain == 1
                and (
                    not isinstance(owner, str)
                    or not 32 <= len(owner) <= 256
                    or not isinstance(started_at, str)
                )
            )
        ):
            raise HostMigrationStateError("migration_state_unavailable")
        if uncertain == 1:
            _parse_timestamp(started_at)

    def _initialize(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS migration_schema (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        schema_version INTEGER NOT NULL
                    )
                    """
                )
                row = connection.execute(
                    """
                    SELECT schema_version
                    FROM migration_schema
                    WHERE singleton = 1
                    """
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO migration_schema (singleton, schema_version)
                        VALUES (1, ?)
                        """,
                        (MIGRATION_SCHEMA_VERSION,),
                    )
                elif row["schema_version"] != MIGRATION_SCHEMA_VERSION:
                    raise HostMigrationStateError(
                        "migration_schema_incompatible"
                    )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS migration_meta (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        legacy_generation INTEGER NOT NULL
                            CHECK (legacy_generation >= 0),
                        write_uncertain INTEGER NOT NULL
                            CHECK (write_uncertain IN (0, 1)),
                        write_owner_token TEXT,
                        write_started_at TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO migration_meta (
                        singleton, legacy_generation, write_uncertain,
                        write_owner_token, write_started_at
                    ) VALUES (1, 0, 0, NULL, NULL)
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS freeze_lease (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        owner_token TEXT NOT NULL,
                        acquired_at TEXT NOT NULL,
                        renewed_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS migration_checkpoints (
                        migration_key TEXT PRIMARY KEY,
                        migration_version INTEGER NOT NULL,
                        source_identity TEXT NOT NULL,
                        target_identity TEXT NOT NULL,
                        snapshot_generation INTEGER NOT NULL,
                        upper_bound_asset_id TEXT,
                        last_completed_asset_id TEXT,
                        status TEXT NOT NULL,
                        initial_asset_count INTEGER NOT NULL,
                        processed_count INTEGER NOT NULL,
                        imported_count INTEGER NOT NULL,
                        skipped_idempotent_count INTEGER NOT NULL,
                        blocked_asset_id TEXT,
                        error_code TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT
                    )
                    """
                )
                self._assert_runtime_schema(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        except HostMigrationStateError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise HostMigrationStateError(
                "migration_state_unavailable"
            ) from exc

    def validate_legacy_root(self, root: str | Path) -> bool:
        try:
            return Path(root).resolve(strict=False) == self.legacy_root
        except (OSError, RuntimeError, TypeError):
            return False

    def write_guard(self) -> AssetWriteGuard:
        return AssetWriteGuard(self)

    def _finalize_write(
        self,
        connection: sqlite3.Connection,
        owner_token: str,
        outcome: str,
    ) -> None:
        """Finalize one guarded write without clearing uncertainty on failure."""
        if outcome not in {"changed", "unchanged", "rolled_back"}:
            raise HostMigrationStateError(
                "asset_write_gate_unavailable"
            )
        cursor = connection.execute(
            """
            UPDATE migration_meta
            SET legacy_generation = legacy_generation + ?,
                write_uncertain = 0,
                write_owner_token = NULL,
                write_started_at = NULL
            WHERE singleton = 1
              AND write_uncertain = 1
              AND write_owner_token = ?
            """,
            (1 if outcome == "changed" else 0, owner_token),
        )
        if cursor.rowcount != 1:
            raise HostMigrationStateError(
                "asset_write_gate_unavailable"
            )

    def current_generation(self) -> int:
        try:
            with self._connect() as connection:
                self._assert_runtime_schema(connection)
                row = connection.execute(
                    """
                    SELECT legacy_generation, write_uncertain
                    FROM migration_meta
                    WHERE singleton = 1
                    """
                ).fetchone()
            if row is None:
                raise HostMigrationStateError(
                    "migration_state_unavailable"
                )
            if int(row["write_uncertain"]) != 0:
                raise HostMigrationStateError(
                    "source_generation_uncertain"
                )
            return int(row["legacy_generation"])
        except HostMigrationStateError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise HostMigrationStateError(
                "migration_state_unavailable"
            ) from exc

    @guarded_mutation("migration_freeze_acquire")
    def acquire_freeze(
        self,
        *,
        ttl_seconds: int,
        owner_token: str | None = None,
    ) -> str:
        ttl = _validate_ttl(ttl_seconds)
        owner = owner_token or secrets.token_hex(32)
        _validate_owner(owner)
        now = self._now()
        expires = now + timedelta(seconds=ttl)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_runtime_schema(connection)
            meta = connection.execute(
                """
                SELECT write_uncertain
                FROM migration_meta
                WHERE singleton = 1
                """
            ).fetchone()
            if meta is None:
                raise HostMigrationStateError(
                    "migration_state_unavailable"
                )
            if int(meta["write_uncertain"]) != 0:
                raise HostMigrationStateError(
                    "source_generation_uncertain"
                )
            row = connection.execute(
                """
                SELECT owner_token, expires_at
                FROM freeze_lease
                WHERE singleton = 1
                """
            ).fetchone()
            if row is not None and _parse_timestamp(row["expires_at"]) > now:
                raise HostMigrationStateError("migration_freeze_busy")
            connection.execute(
                """
                INSERT INTO freeze_lease (
                    singleton, owner_token, acquired_at, renewed_at, expires_at
                ) VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    owner_token = excluded.owner_token,
                    acquired_at = excluded.acquired_at,
                    renewed_at = excluded.renewed_at,
                    expires_at = excluded.expires_at
                """,
                (
                    owner,
                    self._timestamp(now),
                    self._timestamp(now),
                    self._timestamp(expires),
                ),
            )
            connection.commit()
            return owner
        except HostMigrationStateError:
            connection.rollback()
            raise
        except sqlite3.OperationalError as exc:
            connection.rollback()
            if "locked" in str(exc).casefold():
                raise HostMigrationStateError(
                    "migration_freeze_busy"
                ) from exc
            raise HostMigrationStateError(
                "migration_state_unavailable"
            ) from exc
        except (OSError, sqlite3.Error, ValueError) as exc:
            connection.rollback()
            raise HostMigrationStateError(
                "migration_state_unavailable"
            ) from exc
        finally:
            connection.close()

    @guarded_mutation("migration_freeze_renew")
    def renew_freeze(self, owner_token: str, *, ttl_seconds: int) -> None:
        ttl = _validate_ttl(ttl_seconds)
        _validate_owner(owner_token)
        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_runtime_schema(connection)
            row = connection.execute(
                """
                SELECT owner_token, expires_at
                FROM freeze_lease
                WHERE singleton = 1
                """
            ).fetchone()
            if (
                row is None
                or row["owner_token"] != owner_token
                or _parse_timestamp(row["expires_at"]) <= now
            ):
                raise HostMigrationStateError("migration_freeze_lost")
            connection.execute(
                """
                UPDATE freeze_lease
                SET renewed_at = ?, expires_at = ?
                WHERE singleton = 1 AND owner_token = ?
                """,
                (
                    self._timestamp(now),
                    self._timestamp(now + timedelta(seconds=ttl)),
                    owner_token,
                ),
            )
            connection.commit()
        except HostMigrationStateError:
            connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            connection.rollback()
            raise HostMigrationStateError(
                "migration_state_unavailable"
            ) from exc
        finally:
            connection.close()

    def assert_freeze_owner(self, owner_token: str) -> None:
        _validate_owner(owner_token)
        try:
            with self._connect() as connection:
                self._assert_runtime_schema(connection)
                row = connection.execute(
                    """
                    SELECT owner_token, expires_at
                    FROM freeze_lease
                    WHERE singleton = 1
                    """
                ).fetchone()
            if (
                row is None
                or row["owner_token"] != owner_token
                or not self._is_future(row["expires_at"])
            ):
                raise HostMigrationStateError("migration_freeze_lost")
        except HostMigrationStateError:
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise HostMigrationStateError(
                "migration_state_unavailable"
            ) from exc

    @guarded_mutation("migration_freeze_release")
    def release_freeze(self, owner_token: str) -> bool:
        _validate_owner(owner_token)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_runtime_schema(connection)
            row = connection.execute(
                """
                SELECT owner_token
                FROM freeze_lease
                WHERE singleton = 1
                """
            ).fetchone()
            if row is None or row["owner_token"] != owner_token:
                connection.rollback()
                return False
            connection.execute(
                """
                DELETE FROM freeze_lease
                WHERE singleton = 1 AND owner_token = ?
                """,
                (owner_token,),
            )
            connection.commit()
            return True
        except (OSError, sqlite3.Error) as exc:
            connection.rollback()
            raise HostMigrationStateError(
                "migration_state_unavailable"
            ) from exc
        finally:
            connection.close()

    def get_checkpoint(self, migration_key: str) -> MigrationCheckpoint | None:
        _validate_migration_key(migration_key)
        try:
            with self._connect() as connection:
                self._assert_runtime_schema(connection)
                row = connection.execute(
                    """
                    SELECT *
                    FROM migration_checkpoints
                    WHERE migration_key = ?
                    """,
                    (migration_key,),
                ).fetchone()
            return _checkpoint_from_row(row) if row is not None else None
        except HostMigrationStateError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise HostMigrationStateError(
                "migration_state_unavailable"
            ) from exc

    def inspect(self, migration_key: str) -> MigrationStateInspection:
        """Return a non-mutating recovery snapshot without ownership secrets."""
        _validate_migration_key(migration_key)
        try:
            now = self._now()
            with self._connect() as connection:
                connection.execute("BEGIN")
                self._assert_runtime_schema(connection)
                return _inspect_connection(
                    connection,
                    migration_key=migration_key,
                    now=now,
                )
        except HostMigrationStateError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise HostMigrationStateError(
                "migration_state_unavailable"
            ) from exc

    @guarded_mutation("migration_checkpoint_create")
    def create_checkpoint(
        self,
        *,
        owner_token: str,
        migration_key: str,
        migration_version: int,
        source_identity: str,
        target_identity: str,
        snapshot_generation: int,
        upper_bound_asset_id: str | None,
        initial_asset_count: int,
    ) -> MigrationCheckpoint:
        _validate_checkpoint_inputs(
            migration_key=migration_key,
            migration_version=migration_version,
            source_identity=source_identity,
            target_identity=target_identity,
            snapshot_generation=snapshot_generation,
            upper_bound_asset_id=upper_bound_asset_id,
            initial_asset_count=initial_asset_count,
        )
        now = self._timestamp()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_runtime_schema(connection)
            self._assert_owner_in_transaction(connection, owner_token)
            generation = self._generation_in_transaction(connection)
            if generation != snapshot_generation:
                raise HostMigrationStateError(
                    "source_changed_since_checkpoint"
                )
            connection.execute(
                """
                INSERT INTO migration_checkpoints (
                    migration_key, migration_version, source_identity,
                    target_identity, snapshot_generation,
                    upper_bound_asset_id, last_completed_asset_id,
                    status, initial_asset_count, processed_count,
                    imported_count, skipped_idempotent_count,
                    blocked_asset_id, error_code, created_at, updated_at,
                    completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, 'ready', ?, 0, 0, 0,
                          NULL, NULL, ?, ?, NULL)
                """,
                (
                    migration_key,
                    migration_version,
                    source_identity,
                    target_identity,
                    snapshot_generation,
                    upper_bound_asset_id,
                    initial_asset_count,
                    now,
                    now,
                ),
            )
            connection.commit()
        except HostMigrationStateError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise HostMigrationStateError(
                "migration_checkpoint_conflict"
            ) from exc
        except (OSError, sqlite3.Error, ValueError) as exc:
            connection.rollback()
            raise HostMigrationStateError(
                "migration_state_unavailable"
            ) from exc
        finally:
            connection.close()
        checkpoint = self.get_checkpoint(migration_key)
        if checkpoint is None:
            raise HostMigrationStateError("migration_state_unavailable")
        return checkpoint

    @guarded_mutation("migration_checkpoint_status")
    def set_checkpoint_status(
        self,
        *,
        owner_token: str,
        migration_key: str,
        status: str,
        error_code: str | None = None,
        blocked_asset_id: str | None = None,
        require_generation: int | None = None,
    ) -> MigrationCheckpoint:
        if status not in {
            "ready",
            "running",
            "paused",
            "blocked",
            "failed",
            "completed",
        }:
            raise HostMigrationStateError("migration_status_invalid")
        if blocked_asset_id is not None:
            _validate_asset_id(blocked_asset_id)
        _validate_error_code(error_code)
        if status in {"blocked", "failed"} and error_code is None:
            raise HostMigrationStateError("migration_error_code_invalid")
        if (
            status in {"ready", "running", "paused", "completed"}
            and (blocked_asset_id is not None or error_code is not None)
        ):
            raise HostMigrationStateError(
                "migration_checkpoint_invariant"
            )
        now = self._timestamp()
        completed_at = now if status == "completed" else None
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_runtime_schema(connection)
            self._assert_owner_in_transaction(connection, owner_token)
            current = connection.execute(
                """
                SELECT status
                FROM migration_checkpoints
                WHERE migration_key = ?
                """,
                (migration_key,),
            ).fetchone()
            if current is None:
                raise HostMigrationStateError(
                    "migration_checkpoint_missing"
                )
            current_status = current["status"]
            if (
                current_status not in _CHECKPOINT_TRANSITIONS
                or status not in _CHECKPOINT_TRANSITIONS[current_status]
            ):
                raise HostMigrationStateError(
                    "migration_status_transition_invalid"
                )
            if (
                require_generation is not None
                and self._generation_in_transaction(connection)
                != require_generation
            ):
                raise HostMigrationStateError(
                    "source_changed_since_checkpoint"
                )
            cursor = connection.execute(
                """
                UPDATE migration_checkpoints
                SET status = ?, blocked_asset_id = ?, error_code = ?,
                    updated_at = ?, completed_at = ?
                WHERE migration_key = ?
                """,
                (
                    status,
                    blocked_asset_id,
                    error_code,
                    now,
                    completed_at,
                    migration_key,
                ),
            )
            if cursor.rowcount != 1:
                raise HostMigrationStateError(
                    "migration_checkpoint_missing"
                )
            connection.commit()
        except HostMigrationStateError:
            connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            connection.rollback()
            raise HostMigrationStateError(
                "migration_state_unavailable"
            ) from exc
        finally:
            connection.close()
        checkpoint = self.get_checkpoint(migration_key)
        if checkpoint is None:
            raise HostMigrationStateError("migration_checkpoint_missing")
        return checkpoint

    @guarded_mutation("migration_checkpoint_progress")
    def record_asset_success(
        self,
        *,
        owner_token: str,
        migration_key: str,
        snapshot_generation: int,
        asset_id: str,
        imported: bool,
    ) -> MigrationCheckpoint:
        _validate_asset_id(asset_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_runtime_schema(connection)
            self._assert_owner_in_transaction(connection, owner_token)
            if (
                self._generation_in_transaction(connection)
                != snapshot_generation
            ):
                raise HostMigrationStateError(
                    "source_changed_since_checkpoint"
                )
            row = connection.execute(
                """
                SELECT last_completed_asset_id, upper_bound_asset_id, status
                FROM migration_checkpoints
                WHERE migration_key = ?
                """,
                (migration_key,),
            ).fetchone()
            if row is None:
                raise HostMigrationStateError(
                    "migration_checkpoint_missing"
                )
            if row["status"] != "running":
                raise HostMigrationStateError(
                    "migration_status_transition_invalid"
                )
            last_completed = row["last_completed_asset_id"]
            upper_bound = row["upper_bound_asset_id"]
            if (
                (last_completed is not None and asset_id <= last_completed)
                or (upper_bound is not None and asset_id > upper_bound)
            ):
                raise HostMigrationStateError(
                    "migration_checkpoint_invariant"
                )
            now = self._timestamp()
            cursor = connection.execute(
                """
                UPDATE migration_checkpoints
                SET last_completed_asset_id = ?,
                    processed_count = processed_count + 1,
                    imported_count = imported_count + ?,
                    skipped_idempotent_count =
                        skipped_idempotent_count + ?,
                    status = 'running',
                    blocked_asset_id = NULL,
                    error_code = NULL,
                    updated_at = ?
                WHERE migration_key = ?
                """,
                (
                    asset_id,
                    1 if imported else 0,
                    0 if imported else 1,
                    now,
                    migration_key,
                ),
            )
            if cursor.rowcount != 1:
                raise HostMigrationStateError(
                    "migration_checkpoint_missing"
                )
            connection.commit()
        except HostMigrationStateError:
            connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            connection.rollback()
            raise HostMigrationStateError(
                "migration_state_unavailable"
            ) from exc
        finally:
            connection.close()
        checkpoint = self.get_checkpoint(migration_key)
        if checkpoint is None:
            raise HostMigrationStateError("migration_checkpoint_missing")
        return checkpoint

    def _assert_owner_in_transaction(
        self,
        connection: sqlite3.Connection,
        owner_token: str,
    ) -> None:
        _validate_owner(owner_token)
        row = connection.execute(
            """
            SELECT owner_token, expires_at
            FROM freeze_lease
            WHERE singleton = 1
            """
        ).fetchone()
        if (
            row is None
            or row["owner_token"] != owner_token
            or not self._is_future(row["expires_at"])
        ):
            raise HostMigrationStateError("migration_freeze_lost")

    @staticmethod
    def _generation_in_transaction(
        connection: sqlite3.Connection,
    ) -> int:
        row = connection.execute(
            """
            SELECT legacy_generation, write_uncertain
            FROM migration_meta
            WHERE singleton = 1
            """
        ).fetchone()
        if row is None:
            raise HostMigrationStateError("migration_state_unavailable")
        if int(row["write_uncertain"]) != 0:
            raise HostMigrationStateError(
                "source_generation_uncertain"
            )
        return int(row["legacy_generation"])


def inspect_existing_migration_state(
    db_path: str | Path,
    *,
    migration_key: str,
    clock: Callable[[], datetime] | None = None,
) -> MigrationStateInspection | None:
    """Inspect an existing migration DB in read-only mode without creating it."""
    _validate_migration_key(migration_key)
    try:
        path = Path(db_path).resolve(strict=False)
        if not path.is_file():
            return None
        now = (clock or (lambda: datetime.now(timezone.utc)))()
        if not isinstance(now, datetime):
            raise HostMigrationStateError("migration_clock_unavailable")
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN")
            HostMigrationState._assert_runtime_schema(connection)
            return _inspect_connection(
                connection,
                migration_key=migration_key,
                now=now.astimezone(timezone.utc),
            )
        finally:
            connection.close()
    except HostMigrationStateError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise HostMigrationStateError("migration_state_unavailable") from exc


def _inspect_connection(
    connection: sqlite3.Connection,
    *,
    migration_key: str,
    now: datetime,
) -> MigrationStateInspection:
    meta = connection.execute(
        """
        SELECT legacy_generation, write_uncertain
        FROM migration_meta
        WHERE singleton = 1
        """
    ).fetchone()
    lease = connection.execute(
        """
        SELECT expires_at
        FROM freeze_lease
        WHERE singleton = 1
        """
    ).fetchone()
    row = connection.execute(
        """
        SELECT *
        FROM migration_checkpoints
        WHERE migration_key = ?
        """,
        (migration_key,),
    ).fetchone()
    if meta is None:
        raise HostMigrationStateError("migration_state_unavailable")
    lease_state = "none"
    if lease is not None:
        lease_state = (
            "active"
            if _parse_timestamp(lease["expires_at"]) > now
            else "expired"
        )
    return MigrationStateInspection(
        generation=int(meta["legacy_generation"]),
        write_uncertain=bool(meta["write_uncertain"]),
        lease_state=lease_state,
        checkpoint=_checkpoint_from_row(row) if row is not None else None,
    )


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise HostMigrationStateError("migration_state_unavailable") from exc
    if parsed.tzinfo is None:
        raise HostMigrationStateError("migration_state_unavailable")
    return parsed.astimezone(timezone.utc)


def _validate_ttl(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 86_400:
        raise HostMigrationStateError("migration_freeze_ttl_invalid")
    return value


def _validate_owner(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) < 32
        or len(value) > 256
        or any(char.isspace() for char in value)
    ):
        raise HostMigrationStateError("migration_owner_invalid")


def _validate_asset_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != _ASSET_ID_LENGTH
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise HostMigrationStateError("migration_asset_id_invalid")


def _validate_migration_key(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise HostMigrationStateError("migration_key_invalid")


def _validate_error_code(value: str | None) -> None:
    if value is None:
        return
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789_"
            for char in value
        )
    ):
        raise HostMigrationStateError("migration_error_code_invalid")


def _validate_checkpoint_inputs(
    *,
    migration_key: str,
    migration_version: int,
    source_identity: str,
    target_identity: str,
    snapshot_generation: int,
    upper_bound_asset_id: str | None,
    initial_asset_count: int,
) -> None:
    _validate_migration_key(migration_key)
    if (
        isinstance(migration_version, bool)
        or not isinstance(migration_version, int)
        or migration_version < 1
    ):
        raise HostMigrationStateError("migration_version_invalid")
    if (
        not isinstance(source_identity, str)
        or not source_identity
        or not isinstance(target_identity, str)
        or not target_identity
        or source_identity == target_identity
    ):
        raise HostMigrationStateError("migration_identity_invalid")
    _validate_path_identity(source_identity)
    _validate_path_identity(target_identity)
    if (
        isinstance(snapshot_generation, bool)
        or not isinstance(snapshot_generation, int)
        or snapshot_generation < 0
        or isinstance(initial_asset_count, bool)
        or not isinstance(initial_asset_count, int)
        or initial_asset_count < 0
    ):
        raise HostMigrationStateError("migration_checkpoint_invalid")
    if upper_bound_asset_id is not None:
        _validate_asset_id(upper_bound_asset_id)


def _checkpoint_from_row(row: sqlite3.Row) -> MigrationCheckpoint:
    try:
        checkpoint = MigrationCheckpoint(
            migration_key=row["migration_key"],
            migration_version=int(row["migration_version"]),
            source_identity=row["source_identity"],
            target_identity=row["target_identity"],
            snapshot_generation=int(row["snapshot_generation"]),
            upper_bound_asset_id=row["upper_bound_asset_id"],
            last_completed_asset_id=row["last_completed_asset_id"],
            status=row["status"],
            initial_asset_count=int(row["initial_asset_count"]),
            processed_count=int(row["processed_count"]),
            imported_count=int(row["imported_count"]),
            skipped_idempotent_count=int(row["skipped_idempotent_count"]),
            blocked_asset_id=row["blocked_asset_id"],
            error_code=row["error_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )
        _validate_migration_key(checkpoint.migration_key)
        for identity in (
            checkpoint.source_identity,
            checkpoint.target_identity,
        ):
            _validate_path_identity(identity)
        for asset_id in (
            checkpoint.upper_bound_asset_id,
            checkpoint.last_completed_asset_id,
            checkpoint.blocked_asset_id,
        ):
            if asset_id is not None:
                _validate_asset_id(asset_id)
        _validate_error_code(checkpoint.error_code)
        if (
            checkpoint.migration_version < 1
            or checkpoint.snapshot_generation < 0
            or checkpoint.status not in _CHECKPOINT_TRANSITIONS
            or min(
                checkpoint.initial_asset_count,
                checkpoint.processed_count,
                checkpoint.imported_count,
                checkpoint.skipped_idempotent_count,
            ) < 0
            or checkpoint.processed_count
            != (
                checkpoint.imported_count
                + checkpoint.skipped_idempotent_count
            )
            or (
                checkpoint.status in {"blocked", "failed"}
                and checkpoint.error_code is None
            )
            or (
                checkpoint.status
                not in {"blocked", "failed"}
                and (
                    checkpoint.blocked_asset_id is not None
                    or checkpoint.error_code is not None
                )
            )
            or (
                checkpoint.status == "completed"
                and checkpoint.completed_at is None
            )
            or (
                checkpoint.status != "completed"
                and checkpoint.completed_at is not None
            )
        ):
            raise HostMigrationStateError(
                "migration_checkpoint_invalid"
            )
        _parse_timestamp(checkpoint.created_at)
        _parse_timestamp(checkpoint.updated_at)
        if checkpoint.completed_at is not None:
            _parse_timestamp(checkpoint.completed_at)
        return checkpoint
    except HostMigrationStateError:
        raise
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise HostMigrationStateError(
            "migration_checkpoint_invalid"
        ) from exc


def _validate_path_identity(value: str) -> None:
    prefix = "path-sha256:"
    if (
        not isinstance(value, str)
        or not value.startswith(prefix)
        or len(value) != len(prefix) + 64
        or any(
            char not in "0123456789abcdef"
            for char in value[len(prefix):]
        )
    ):
        raise HostMigrationStateError("migration_identity_invalid")


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(
            root.resolve(strict=False)
        )
        return True
    except (OSError, RuntimeError, ValueError):
        return False


__all__ = [
    "AssetWriteGuard",
    "HostMigrationState",
    "HostMigrationStateError",
    "MIGRATION_SCHEMA_VERSION",
    "MigrationCheckpoint",
    "canonical_path_identity",
]
