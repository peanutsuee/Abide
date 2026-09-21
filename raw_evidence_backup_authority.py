"""Metadata-only O5E backup repository and restore-revocation authority."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import secrets
import sqlite3
from typing import Any

from maintenance_write_gate import guarded_mutation
from raw_evidence_store import RawEvidenceError


BACKUP_ROOT_ENV = "OMBRE_RAW_EVIDENCE_BACKUP_ROOT"
BACKUP_RETENTION_ENV = "OMBRE_RAW_EVIDENCE_BACKUP_RETENTION_DAYS"
DEFAULT_BACKUP_RETENTION_DAYS = 7
MIN_BACKUP_RETENTION_DAYS = 1
MAX_BACKUP_RETENTION_DAYS = 30
AUTHORITY_SCHEMA_VERSION = 1
_ID_PATTERN = r"^[0-9a-f]{32}$"


class BackupAuthorityError(RawEvidenceError):
    """Stable, content-free backup authority failure."""


@dataclass(frozen=True)
class BackupRetention:
    days: int = DEFAULT_BACKUP_RETENTION_DAYS

    def validate(self) -> None:
        if (
            isinstance(self.days, bool)
            or not isinstance(self.days, int)
            or not MIN_BACKUP_RETENTION_DAYS <= self.days <= MAX_BACKUP_RETENTION_DAYS
        ):
            raise BackupAuthorityError("backup_retention_invalid")

    @classmethod
    def from_env(cls) -> "BackupRetention":
        raw = os.environ.get(BACKUP_RETENTION_ENV)
        if raw in (None, ""):
            result = cls()
        elif not raw.isdecimal():
            raise BackupAuthorityError("backup_retention_invalid")
        else:
            result = cls(int(raw))
        result.validate()
        return result


class RawEvidenceBackupAuthority:
    """Own the backup repository identity, catalog, and restore epoch."""

    def __init__(self, root: Path, *, live_root: Path | None = None):
        self.root = root
        self.live_root = live_root
        self.database_path = root / "authority.sqlite3"
        self.bundles_root = root / "bundles"
        self.staging_root = root / "staging"
        self.temp_root = root / "tmp"
        self._initialize()

    @classmethod
    def open(
        cls,
        root: str | Path,
        *,
        live_root: str | Path | None = None,
        forbidden_roots: tuple[str | Path, ...] = (),
    ) -> "RawEvidenceBackupAuthority":
        candidate = _validate_root(root, forbidden_roots, live_root)
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            for child in (candidate / "bundles", candidate / "staging", candidate / "tmp"):
                child.mkdir(exist_ok=True)
                _validate_owned(child, candidate)
        except OSError as exc:
            raise BackupAuthorityError("backup_repository_unavailable") from exc
        return cls(candidate, live_root=_canonical(live_root) if live_root else None)

    @classmethod
    def from_env(
        cls,
        *,
        live_root: str | Path | None = None,
        forbidden_roots: tuple[str | Path, ...] = (),
    ) -> "RawEvidenceBackupAuthority":
        value = os.environ.get(BACKUP_ROOT_ENV, "")
        if not value:
            raise BackupAuthorityError("backup_repository_missing")
        return cls.open(value, live_root=live_root, forbidden_roots=forbidden_roots)

    def _initialize(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS authority_schema (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        schema_version INTEGER NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                row = conn.execute(
                    "SELECT schema_version FROM authority_schema WHERE singleton = 1"
                ).fetchone()
                if row is not None and int(row["schema_version"]) != AUTHORITY_SCHEMA_VERSION:
                    raise BackupAuthorityError("backup_authority_schema_unsupported")
                conn.execute(
                    "INSERT OR IGNORE INTO authority_schema "
                    "(singleton, schema_version, updated_at) VALUES (1, ?, ?)",
                    (AUTHORITY_SCHEMA_VERSION, _now()),
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS authority (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        repository_id TEXT NOT NULL,
                        current_restore_epoch INTEGER NOT NULL CHECK (current_restore_epoch >= 0),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS revocations (
                        operation_id TEXT PRIMARY KEY,
                        repository_id TEXT NOT NULL,
                        target_evidence_id TEXT NOT NULL,
                        allocated_epoch INTEGER NOT NULL UNIQUE,
                        state TEXT NOT NULL CHECK (state IN ('pending', 'applied')),
                        reason TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        applied_at TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS backup_catalog (
                        backup_id TEXT PRIMARY KEY,
                        repository_id TEXT NOT NULL,
                        bundle_name TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        restore_epoch INTEGER NOT NULL,
                        format_version INTEGER NOT NULL,
                        encrypted_size_bytes INTEGER NOT NULL CHECK (encrypted_size_bytes >= 0),
                        status TEXT NOT NULL CHECK (
                            status IN ('active', 'revoked', 'expired', 'pruned', 'invalid')
                        ),
                        reason TEXT,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                existing = conn.execute(
                    "SELECT repository_id FROM authority WHERE singleton = 1"
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO authority VALUES (1, ?, 0, ?, ?)",
                        (secrets.token_hex(16), _now(), _now()),
                    )
                conn.commit()
        except BackupAuthorityError:
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise BackupAuthorityError("backup_authority_unavailable") from exc

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(str(self.database_path), timeout=30)
        except (OSError, sqlite3.Error) as exc:
            raise BackupAuthorityError("backup_authority_unavailable") from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def repository_id(self) -> str:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT repository_id FROM authority WHERE singleton = 1"
                ).fetchone()
        except sqlite3.Error as exc:
            raise BackupAuthorityError("backup_authority_corrupt") from exc
        if row is None or not _valid_id(row["repository_id"]):
            raise BackupAuthorityError("backup_authority_corrupt")
        return str(row["repository_id"])

    def current_restore_epoch(self) -> int:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT current_restore_epoch FROM authority WHERE singleton = 1"
                ).fetchone()
        except sqlite3.Error as exc:
            raise BackupAuthorityError("backup_authority_corrupt") from exc
        try:
            epoch = int(row["current_restore_epoch"]) if row is not None else -1
        except (KeyError, TypeError, ValueError) as exc:
            raise BackupAuthorityError("backup_authority_corrupt") from exc
        if epoch < 0:
            raise BackupAuthorityError("backup_authority_corrupt")
        return epoch

    def bind_store(self, store) -> str:
        repository_id = self.repository_id()
        bound = store.backup_repository_id()
        if bound is not None and bound != repository_id:
            raise BackupAuthorityError("backup_repository_mismatch")
        if bound is None:
            store.bind_backup_repository(repository_id)
        return repository_id

    def has_pending_revocation(self) -> bool:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM revocations WHERE state = 'pending' LIMIT 1"
                ).fetchone()
        except sqlite3.Error as exc:
            raise BackupAuthorityError("backup_authority_corrupt") from exc
        return row is not None

    @guarded_mutation("raw_evidence_backup_revocation_begin")
    def begin_revocation(
        self,
        operation_id: str,
        *,
        target_evidence_id: str,
        reason: str,
    ) -> dict[str, Any]:
        if not _valid_id(operation_id) or not _valid_id(target_evidence_id):
            raise BackupAuthorityError("revocation_invalid")
        if not isinstance(reason, str) or not reason or len(reason) > 128 or "\x00" in reason:
            raise BackupAuthorityError("revocation_invalid")
        repository_id = self.repository_id()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM revocations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["target_evidence_id"] != target_evidence_id
                    or existing["reason"] != reason
                ):
                    conn.rollback()
                    raise BackupAuthorityError("revocation_conflict")
                conn.commit()
                return dict(existing)
            pending = conn.execute(
                "SELECT * FROM revocations WHERE target_evidence_id = ? AND state = 'pending'",
                (target_evidence_id,),
            ).fetchone()
            if pending is not None:
                conn.commit()
                return dict(pending)
            current = conn.execute(
                "SELECT current_restore_epoch FROM authority WHERE singleton = 1"
            ).fetchone()
            if current is None:
                conn.rollback()
                raise BackupAuthorityError("backup_authority_corrupt")
            epoch = int(current["current_restore_epoch"]) + 1
            created_at = _now()
            conn.execute(
                "INSERT INTO revocations VALUES (?, ?, ?, ?, 'pending', ?, ?, NULL)",
                (operation_id, repository_id, target_evidence_id, epoch, reason, created_at),
            )
            conn.execute(
                "UPDATE authority SET current_restore_epoch = ?, updated_at = ? WHERE singleton = 1",
                (epoch, created_at),
            )
            conn.commit()
        return {
            "operation_id": operation_id,
            "repository_id": repository_id,
            "target_evidence_id": target_evidence_id,
            "allocated_epoch": epoch,
            "state": "pending",
            "reason": reason,
            "created_at": created_at,
            "applied_at": None,
        }

    @guarded_mutation("raw_evidence_backup_revocation_apply")
    def apply_revocation(self, operation_id: str) -> dict[str, Any]:
        if not _valid_id(operation_id):
            raise BackupAuthorityError("revocation_invalid")
        applied_at = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM revocations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise BackupAuthorityError("revocation_not_found")
            conn.execute(
                "UPDATE revocations SET state = 'applied', applied_at = ? WHERE operation_id = ?",
                (applied_at, operation_id),
            )
            conn.commit()
        return {**dict(row), "state": "applied", "applied_at": applied_at}

    @guarded_mutation("raw_evidence_backup_revocation_reconcile")
    def reconcile_revocation(self, operation_id: str) -> dict[str, Any]:
        """Idempotently finalize a pending operation after live-state retry."""

        return self.apply_revocation(operation_id)

    def pending_revocations(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM revocations WHERE state = 'pending' ORDER BY allocated_epoch"
            ).fetchall()
        return [dict(row) for row in rows]

    @guarded_mutation("raw_evidence_backup_catalog_register")
    def register_bundle(
        self,
        *,
        backup_id: str,
        bundle_name: str,
        created_at: str,
        expires_at: str,
        restore_epoch: int,
        format_version: int,
        encrypted_size_bytes: int,
    ) -> dict[str, Any]:
        if not _valid_id(backup_id) or not bundle_name.endswith(".obrawbackup"):
            raise BackupAuthorityError("backup_catalog_invalid")
        if restore_epoch < 0 or encrypted_size_bytes < 0:
            raise BackupAuthorityError("backup_catalog_invalid")
        repository_id = self.repository_id()
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM revocations WHERE state = 'pending'").fetchone():
                conn.rollback()
                raise BackupAuthorityError("revocation_pending")
            conn.execute(
                """
                INSERT INTO backup_catalog (
                    backup_id, repository_id, bundle_name, created_at, expires_at,
                    restore_epoch, format_version, encrypted_size_bytes, status,
                    reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?)
                """,
                (
                    backup_id, repository_id, bundle_name, created_at, expires_at,
                    restore_epoch, format_version, encrypted_size_bytes, now,
                ),
            )
            conn.commit()
        return self.catalog_entry(backup_id) or {}

    def catalog_entry(self, backup_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM backup_catalog WHERE backup_id = ?", (backup_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def catalog_by_name(self, bundle_name: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM backup_catalog WHERE bundle_name = ?", (bundle_name,)
            ).fetchone()
        return dict(row) if row is not None else None

    def catalog_entries(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM backup_catalog ORDER BY created_at, backup_id"
            ).fetchall()
        return [dict(row) for row in rows]

    @guarded_mutation("raw_evidence_backup_catalog_expire")
    def expire_catalog(self, *, now: str | None = None) -> int:
        current = now or _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE backup_catalog SET status = 'expired', updated_at = ? "
                "WHERE status = 'active' AND expires_at <= ?",
                (current, current),
            )
            conn.commit()
        return int(cursor.rowcount)

    @guarded_mutation("raw_evidence_backup_catalog_prune")
    def mark_pruned(self, backup_id: str, *, reason: str) -> None:
        if not _valid_id(backup_id) or not isinstance(reason, str) or not reason:
            raise BackupAuthorityError("backup_catalog_invalid")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE backup_catalog SET status = 'pruned', reason = ?, updated_at = ? "
                "WHERE backup_id = ? AND status IN ('expired', 'revoked')",
                (reason, _now(), backup_id),
            )
            conn.commit()

    @guarded_mutation("raw_evidence_backup_catalog_revoke")
    def mark_revoked(self, *, current_epoch: int) -> int:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE backup_catalog SET status = 'revoked', reason = 'restore_epoch_revoked', "
                "updated_at = ? WHERE status = 'active' AND restore_epoch < ?",
                (_now(), current_epoch),
            )
            conn.commit()
        return int(cursor.rowcount)

    def integrity_check(self) -> dict[str, Any]:
        with self._connect() as conn:
            quick = conn.execute("PRAGMA quick_check").fetchone()[0]
            foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
        return {"quick_check": quick, "foreign_key_errors": len(foreign)}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: str | Path | None) -> Path:
    if value is None:
        raise BackupAuthorityError("path_invalid")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise BackupAuthorityError("path_invalid")
    if any(
        not _valid_windows_component(part)
        for part in candidate.parts
        if part not in {candidate.anchor, ""}
    ):
        raise BackupAuthorityError("backup_repository_path_invalid")
    if _contains_reparse(candidate):
        raise BackupAuthorityError("backup_repository_path_invalid")
    return candidate.resolve(strict=False)


def _validate_root(
    value: str | Path,
    forbidden_roots: tuple[str | Path, ...],
    live_root: str | Path | None,
) -> Path:
    root = _canonical(value)
    repository = Path(__file__).resolve().parent
    forbidden = [repository, *forbidden_roots]
    if live_root is not None:
        forbidden.append(live_root)
    for item in forbidden:
        other = _canonical(item)
        if _within(root, other) or _within(other, root):
            raise BackupAuthorityError("backup_repository_overlap")
    if _contains_reparse(root):
        raise BackupAuthorityError("backup_repository_path_invalid")
    return root


def _validate_owned(path: Path, root: Path) -> None:
    if not _within(root, path) or _contains_reparse(path):
        raise BackupAuthorityError("backup_repository_path_invalid")


def _contains_reparse(path: Path) -> bool:
    current = Path(path.anchor)
    try:
        relative = path.relative_to(current)
    except ValueError:
        return True
    for part in relative.parts:
        current = current / part
        if not current.exists():
            continue
        try:
            stat_value = current.lstat()
        except OSError:
            return True
        if current.is_symlink() or getattr(stat_value, "st_file_attributes", 0) & 0x400:
            return True
    return False


def _within(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 32 and all(
        character in "0123456789abcdef" for character in value
    )


def _valid_windows_component(value: str) -> bool:
    stripped = value.rstrip(" .")
    if stripped != value or not value:
        return False
    stem = value.split(".", 1)[0].upper()
    return stem not in {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }


__all__ = [
    "AUTHORITY_SCHEMA_VERSION",
    "BACKUP_RETENTION_ENV",
    "BACKUP_ROOT_ENV",
    "BackupAuthorityError",
    "BackupRetention",
    "DEFAULT_BACKUP_RETENTION_DAYS",
    "MAX_BACKUP_RETENTION_DAYS",
    "MIN_BACKUP_RETENTION_DAYS",
    "RawEvidenceBackupAuthority",
]
