"""Isolated Raw Evidence registry and content-addressed storage foundation.

O5A deliberately has no application integration.  A caller must construct a
store with an explicit root before this module creates any filesystem state.
The store records source/evidence identity separately from content identity;
it never reads a caller-provided filesystem path.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import stat
import tempfile
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable

from maintenance_write_gate import DEFAULT_WRITE_COORDINATOR, guarded_mutation


SCHEMA_VERSION = 6
REVISION_SCHEMA_VERSION = 1
RECORD_SCHEMA_VERSION = 1
HASH_ALGORITHM = "sha256-v1"
SPAN_DESCRIPTOR_SCHEMA_VERSION = 1
SPAN_LOCATOR_SCHEMA_VERSION = 1
SPAN_LOCATOR_KIND = "raw_byte_range_v1"
SPAN_HASH_ALGORITHM = "sha256"
LINEAGE_CITATION_SCHEMA_VERSION = 1
SOURCE_SPAN_CANDIDATE_SCHEMA_VERSION = 1
IMPORT_RUN_SCHEMA_VERSION = 1
RETENTION_POLICY_VERSION = "foundation_v1"
DEFAULT_RETENTION_DAYS = 30
BACKUP_CLAIM_DEFAULT_TTL_SECONDS = 300
BACKUP_CLAIM_STATES = frozenset({"active", "released"})

IMPORT_RUN_STATUSES = frozenset(
    {
        "capture_pending",
        "capture_succeeded",
        "processing",
        "paused",
        "failed",
        "completed",
        "needs_reconcile",
    }
)
IMPORT_ITEM_STATUSES = frozenset(
    {
        "capture_pending",
        "capture_succeeded",
        "extraction_pending",
        "extraction_failed",
        "extraction_ready",
        "memory_planned",
        "memory_applying",
        "succeeded",
        "failed",
        "ambiguous",
    }
)
LINEAGE_KINDS = frozenset(
    {"created", "preserve_raw_created", "contributed_update"}
)
LINEAGE_STATUSES = frozenset(
    {
        "pending",
        "complete",
        "source_redacted",
        "source_expired",
        "evidence_missing",
        "integrity_failed",
        "memory_deleted",
        "needs_reconcile",
        "provenance_broken",
    }
)

FIDELITY_LEVELS = frozenset(
    {
        "IMPORT_SNAPSHOT",
        "SOURCE_TEXT",
        "SOURCE_ITEM",
        "EXACT_SPAN",
        "ORIGINAL_BYTES",
    }
)
PRIVACY_CLASSES = frozenset({"ordinary", "sealed", "restricted_admin"})
LIFECYCLE_STATES = frozenset(
    {
        "captured",
        "available",
        "quarantined",
        "tombstoned",
        "expired",
        "purge_pending",
        "purged",
        "integrity_failed",
        "missing",
    }
)
MUTABLE_LIFECYCLE_STATES = frozenset(
    {
        "captured",
        "available",
        "quarantined",
        "integrity_failed",
        "tombstoned",
    }
)
CAS_STATES = frozenset({"publish_pending", "live", "gc_pending", "purged"})
VERIFICATION_STATES = frozenset({"verified", "quarantined", "failed"})
IDENTITY_ORIGINS = frozenset({"upstream", "local", "unknown"})
SPAN_VERIFICATION_STATES = frozenset(
    {"VERIFIED", "UNAVAILABLE", "NOT_VERIFIED", "INVALID", "UNSUPPORTED", "ACCESS_DENIED"}
)

_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_MEMORY_ID_PATTERN = re.compile(r"^[0-9a-f]{12,64}$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RELATIVE_BLOB_PATTERN = re.compile(
    r"^blobs/sha256/[0-9a-f]{2}/[0-9a-f]{64}$"
)
_MAX_READ_CHUNK = 1024 * 1024
_REPARSE_ATTRIBUTE = 0x400


class RawEvidenceError(RuntimeError):
    """Stable, content-free O5A failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RawEvidenceLimits:
    """Internal bounded-write settings; not a public product guarantee."""

    max_evidence_bytes: int = 16 * 1024 * 1024
    max_temp_bytes: int = 16 * 1024 * 1024
    max_metadata_chars: int = 4096
    max_store_bytes: int = 512 * 1024 * 1024

    def validate(self) -> None:
        values = (
            self.max_evidence_bytes,
            self.max_temp_bytes,
            self.max_metadata_chars,
            self.max_store_bytes,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
            raise RawEvidenceError("limits_invalid")


class RawEvidenceStore:
    """A private registry plus CAS store rooted at an explicit directory."""

    def __init__(
        self,
        evidence_root: str | Path | None,
        *,
        enabled: bool = True,
        limits: RawEvidenceLimits | None = None,
        forbidden_roots: Iterable[str | Path] = (),
        write_coordinator=None,
    ) -> None:
        if not isinstance(enabled, bool):
            raise RawEvidenceError("invalid_input")
        self.enabled = enabled
        self.write_coordinator = write_coordinator or DEFAULT_WRITE_COORDINATOR
        self.limits = limits or RawEvidenceLimits()
        self.limits.validate()
        self._lock = threading.RLock()
        self.root: Path | None = None
        self.blobs_root: Path | None = None
        self.temp_root: Path | None = None
        self.quarantine_root: Path | None = None
        self.registry_path: Path | None = None

        # Disabled construction is intentionally inert, including for an
        # invalid or absent path.  O5A has no startup/configuration hook.
        if not enabled:
            return

        self.root = _validate_owned_root(evidence_root, forbidden_roots)
        self.blobs_root = self.root / "blobs" / "sha256"
        self.temp_root = self.root / ".tmp"
        self.quarantine_root = self.root / ".quarantine"
        self.registry_path = self.root / "registry.sqlite3"
        self._prepare_layout()
        self._init_schema()

    @classmethod
    def open(cls, evidence_root: str | Path | None, **kwargs: Any) -> "RawEvidenceStore":
        """Construct an explicitly requested store or disabled handle."""

        return cls(evidence_root, **kwargs)

    def __enter__(self) -> "RawEvidenceStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    @property
    def is_disabled(self) -> bool:
        return not self.enabled

    @property
    def database_path(self) -> Path | None:
        return self.registry_path

    def close(self) -> None:
        """The store uses short-lived SQLite connections and needs no close."""

        return None

    def backup_repository_id(self) -> str | None:
        """Return the bound O5E backup repository identity, if any."""

        self._require_enabled()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT repository_id FROM o5e_coordination WHERE singleton = 1"
                ).fetchone()
        return row["repository_id"] if row is not None else None

    @guarded_mutation("raw_evidence_backup_repository_bind")
    def bind_backup_repository(self, repository_id: str) -> str:
        """Bind the store to one operator-managed backup repository UUID."""

        self._require_enabled()
        if not isinstance(repository_id, str) or _ID_PATTERN.fullmatch(repository_id) is None:
            raise RawEvidenceError("backup_repository_invalid")
        with self._lock:
            with self._connect() as conn:
                self._begin_write(conn)
                row = conn.execute(
                    "SELECT repository_id FROM o5e_coordination WHERE singleton = 1"
                ).fetchone()
                existing = row["repository_id"] if row is not None else None
                if existing is not None and existing != repository_id:
                    conn.rollback()
                    raise RawEvidenceError("backup_repository_mismatch")
                conn.execute(
                    "UPDATE o5e_coordination SET repository_id = ?, updated_at = ? "
                    "WHERE singleton = 1",
                    (repository_id, _now_iso()),
                )
                conn.commit()
        return repository_id

    @guarded_mutation("raw_evidence_backup_claim_acquire")
    def acquire_backup_claim(
        self,
        operation_id: str,
        *,
        owner_class: str = "operator_backup",
        ttl_seconds: int = BACKUP_CLAIM_DEFAULT_TTL_SECONDS,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Acquire the single bounded O5E snapshot fence for this store."""

        self._require_enabled()
        operation_id = _validate_id(operation_id, "backup_operation_id")
        if (
            not isinstance(owner_class, str)
            or not owner_class
            or len(owner_class) > 64
            or not owner_class.replace("_", "").isalnum()
            or isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 1 <= ttl_seconds <= 3600
        ):
            raise RawEvidenceError("backup_claim_invalid")
        started_at = now or _now_iso()
        started = _parse_timestamp(started_at)
        expires_at = (started + timedelta(seconds=ttl_seconds)).isoformat(
            timespec="seconds"
        )
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM o5e_coordination WHERE singleton = 1"
                ).fetchone()
                if row is None:
                    conn.rollback()
                    raise RawEvidenceError("backup_claim_invalid")
                active = row["backup_state"] == "active"
                if active and _parse_timestamp(row["backup_expires_at"]) > started:
                    conn.rollback()
                    raise RawEvidenceError("backup_busy")
                conn.execute(
                    """
                    UPDATE o5e_coordination SET
                        active_backup_operation_id = ?, backup_owner_class = ?,
                        backup_started_at = ?, backup_heartbeat_at = ?,
                        backup_expires_at = ?, backup_state = 'active',
                        updated_at = ?
                    WHERE singleton = 1
                    """,
                    (
                        operation_id, owner_class, started_at, started_at,
                        expires_at, started_at,
                    ),
                )
                conn.commit()
        return {
            "operation_id": operation_id,
            "owner_class": owner_class,
            "started_at": started_at,
            "heartbeat_at": started_at,
            "expires_at": expires_at,
            "state": "active",
        }

    @guarded_mutation("raw_evidence_backup_claim_heartbeat")
    def heartbeat_backup_claim(
        self,
        operation_id: str,
        *,
        ttl_seconds: int = BACKUP_CLAIM_DEFAULT_TTL_SECONDS,
        now: str | None = None,
    ) -> dict[str, Any]:
        operation_id = _validate_id(operation_id, "backup_operation_id")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 3600:
            raise RawEvidenceError("backup_claim_invalid")
        heartbeat_at = now or _now_iso()
        expires_at = (
            _parse_timestamp(heartbeat_at) + timedelta(seconds=ttl_seconds)
        ).isoformat(timespec="seconds")
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM o5e_coordination WHERE singleton = 1"
                ).fetchone()
                if (
                    row is None
                    or row["backup_state"] != "active"
                    or row["active_backup_operation_id"] != operation_id
                    or _parse_timestamp(row["backup_expires_at"]) <= _parse_timestamp(heartbeat_at)
                ):
                    conn.rollback()
                    raise RawEvidenceError("backup_claim_lost")
                conn.execute(
                    "UPDATE o5e_coordination SET backup_heartbeat_at = ?, "
                    "backup_expires_at = ?, updated_at = ? WHERE singleton = 1",
                    (heartbeat_at, expires_at, heartbeat_at),
                )
                conn.commit()
        return {"operation_id": operation_id, "heartbeat_at": heartbeat_at, "expires_at": expires_at}

    @guarded_mutation("raw_evidence_backup_claim_release")
    def release_backup_claim(self, operation_id: str, *, now: str | None = None) -> bool:
        operation_id = _validate_id(operation_id, "backup_operation_id")
        released_at = now or _now_iso()
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """
                    UPDATE o5e_coordination SET
                        active_backup_operation_id = NULL, backup_owner_class = NULL,
                        backup_started_at = NULL, backup_heartbeat_at = NULL,
                        backup_expires_at = NULL, backup_state = 'released',
                        updated_at = ?
                    WHERE singleton = 1 AND backup_state = 'active'
                      AND active_backup_operation_id = ?
                    """,
                    (released_at, operation_id),
                )
                conn.commit()
        return cursor.rowcount == 1

    def assert_backup_claim(self, operation_id: str, *, now: str | None = None) -> None:
        """Fail closed when a bounded backup has lost its registry authority."""

        operation_id = _validate_id(operation_id, "backup_operation_id")
        current = _parse_timestamp(now or _now_iso())
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM o5e_coordination WHERE singleton = 1"
                ).fetchone()
        if (
            row is None
            or row["backup_state"] != "active"
            or row["active_backup_operation_id"] != operation_id
            or _parse_timestamp(row["backup_expires_at"]) <= current
        ):
            raise RawEvidenceError("backup_claim_lost")

    def backup_claim_status(self) -> dict[str, Any] | None:
        self._require_enabled()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM o5e_coordination WHERE singleton = 1"
                ).fetchone()
        if row is None:
            return None
        return dict(row)

    @guarded_mutation("raw_evidence_create")
    def create_evidence(
        self,
        content: bytes | bytearray | memoryview | BinaryIO,
        *,
        source_system: str = "local",
        source_kind: str = "unknown",
        source_scope: str = "local",
        upstream_source_id: str | None = None,
        upstream_item_id: str | None = None,
        source_occurrence_key: str | None = None,
        identity_origin: str = "unknown",
        fidelity_level: str = "IMPORT_SNAPSHOT",
        media_type: str = "application/octet-stream",
        privacy_class: str = "ordinary",
        captured_at: str | None = None,
    ) -> dict[str, Any]:
        """Create one logical evidence object and one immutable revision."""

        self._require_enabled()
        metadata = self._validate_metadata(
            source_system=source_system,
            source_kind=source_kind,
            source_scope=source_scope,
            upstream_source_id=upstream_source_id,
            upstream_item_id=upstream_item_id,
            source_occurrence_key=source_occurrence_key,
            identity_origin=identity_origin,
            fidelity_level=fidelity_level,
            media_type=media_type,
            privacy_class=privacy_class,
            captured_at=captured_at,
        )
        evidence_id = uuid.uuid4().hex
        revision_id = uuid.uuid4().hex
        now = _now_iso()
        temp_path: Path | None = None

        with self._lock:
            try:
                temp_path, content_size, content_hash = self._stage_content(content)
                self._ensure_store_capacity(content_size)
                blob_relpath = self._coordinate_capture(
                    evidence_id=evidence_id,
                    revision_id=revision_id,
                    metadata=metadata,
                    content_hash=content_hash,
                    content_size=content_size,
                    temp_path=temp_path,
                    now=now,
                )
                temp_path = None
            except RawEvidenceError:
                raise
            except sqlite3.Error as exc:
                raise RawEvidenceError("storage_unavailable") from exc
            except OSError as exc:
                raise RawEvidenceError("storage_unavailable") from exc
            finally:
                if temp_path is not None:
                    self._remove_temp(temp_path)

        return self._get_evidence_internal(evidence_id)

    def create(self, content: bytes | bytearray | memoryview | BinaryIO, **kwargs: Any) -> dict[str, Any]:
        """Internal shorthand for :meth:`create_evidence`."""

        return self.create_evidence(content, **kwargs)

    @guarded_mutation("raw_evidence_import_run_create")
    def create_or_get_import_run(
        self,
        *,
        run_id: str,
        retry_key: str,
        source_sha256: str,
        source_size_bytes: int,
        filename: str,
        media_type: str,
        source_system: str,
        source_kind: str,
        source_scope: str,
        actor_id: str,
        preserve_raw: bool,
        importer_version: str,
        parser_version: str,
        chunker_version: str,
        upstream_source_id: str | None = None,
        upstream_item_id: str | None = None,
    ) -> dict[str, Any]:
        """Create one durable O5B run or return the exact existing run."""

        self._require_enabled()
        run_id = _validate_id(run_id, "run_id")
        retry_key = _validate_text(retry_key, "retry_key", self.limits.max_metadata_chars)
        source_sha256 = _validate_hash(source_sha256, "source_sha256")
        if (
            isinstance(source_size_bytes, bool)
            or not isinstance(source_size_bytes, int)
            or source_size_bytes < 0
            or source_size_bytes > self.limits.max_evidence_bytes
        ):
            raise RawEvidenceError("source_size_invalid")
        for name, value in (
            ("filename", filename or "upload"),
            ("media_type", media_type or "application/octet-stream"),
            ("source_system", source_system),
            ("source_kind", source_kind),
            ("source_scope", source_scope),
            ("actor_id", actor_id),
            ("importer_version", importer_version),
            ("parser_version", parser_version),
            ("chunker_version", chunker_version),
        ):
            _validate_text(value, name, self.limits.max_metadata_chars)
        for name, value in (
            ("upstream_source_id", upstream_source_id),
            ("upstream_item_id", upstream_item_id),
        ):
            if value is not None:
                _validate_text(value, name, self.limits.max_metadata_chars)
        if not isinstance(preserve_raw, bool):
            raise RawEvidenceError("preserve_raw_invalid")

        now = _now_iso()
        values = {
            "run_id": run_id,
            "retry_key": retry_key,
            "source_sha256": source_sha256,
            "source_size_bytes": source_size_bytes,
            "filename": filename or "upload",
            "media_type": media_type or "application/octet-stream",
            "source_system": source_system,
            "source_kind": source_kind,
            "source_scope": source_scope,
            "actor_id": actor_id,
            "preserve_raw": 1 if preserve_raw else 0,
            "importer_version": importer_version,
            "parser_version": parser_version,
            "chunker_version": chunker_version,
            "upstream_source_id": upstream_source_id,
            "upstream_item_id": upstream_item_id,
        }
        with self._lock:
            try:
                with self._connect() as conn:
                    self._begin_write(conn)
                    row = conn.execute(
                        "SELECT * FROM import_runs WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()
                    if row is None:
                        duplicate = conn.execute(
                            "SELECT run_id FROM import_runs WHERE retry_key = ?",
                            (retry_key,),
                        ).fetchone()
                        if duplicate is not None:
                            raise RawEvidenceError("idempotency_conflict")
                        conn.execute(
                            """
                            INSERT INTO import_runs (
                                run_id, retry_key, source_sha256, source_size_bytes,
                                capture_mode, fidelity_level, filename, media_type,
                                source_system, source_kind, source_scope,
                                upstream_source_id, upstream_item_id, actor_id,
                                importer_version, parser_version, chunker_version,
                                preserve_raw, status, error_category,
                                total_chunks, processed_chunks, started_at,
                                updated_at, completed_at, retry_count,
                                run_schema_version
                            ) VALUES (?, ?, ?, ?, 'IMPORT_SNAPSHOT', 'IMPORT_SNAPSHOT',
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                'capture_pending', NULL, 0, 0, ?, ?, NULL, 0, ?)
                            """,
                            (
                                run_id,
                                retry_key,
                                source_sha256,
                                source_size_bytes,
                                values["filename"],
                                values["media_type"],
                                values["source_system"],
                                values["source_kind"],
                                values["source_scope"],
                                values["upstream_source_id"],
                                values["upstream_item_id"],
                                values["actor_id"],
                                values["importer_version"],
                                values["parser_version"],
                                values["chunker_version"],
                                values["preserve_raw"],
                                now,
                                now,
                                IMPORT_RUN_SCHEMA_VERSION,
                            ),
                        )
                    else:
                        for field in (
                            "retry_key",
                            "source_sha256",
                            "source_size_bytes",
                            "filename",
                            "media_type",
                            "source_system",
                            "source_kind",
                            "source_scope",
                            "actor_id",
                            "preserve_raw",
                            "importer_version",
                            "parser_version",
                            "chunker_version",
                            "upstream_source_id",
                            "upstream_item_id",
                        ):
                            if row[field] != values[field]:
                                raise RawEvidenceError("idempotency_conflict")
                        conn.execute(
                            """
                            UPDATE import_runs
                            SET retry_count = retry_count + 1, updated_at = ?
                            WHERE run_id = ?
                            """,
                            (now, run_id),
                        )
                    conn.commit()
            except RawEvidenceError:
                raise
            except sqlite3.IntegrityError as exc:
                raise RawEvidenceError("idempotency_conflict") from exc
            except sqlite3.Error as exc:
                raise RawEvidenceError("storage_unavailable") from exc
        return self.get_import_run(run_id)

    def find_resumable_import_run(
        self,
        *,
        source_sha256: str,
        filename: str,
        preserve_raw: bool,
    ) -> dict[str, Any] | None:
        """Find the newest incomplete run for the exact captured source."""

        self._require_enabled()
        source_sha256 = _validate_hash(source_sha256, "source_sha256")
        filename = filename or "upload"
        _validate_text(filename, "filename", self.limits.max_metadata_chars)
        if not isinstance(preserve_raw, bool):
            raise RawEvidenceError("preserve_raw_invalid")
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM import_runs
                    WHERE source_sha256 = ? AND filename = ?
                      AND preserve_raw = ?
                      AND status IN (
                          'capture_pending', 'capture_succeeded', 'processing',
                          'paused', 'failed', 'needs_reconcile'
                      )
                    ORDER BY updated_at DESC, run_id DESC
                    LIMIT 1
                    """,
                    (source_sha256, filename, 1 if preserve_raw else 0),
                ).fetchone()
        return dict(row) if row is not None else None

    def get_import_run(self, run_id: str) -> dict[str, Any]:
        self._require_enabled()
        run_id = _validate_id(run_id, "run_id")
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM import_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
        if row is None:
            raise RawEvidenceError("not_found")
        return dict(row)

    @guarded_mutation("raw_evidence_import_run_update")
    def update_import_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        error_category: str | None = None,
        total_chunks: int | None = None,
        processed_chunks: int | None = None,
    ) -> dict[str, Any]:
        self._require_enabled()
        run_id = _validate_id(run_id, "run_id")
        if status is not None:
            status = _validate_choice(status, IMPORT_RUN_STATUSES, "status")
        if error_category is not None:
            _validate_text(error_category, "error_category", 128)
        for name, value in (
            ("total_chunks", total_chunks),
            ("processed_chunks", processed_chunks),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise RawEvidenceError(f"{name}_invalid")
        assignments = []
        values: list[Any] = []
        if status is not None:
            assignments.append("status = ?")
            values.append(status)
        if error_category is not None:
            assignments.append("error_category = ?")
            values.append(error_category)
        if total_chunks is not None:
            assignments.append("total_chunks = ?")
            values.append(total_chunks)
        if processed_chunks is not None:
            assignments.append("processed_chunks = ?")
            values.append(processed_chunks)
        assignments.append("updated_at = ?")
        values.extend((_now_iso(), run_id))
        with self._lock:
            try:
                with self._connect() as conn:
                    self._begin_write(conn)
                    cursor = conn.execute(
                        f"UPDATE import_runs SET {', '.join(assignments)} WHERE run_id = ?",
                        values,
                    )
                    if cursor.rowcount != 1:
                        conn.rollback()
                        raise RawEvidenceError("not_found")
                    conn.commit()
            except RawEvidenceError:
                raise
            except sqlite3.Error as exc:
                raise RawEvidenceError("storage_unavailable") from exc
        return self.get_import_run(run_id)

    def get_import_item(self, run_id: str, item_key: str) -> dict[str, Any] | None:
        self._require_enabled()
        run_id = _validate_id(run_id, "run_id")
        _validate_text(item_key, "item_key", self.limits.max_metadata_chars)
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM import_run_items WHERE run_id = ? AND item_key = ?",
                    (run_id, item_key),
                ).fetchone()
        return dict(row) if row is not None else None

    def list_import_items(self, run_id: str, *, prefix: str | None = None) -> list[dict[str, Any]]:
        self._require_enabled()
        run_id = _validate_id(run_id, "run_id")
        with self._lock:
            with self._connect() as conn:
                if prefix is None:
                    rows = conn.execute(
                        "SELECT * FROM import_run_items WHERE run_id = ? ORDER BY item_key",
                        (run_id,),
                    ).fetchall()
                else:
                    _validate_text(prefix, "item_prefix", self.limits.max_metadata_chars)
                    rows = conn.execute(
                        """
                        SELECT * FROM import_run_items
                        WHERE run_id = ? AND item_key LIKE ?
                        ORDER BY item_key
                        """,
                        (run_id, f"{prefix}%"),
                    ).fetchall()
        return [dict(row) for row in rows]

    @guarded_mutation("raw_evidence_import_item_update")
    def upsert_import_item(
        self,
        run_id: str,
        item_key: str,
        *,
        item_kind: str,
        input_digest: str,
        status: str,
        evidence_id: str | None = None,
        revision_id: str | None = None,
        operation_key: str | None = None,
        operation_kind: str | None = None,
        target_bucket_id: str | None = None,
        payload_digest: str | None = None,
        result_id: str | None = None,
        item_count: int | None = None,
        error_category: str | None = None,
    ) -> dict[str, Any]:
        self._require_enabled()
        run_id = _validate_id(run_id, "run_id")
        _validate_text(item_key, "item_key", self.limits.max_metadata_chars)
        _validate_text(item_kind, "item_kind", 64)
        input_digest = _validate_hash(input_digest, "input_digest")
        status = _validate_choice(status, IMPORT_ITEM_STATUSES, "status")
        if evidence_id is not None:
            evidence_id = _validate_id(evidence_id, "evidence_id")
        if revision_id is not None:
            revision_id = _validate_id(revision_id, "revision_id")
        for name, value in (
            ("operation_key", operation_key),
            ("operation_kind", operation_kind),
            ("target_bucket_id", target_bucket_id),
            ("result_id", result_id),
        ):
            if value is not None:
                _validate_text(value, name, 256)
        if operation_kind is not None and operation_kind not in {"create", "update"}:
            raise RawEvidenceError("operation_kind_invalid")
        if payload_digest is not None:
            payload_digest = _validate_hash(payload_digest, "payload_digest")
        if item_count is not None and (
            isinstance(item_count, bool) or not isinstance(item_count, int) or item_count < 0
        ):
            raise RawEvidenceError("item_count_invalid")
        if error_category is not None:
            _validate_text(error_category, "error_category", 128)

        now = _now_iso()
        with self._lock:
            try:
                with self._connect() as conn:
                    self._begin_write(conn)
                    existing = conn.execute(
                        "SELECT * FROM import_run_items WHERE run_id = ? AND item_key = ?",
                        (run_id, item_key),
                    ).fetchone()
                    if existing is None:
                        conn.execute(
                            """
                            INSERT INTO import_run_items (
                                run_id, item_key, item_kind, input_digest, status,
                                evidence_id, revision_id, operation_key, operation_kind,
                                target_bucket_id, payload_digest, result_id, item_count,
                                error_category, created_at, updated_at, item_schema_version
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                run_id,
                                item_key,
                                item_kind,
                                input_digest,
                                status,
                                evidence_id,
                                revision_id,
                                operation_key,
                                operation_kind,
                                target_bucket_id,
                                payload_digest,
                                result_id,
                                item_count,
                                error_category,
                                now,
                                now,
                                IMPORT_RUN_SCHEMA_VERSION,
                            ),
                        )
                    else:
                        if existing["input_digest"] != input_digest:
                            raise RawEvidenceError("idempotency_conflict")
                        if (
                            existing["operation_key"] is not None
                            and operation_key is not None
                            and existing["operation_key"] != operation_key
                        ):
                            raise RawEvidenceError("idempotency_conflict")
                        if existing["status"] == "succeeded" and status != "succeeded":
                            conn.commit()
                            return dict(existing)
                        conn.execute(
                            """
                            UPDATE import_run_items SET
                                item_kind = ?, status = ?,
                                evidence_id = COALESCE(?, evidence_id),
                                revision_id = COALESCE(?, revision_id),
                                operation_key = COALESCE(?, operation_key),
                                operation_kind = COALESCE(?, operation_kind),
                                target_bucket_id = COALESCE(?, target_bucket_id),
                                payload_digest = COALESCE(?, payload_digest),
                                result_id = COALESCE(?, result_id),
                                item_count = COALESCE(?, item_count),
                                error_category = ?, updated_at = ?
                            WHERE run_id = ? AND item_key = ?
                            """,
                            (
                                item_kind,
                                status,
                                evidence_id,
                                revision_id,
                                operation_key,
                                operation_kind,
                                target_bucket_id,
                                payload_digest,
                                result_id,
                                item_count,
                                error_category,
                                now,
                                run_id,
                                item_key,
                            ),
                        )
                    conn.commit()
            except RawEvidenceError:
                raise
            except sqlite3.Error as exc:
                raise RawEvidenceError("storage_unavailable") from exc
        result = self.get_import_item(run_id, item_key)
        assert result is not None
        return result

    @guarded_mutation("raw_evidence_lineage_intent")
    def create_lineage_intent(
        self,
        *,
        run_id: str,
        run_item_key: str,
        operation_key: str,
        memory_id: str,
        memory_mutation_id: str,
        evidence_id: str,
        revision_id: str,
        lineage_kind: str,
    ) -> dict[str, Any]:
        """Durably record one capture-bound lineage intent before mutation."""

        self._require_enabled()
        run_id = _validate_id(run_id, "run_id")
        _validate_text(run_item_key, "run_item_key", self.limits.max_metadata_chars)
        _validate_text(operation_key, "operation_key", 256)
        memory_id = _validate_memory_id(memory_id, "memory_id")
        memory_mutation_id = _validate_hash(memory_mutation_id, "memory_mutation_id")
        evidence_id = _validate_id(evidence_id, "evidence_id")
        revision_id = _validate_id(revision_id, "revision_id")
        lineage_kind = _validate_choice(lineage_kind, LINEAGE_KINDS, "lineage_kind")

        with self._lock:
            try:
                with self._connect() as conn:
                    self._begin_write(conn)
                    item = conn.execute(
                        """
                        SELECT evidence_id, revision_id
                        FROM import_run_items
                        WHERE run_id = ? AND item_key = ?
                        """,
                        (run_id, run_item_key),
                    ).fetchone()
                    if item is None:
                        raise RawEvidenceError("not_found")
                    if (
                        item["evidence_id"] != evidence_id
                        or item["revision_id"] != revision_id
                    ):
                        raise RawEvidenceError("lineage_identity_conflict")
                    revision = conn.execute(
                        """
                        SELECT evidence_id
                        FROM evidence_revisions
                        WHERE revision_id = ?
                        """,
                        (revision_id,),
                    ).fetchone()
                    if revision is None or revision["evidence_id"] != evidence_id:
                        raise RawEvidenceError("lineage_identity_conflict")
                    existing = conn.execute(
                        """
                        SELECT * FROM memory_lineage
                        WHERE run_id = ? AND run_item_key = ?
                          AND operation_key = ? AND memory_id = ?
                          AND evidence_id = ? AND revision_id = ?
                          AND lineage_kind = ?
                        """,
                        (
                            run_id,
                            run_item_key,
                            operation_key,
                            memory_id,
                            evidence_id,
                            revision_id,
                            lineage_kind,
                        ),
                    ).fetchone()
                    if existing is not None:
                        if existing["memory_mutation_id"] != memory_mutation_id:
                            raise RawEvidenceError("lineage_identity_conflict")
                        conn.commit()
                        return dict(existing)

                    now = _now_iso()
                    lineage_id = uuid.uuid4().hex
                    conn.execute(
                        """
                        INSERT INTO memory_lineage (
                            lineage_id, memory_id, memory_mutation_id,
                            run_id, run_item_key, operation_key,
                            evidence_id, revision_id, lineage_kind, status,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                        """,
                        (
                            lineage_id,
                            memory_id,
                            memory_mutation_id,
                            run_id,
                            run_item_key,
                            operation_key,
                            evidence_id,
                            revision_id,
                            lineage_kind,
                            now,
                            now,
                        ),
                    )
                    conn.commit()
            except RawEvidenceError:
                raise
            except sqlite3.IntegrityError as exc:
                raise RawEvidenceError("lineage_identity_conflict") from exc
            except sqlite3.Error as exc:
                raise RawEvidenceError("storage_unavailable") from exc
        result = self.get_lineage(lineage_id)
        assert result is not None
        return result

    def get_lineage(self, lineage_id: str) -> dict[str, Any] | None:
        self._require_enabled()
        lineage_id = _validate_id(lineage_id, "lineage_id")
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM memory_lineage WHERE lineage_id = ?",
                    (lineage_id,),
                ).fetchone()
        return dict(row) if row is not None else None

    def list_lineage(
        self,
        *,
        run_id: str | None = None,
        status: str | None = None,
        memory_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read bounded lineage metadata for internal reconciliation/tests."""

        self._require_enabled()
        clauses: list[str] = []
        values: list[Any] = []
        if run_id is not None:
            run_id = _validate_id(run_id, "run_id")
            clauses.append("run_id = ?")
            values.append(run_id)
        if status is not None:
            status = _validate_choice(status, LINEAGE_STATUSES, "status")
            clauses.append("status = ?")
            values.append(status)
        if memory_id is not None:
            memory_id = _validate_memory_id(memory_id, "memory_id")
            clauses.append("memory_id = ?")
            values.append(memory_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM memory_lineage"
                    + where
                    + " ORDER BY created_at, lineage_id",
                    values,
                ).fetchall()
        return [dict(row) for row in rows]

    @guarded_mutation("raw_evidence_lineage_status_update")
    def update_lineage_status(
        self,
        lineage_id: str,
        *,
        status: str,
    ) -> dict[str, Any]:
        self._require_enabled()
        lineage_id = _validate_id(lineage_id, "lineage_id")
        status = _validate_choice(status, LINEAGE_STATUSES, "status")
        now = _now_iso()
        with self._lock:
            try:
                with self._connect() as conn:
                    self._begin_write(conn)
                    cursor = conn.execute(
                        """
                        UPDATE memory_lineage
                        SET status = ?, updated_at = ?
                        WHERE lineage_id = ?
                        """,
                        (status, now, lineage_id),
                    )
                    if cursor.rowcount != 1:
                        conn.rollback()
                        raise RawEvidenceError("not_found")
                    conn.commit()
            except RawEvidenceError:
                raise
            except sqlite3.Error as exc:
                raise RawEvidenceError("storage_unavailable") from exc
        result = self.get_lineage(lineage_id)
        assert result is not None
        return result

    @guarded_mutation("raw_evidence_source_span_descriptor_create")
    def create_span_descriptor(
        self,
        revision_id: str,
        raw_byte_start: int,
        raw_byte_end: int,
        *,
        span_hash: str,
        span_id: str | None = None,
        allow_sealed: bool = False,
        allow_restricted_admin: bool = False,
    ) -> dict[str, Any]:
        """Create or reuse one immutable revision-bound raw-byte descriptor."""

        self._require_enabled()
        revision_id = _validate_id(revision_id, "revision_id")
        span_id = _validate_id(
            uuid.uuid4().hex if span_id is None else span_id, "span_id"
        )
        span_hash = _validate_hash(span_hash, "span_hash")
        row = self._fetch_revision(revision_id)
        self._check_visibility(
            row["privacy_class"], allow_sealed, allow_restricted_admin
        )
        if (
            row["revision_schema_version"] != REVISION_SCHEMA_VERSION
            or row["hash_algorithm"] != HASH_ALGORITHM
        ):
            raise RawEvidenceError("revision_metadata_unsupported")
        _validate_raw_byte_range(
            raw_byte_start, raw_byte_end, row["content_size_bytes"]
        )
        content = self._read_verified(row, return_content=True)
        selected_hash = hashlib.sha256(content[raw_byte_start:raw_byte_end]).hexdigest()
        if selected_hash != span_hash:
            raise RawEvidenceError("span_hash_invalid")

        now = _now_iso()
        with self._lock:
            try:
                with self._connect() as conn:
                    self._begin_write(conn)
                    existing = conn.execute(
                        "SELECT * FROM source_span_descriptors WHERE span_id = ?",
                        (span_id,),
                    ).fetchone()
                    if existing is not None:
                        expected = (
                            revision_id,
                            SPAN_LOCATOR_KIND,
                            SPAN_LOCATOR_SCHEMA_VERSION,
                            raw_byte_start,
                            raw_byte_end,
                            SPAN_HASH_ALGORITHM,
                            span_hash,
                            SPAN_DESCRIPTOR_SCHEMA_VERSION,
                        )
                        actual = (
                            existing["revision_id"],
                            existing["locator_kind"],
                            existing["locator_schema_version"],
                            existing["raw_byte_start"],
                            existing["raw_byte_end"],
                            existing["span_hash_algorithm"],
                            existing["span_hash"],
                            existing["descriptor_schema_version"],
                        )
                        if actual != expected:
                            conn.rollback()
                            raise RawEvidenceError("idempotency_conflict")
                        conn.commit()
                    else:
                        conn.execute(
                            """
                            INSERT INTO source_span_descriptors (
                                span_id, revision_id, locator_kind,
                                locator_schema_version, raw_byte_start,
                                raw_byte_end, span_hash_algorithm, span_hash,
                                created_at, descriptor_schema_version
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                span_id,
                                revision_id,
                                SPAN_LOCATOR_KIND,
                                SPAN_LOCATOR_SCHEMA_VERSION,
                                raw_byte_start,
                                raw_byte_end,
                                SPAN_HASH_ALGORITHM,
                                span_hash,
                                now,
                                SPAN_DESCRIPTOR_SCHEMA_VERSION,
                            ),
                        )
                        conn.commit()
            except RawEvidenceError:
                raise
            except sqlite3.IntegrityError as exc:
                raise RawEvidenceError("span_descriptor_conflict") from exc
            except sqlite3.Error as exc:
                raise RawEvidenceError("storage_unavailable") from exc
        result = self.get_span_descriptor(
            span_id,
            revision_id,
            allow_sealed=allow_sealed,
            allow_restricted_admin=allow_restricted_admin,
        )
        assert result is not None
        return result

    def create_source_span_descriptor(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Descriptive alias for the internal span descriptor creator."""

        return self.create_span_descriptor(*args, **kwargs)

    def get_span_descriptor(
        self,
        span_id: str,
        revision_id: str,
        *,
        allow_sealed: bool = False,
        allow_restricted_admin: bool = False,
    ) -> dict[str, Any] | None:
        """Read one descriptor only with its explicit revision identity."""

        self._require_enabled()
        span_id = _validate_id(span_id, "span_id")
        revision_id = _validate_id(revision_id, "revision_id")
        row = self._fetch_span_descriptor(span_id, revision_id)
        if row is None:
            return None
        self._check_visibility(
            row["privacy_class"], allow_sealed, allow_restricted_admin
        )
        return dict(row)

    def get_source_span_descriptor(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        """Descriptive alias for the internal span descriptor reader."""

        return self.get_span_descriptor(*args, **kwargs)

    def list_span_descriptors(
        self,
        revision_id: str,
        *,
        allow_sealed: bool = False,
        allow_restricted_admin: bool = False,
    ) -> list[dict[str, Any]]:
        """List descriptors for one explicit revision, never an evidence latest."""

        self._require_enabled()
        revision_id = _validate_id(revision_id, "revision_id")
        revision = self._fetch_revision(revision_id)
        self._check_visibility(
            revision["privacy_class"], allow_sealed, allow_restricted_admin
        )
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT span_id, revision_id, locator_kind,
                           locator_schema_version, raw_byte_start, raw_byte_end,
                           span_hash_algorithm, span_hash, created_at,
                           descriptor_schema_version
                    FROM source_span_descriptors
                    WHERE revision_id = ?
                    ORDER BY raw_byte_start, raw_byte_end, span_id
                    """,
                    (revision_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    @guarded_mutation("raw_evidence_lineage_citation_create")
    def create_lineage_citation(
        self,
        lineage_id: str,
        span_id: str,
        *,
        revision_id: str | None = None,
        allow_sealed: bool = False,
        allow_restricted_admin: bool = False,
    ) -> dict[str, Any]:
        """Create one immutable lineage-to-span relation after revision binding."""

        self._require_enabled()
        lineage_id = _validate_id(lineage_id, "lineage_id")
        span_id = _validate_id(span_id, "span_id")
        if revision_id is not None:
            revision_id = _validate_id(revision_id, "revision_id")
        with self._lock:
            with self._connect() as conn:
                span = conn.execute(
                    """
                    SELECT s.span_id, s.revision_id, r.evidence_id, e.privacy_class
                    FROM source_span_descriptors AS s
                    JOIN evidence_revisions AS r ON r.revision_id = s.revision_id
                    JOIN evidence_objects AS e ON e.evidence_id = r.evidence_id
                    WHERE s.span_id = ?
                    """,
                    (span_id,),
                ).fetchone()
        if span is None:
            raise RawEvidenceError("not_found")
        self._check_visibility(
            span["privacy_class"], allow_sealed, allow_restricted_admin
        )
        with self._lock:
            try:
                with self._connect() as conn:
                    self._begin_write(conn)
                    lineage = conn.execute(
                        "SELECT revision_id, evidence_id FROM memory_lineage WHERE lineage_id = ?",
                        (lineage_id,),
                    ).fetchone()
                    if lineage is None:
                        conn.rollback()
                        raise RawEvidenceError("not_found")
                    if (
                        lineage["revision_id"] != span["revision_id"]
                        or lineage["evidence_id"] != span["evidence_id"]
                        or (revision_id is not None and revision_id != span["revision_id"])
                    ):
                        conn.rollback()
                        raise RawEvidenceError("lineage_span_revision_mismatch")
                    existing = conn.execute(
                        """
                        SELECT * FROM memory_lineage_citations
                        WHERE lineage_id = ? AND span_id = ?
                        """,
                        (lineage_id, span_id),
                    ).fetchone()
                    if existing is None:
                        conn.execute(
                            """
                            INSERT INTO memory_lineage_citations (
                                lineage_id, span_id, created_at,
                                citation_schema_version
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (
                                lineage_id,
                                span_id,
                                _now_iso(),
                                LINEAGE_CITATION_SCHEMA_VERSION,
                            ),
                        )
                    conn.commit()
            except RawEvidenceError:
                raise
            except sqlite3.IntegrityError as exc:
                raise RawEvidenceError("lineage_span_revision_mismatch") from exc
            except sqlite3.Error as exc:
                raise RawEvidenceError("storage_unavailable") from exc
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT lineage_id, span_id, created_at,
                           citation_schema_version
                    FROM memory_lineage_citations
                    WHERE lineage_id = ? AND span_id = ?
                    """,
                    (lineage_id, span_id),
                ).fetchone()
        assert row is not None
        return dict(row)

    def list_lineage_citations(
        self,
        lineage_id: str,
        revision_id: str,
        *,
        allow_sealed: bool = False,
        allow_restricted_admin: bool = False,
    ) -> list[dict[str, Any]]:
        """List citation identities for one explicit lineage revision."""

        self._require_enabled()
        lineage_id = _validate_id(lineage_id, "lineage_id")
        revision_id = _validate_id(revision_id, "revision_id")
        revision = self._fetch_revision(revision_id)
        self._check_visibility(
            revision["privacy_class"], allow_sealed, allow_restricted_admin
        )
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT c.lineage_id, c.span_id, c.created_at,
                           c.citation_schema_version
                    FROM memory_lineage_citations AS c
                    JOIN memory_lineage AS l ON l.lineage_id = c.lineage_id
                    JOIN source_span_descriptors AS s ON s.span_id = c.span_id
                    JOIN evidence_revisions AS r ON r.revision_id = s.revision_id
                    WHERE c.lineage_id = ? AND l.revision_id = ?
                      AND s.revision_id = ? AND l.evidence_id = r.evidence_id
                    ORDER BY c.created_at, c.span_id
                    """,
                    (lineage_id, revision_id, revision_id),
                ).fetchall()
        return [dict(row) for row in rows]

    def verify_span(
        self,
        span_id: str,
        revision_id: str,
        *,
        allow_sealed: bool = False,
        allow_restricted_admin: bool = False,
    ) -> dict[str, Any]:
        """Verify one exact span against one explicit immutable revision."""

        try:
            span_id = _validate_id(span_id, "span_id")
            revision_id = _validate_id(revision_id, "revision_id")
        except RawEvidenceError:
            return {"state": "INVALID"}
        span = self._fetch_span_descriptor(span_id, revision_id)
        if span is None:
            return {"state": "NOT_VERIFIED"}
        try:
            self._check_visibility(
                span["privacy_class"], allow_sealed, allow_restricted_admin
            )
        except RawEvidenceError:
            return {"state": "ACCESS_DENIED"}
        if (
            span["descriptor_schema_version"] != SPAN_DESCRIPTOR_SCHEMA_VERSION
            or span["locator_kind"] != SPAN_LOCATOR_KIND
            or span["locator_schema_version"] != SPAN_LOCATOR_SCHEMA_VERSION
            or span["span_hash_algorithm"] != SPAN_HASH_ALGORITHM
            or span["hash_algorithm"] != HASH_ALGORITHM
            or span["revision_schema_version"] != REVISION_SCHEMA_VERSION
        ):
            return {"state": "UNSUPPORTED"}
        if span["lifecycle_state"] != "available":
            if span["lifecycle_state"] in {"integrity_failed", "purged", "missing"}:
                return {
                    "state": "INVALID"
                    if span["lifecycle_state"] == "integrity_failed"
                    else "UNAVAILABLE"
                }
            return {"state": "UNAVAILABLE"}
        if span["verification_state"] == "failed":
            return {"state": "INVALID"}
        if span["verification_state"] != "verified":
            return {"state": "NOT_VERIFIED"}
        try:
            content = self._read_verified(span, return_content=True)
            _validate_raw_byte_range(
                span["raw_byte_start"], span["raw_byte_end"], span["content_size_bytes"]
            )
        except RawEvidenceError as exc:
            if exc.code in {"evidence_unavailable", "evidence_missing"}:
                return {"state": "UNAVAILABLE"}
            return {"state": "INVALID"}
        actual_hash = hashlib.sha256(
            content[span["raw_byte_start"] : span["raw_byte_end"]]
        ).hexdigest()
        if actual_hash != span["span_hash"]:
            return {"state": "INVALID"}
        return {"state": "VERIFIED", "span_id": span_id, "revision_id": revision_id}

    def verify_lineage_citation(
        self,
        lineage_id: str,
        span_id: str,
        revision_id: str,
        *,
        allow_sealed: bool = False,
        allow_restricted_admin: bool = False,
    ) -> dict[str, Any]:
        """Verify one citation relation and its exact explicit-revision span."""

        try:
            lineage_id = _validate_id(lineage_id, "lineage_id")
            span_id = _validate_id(span_id, "span_id")
            revision_id = _validate_id(revision_id, "revision_id")
        except RawEvidenceError:
            return {"state": "INVALID"}
        with self._lock:
            with self._connect() as conn:
                lineage = conn.execute(
                    """
                    SELECT l.revision_id, l.evidence_id,
                           r.evidence_id AS revision_evidence_id
                    FROM memory_lineage AS l
                    LEFT JOIN evidence_revisions AS r
                      ON r.revision_id = l.revision_id
                    WHERE l.lineage_id = ?
                    """,
                    (lineage_id,),
                ).fetchone()
                citation = conn.execute(
                    """
                    SELECT 1 FROM memory_lineage_citations
                    WHERE lineage_id = ? AND span_id = ?
                    """,
                    (lineage_id, span_id),
                ).fetchone()
        if lineage is None or citation is None:
            return {"state": "NOT_VERIFIED"}
        if (
            lineage["revision_id"] != revision_id
            or lineage["evidence_id"] != lineage["revision_evidence_id"]
        ):
            return {"state": "INVALID"}
        result = self.verify_span(
            span_id,
            revision_id,
            allow_sealed=allow_sealed,
            allow_restricted_admin=allow_restricted_admin,
        )
        if result["state"] == "VERIFIED":
            result["lineage_id"] = lineage_id
        return result

    @guarded_mutation("raw_evidence_source_span_candidate_set_create")
    def create_candidate_set(
        self,
        *,
        revision_id: str,
        run_id: str,
        run_item_key: str,
        producer_version: str,
        input_digest: str,
        candidates: Iterable[dict[str, Any]],
        allow_sealed: bool = False,
        allow_restricted_admin: bool = False,
    ) -> list[dict[str, Any]]:
        """Atomically establish one complete descriptor/mapping authority set."""

        self._require_enabled()
        revision_id = _validate_id(revision_id, "revision_id")
        run_id = _validate_id(run_id, "run_id")
        _validate_text(run_item_key, "run_item_key", self.limits.max_metadata_chars)
        _validate_text(producer_version, "producer_version", 128)
        input_digest = _validate_hash(input_digest, "input_digest")
        specs = list(candidates)
        if not specs:
            raise RawEvidenceError("candidate_scope_invalid")
        revision = self._fetch_revision(revision_id)
        self._check_visibility(
            revision["privacy_class"], allow_sealed, allow_restricted_admin
        )
        if (
            revision["revision_schema_version"] != REVISION_SCHEMA_VERSION
            or revision["hash_algorithm"] != HASH_ALGORITHM
        ):
            raise RawEvidenceError("revision_metadata_unsupported")
        content = self._read_verified(revision, return_content=True)
        normalized: list[dict[str, Any]] = []
        for spec in specs:
            if not isinstance(spec, dict):
                raise RawEvidenceError("candidate_scope_invalid")
            span_id = _validate_id(spec.get("span_id"), "span_id")
            raw_byte_start = spec.get("raw_byte_start")
            raw_byte_end = spec.get("raw_byte_end")
            span_hash = _validate_hash(spec.get("span_hash"), "span_hash")
            _validate_raw_byte_range(
                raw_byte_start, raw_byte_end, revision["content_size_bytes"]
            )
            if hashlib.sha256(content[raw_byte_start:raw_byte_end]).hexdigest() != span_hash:
                raise RawEvidenceError("span_hash_invalid")
            candidate_token = spec.get("candidate_token")
            if candidate_token is not None:
                candidate_token = _validate_id(candidate_token, "candidate_token")
            normalized.append(
                {
                    "span_id": span_id,
                    "raw_byte_start": raw_byte_start,
                    "raw_byte_end": raw_byte_end,
                    "span_hash": span_hash,
                    "candidate_token": candidate_token,
                }
            )

        with self._lock:
            conn = self._connect()
            try:
                self._begin_write(conn)
                item = conn.execute(
                    """
                    SELECT evidence_id, revision_id FROM import_run_items
                    WHERE run_id = ? AND item_key = ?
                    """,
                    (run_id, run_item_key),
                ).fetchone()
                if (
                    item is None
                    or item["revision_id"] != revision_id
                    or item["evidence_id"] != revision["evidence_id"]
                ):
                    raise RawEvidenceError("candidate_scope_invalid")
                now = _now_iso()
                results: list[dict[str, Any]] = []
                for spec in normalized:
                    existing = conn.execute(
                        "SELECT * FROM source_span_descriptors WHERE span_id = ?",
                        (spec["span_id"],),
                    ).fetchone()
                    expected_descriptor = (
                        revision_id,
                        SPAN_LOCATOR_KIND,
                        SPAN_LOCATOR_SCHEMA_VERSION,
                        spec["raw_byte_start"],
                        spec["raw_byte_end"],
                        SPAN_HASH_ALGORITHM,
                        spec["span_hash"],
                        SPAN_DESCRIPTOR_SCHEMA_VERSION,
                    )
                    if existing is not None:
                        actual_descriptor = (
                            existing["revision_id"],
                            existing["locator_kind"],
                            existing["locator_schema_version"],
                            existing["raw_byte_start"],
                            existing["raw_byte_end"],
                            existing["span_hash_algorithm"],
                            existing["span_hash"],
                            existing["descriptor_schema_version"],
                        )
                        if actual_descriptor != expected_descriptor:
                            raise RawEvidenceError("idempotency_conflict")
                    else:
                        conn.execute(
                            """
                            INSERT INTO source_span_descriptors (
                                span_id, revision_id, locator_kind,
                                locator_schema_version, raw_byte_start,
                                raw_byte_end, span_hash_algorithm, span_hash,
                                created_at, descriptor_schema_version
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                spec["span_id"], revision_id, SPAN_LOCATOR_KIND,
                                SPAN_LOCATOR_SCHEMA_VERSION,
                                spec["raw_byte_start"], spec["raw_byte_end"],
                                SPAN_HASH_ALGORITHM, spec["span_hash"], now,
                                SPAN_DESCRIPTOR_SCHEMA_VERSION,
                            ),
                        )
                    mapping = conn.execute(
                        """
                        SELECT * FROM source_span_candidate_tokens
                        WHERE revision_id = ? AND run_id = ? AND run_item_key = ?
                          AND producer_version = ? AND input_digest = ?
                          AND raw_byte_start = ? AND raw_byte_end = ?
                        """,
                        (
                            revision_id, run_id, run_item_key, producer_version,
                            input_digest, spec["raw_byte_start"],
                            spec["raw_byte_end"],
                        ),
                    ).fetchone()
                    if mapping is not None:
                        if mapping["span_id"] != spec["span_id"]:
                            raise RawEvidenceError("candidate_scope_conflict")
                        results.append(dict(mapping))
                        continue
                    candidate_token = spec["candidate_token"]
                    if candidate_token is None:
                        candidate_token = uuid.uuid4().hex
                    candidate_token = _validate_id(candidate_token, "candidate_token")
                    token_existing = conn.execute(
                        "SELECT 1 FROM source_span_candidate_tokens WHERE candidate_token = ?",
                        (candidate_token,),
                    ).fetchone()
                    if token_existing is not None:
                        raise RawEvidenceError("candidate_scope_conflict")
                    conn.execute(
                        """
                        INSERT INTO source_span_candidate_tokens (
                            candidate_token, revision_id, run_id, run_item_key,
                            producer_version, input_digest, raw_byte_start,
                            raw_byte_end, span_id, created_at,
                            candidate_schema_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            candidate_token, revision_id, run_id, run_item_key,
                            producer_version, input_digest,
                            spec["raw_byte_start"], spec["raw_byte_end"],
                            spec["span_id"], now,
                            SOURCE_SPAN_CANDIDATE_SCHEMA_VERSION,
                        ),
                    )
                    mapping = conn.execute(
                        "SELECT * FROM source_span_candidate_tokens WHERE candidate_token = ?",
                        (candidate_token,),
                    ).fetchone()
                    assert mapping is not None
                    results.append(dict(mapping))
                conn.commit()
                return results
            except RawEvidenceError:
                with suppress(sqlite3.Error):
                    conn.rollback()
                raise
            except sqlite3.IntegrityError as exc:
                with suppress(sqlite3.Error):
                    conn.rollback()
                raise RawEvidenceError("candidate_scope_conflict") from exc
            except sqlite3.Error as exc:
                with suppress(sqlite3.Error):
                    conn.rollback()
                raise RawEvidenceError("storage_unavailable") from exc
            finally:
                conn.close()

    @guarded_mutation("raw_evidence_source_span_candidate_create")
    def create_candidate_mapping(
        self,
        *,
        candidate_token: str | None = None,
        revision_id: str,
        run_id: str,
        run_item_key: str,
        producer_version: str,
        input_digest: str,
        raw_byte_start: int,
        raw_byte_end: int,
        span_id: str,
        allow_sealed: bool = False,
        allow_restricted_admin: bool = False,
    ) -> dict[str, Any]:
        """Persist retry-stable private candidate-token eligibility."""

        self._require_enabled()
        revision_id = _validate_id(revision_id, "revision_id")
        run_id = _validate_id(run_id, "run_id")
        _validate_text(run_item_key, "run_item_key", self.limits.max_metadata_chars)
        _validate_text(producer_version, "producer_version", 128)
        input_digest = _validate_hash(input_digest, "input_digest")
        span_id = _validate_id(span_id, "span_id")
        candidate_token = _validate_id(
            uuid.uuid4().hex if candidate_token is None else candidate_token,
            "candidate_token",
        )
        descriptor = self.get_span_descriptor(
            span_id,
            revision_id,
            allow_sealed=allow_sealed,
            allow_restricted_admin=allow_restricted_admin,
        )
        if descriptor is None:
            raise RawEvidenceError("not_found")
        if (
            descriptor["raw_byte_start"] != raw_byte_start
            or descriptor["raw_byte_end"] != raw_byte_end
        ):
            raise RawEvidenceError("candidate_span_mismatch")
        _validate_raw_byte_range(raw_byte_start, raw_byte_end, descriptor["raw_byte_end"])
        now = _now_iso()
        with self._lock:
            try:
                with self._connect() as conn:
                    self._begin_write(conn)
                    item = conn.execute(
                        """
                        SELECT evidence_id, revision_id FROM import_run_items
                        WHERE run_id = ? AND item_key = ?
                        """,
                        (run_id, run_item_key),
                    ).fetchone()
                    if (
                        item is None
                        or item["revision_id"] != revision_id
                        or item["evidence_id"] != descriptor["evidence_id"]
                    ):
                        conn.rollback()
                        raise RawEvidenceError("candidate_scope_invalid")
                    existing = conn.execute(
                        """
                        SELECT * FROM source_span_candidate_tokens
                        WHERE revision_id = ? AND run_id = ? AND run_item_key = ?
                          AND producer_version = ? AND input_digest = ?
                          AND raw_byte_start = ? AND raw_byte_end = ?
                        """,
                        (
                            revision_id, run_id, run_item_key, producer_version,
                            input_digest, raw_byte_start, raw_byte_end,
                        ),
                    ).fetchone()
                    if existing is not None:
                        if existing["span_id"] != span_id:
                            conn.rollback()
                            raise RawEvidenceError("candidate_scope_conflict")
                        conn.commit()
                        return dict(existing)
                    token_existing = conn.execute(
                        "SELECT 1 FROM source_span_candidate_tokens WHERE candidate_token = ?",
                        (candidate_token,),
                    ).fetchone()
                    if token_existing is not None:
                        conn.rollback()
                        raise RawEvidenceError("candidate_scope_conflict")
                    conn.execute(
                        """
                        INSERT INTO source_span_candidate_tokens (
                            candidate_token, revision_id, run_id, run_item_key,
                            producer_version, input_digest, raw_byte_start,
                            raw_byte_end, span_id, created_at,
                            candidate_schema_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            candidate_token, revision_id, run_id, run_item_key,
                            producer_version, input_digest, raw_byte_start,
                            raw_byte_end, span_id, now,
                            SOURCE_SPAN_CANDIDATE_SCHEMA_VERSION,
                        ),
                    )
                    conn.commit()
            except RawEvidenceError:
                raise
            except sqlite3.IntegrityError as exc:
                raise RawEvidenceError("candidate_scope_conflict") from exc
            except sqlite3.Error as exc:
                raise RawEvidenceError("storage_unavailable") from exc
        return dict(self._get_candidate_mapping(candidate_token))

    def resolve_candidate_tokens(
        self,
        *,
        candidate_tokens: Iterable[str],
        revision_id: str,
        run_id: str,
        run_item_key: str,
        producer_version: str,
        input_digest: str,
        allow_sealed: bool = False,
        allow_restricted_admin: bool = False,
    ) -> dict[str, Any]:
        """Resolve only the current host-rendered opaque token eligibility set."""

        try:
            tokens = list(candidate_tokens)
            revision_id = _validate_id(revision_id, "revision_id")
            run_id = _validate_id(run_id, "run_id")
            _validate_text(run_item_key, "run_item_key", self.limits.max_metadata_chars)
            _validate_text(producer_version, "producer_version", 128)
            input_digest = _validate_hash(input_digest, "input_digest")
            if any(
                not isinstance(token, str) or _ID_PATTERN.fullmatch(token) is None
                for token in tokens
            ) or len(set(tokens)) != len(tokens):
                raise RawEvidenceError("attribution_invalid")
        except (RawEvidenceError, TypeError):
            return {"status": "invalid", "span_ids": []}
        try:
            revision = self._fetch_revision(revision_id)
            self._check_visibility(
                revision["privacy_class"], allow_sealed, allow_restricted_admin
            )
        except RawEvidenceError as exc:
            if exc.code in {"sealed_access_denied", "restricted_admin_access_denied"}:
                return {"status": "access_denied", "span_ids": []}
            return {"status": "invalid", "span_ids": []}
        if not tokens:
            return {"status": "empty", "span_ids": []}
        with self._lock:
            with self._connect() as conn:
                rows = []
                for token in tokens:
                    row = conn.execute(
                        """
                        SELECT c.span_id
                        FROM source_span_candidate_tokens AS c
                        JOIN source_span_descriptors AS s ON s.span_id = c.span_id
                        WHERE c.candidate_token = ? AND c.revision_id = ?
                          AND c.run_id = ? AND c.run_item_key = ?
                          AND c.producer_version = ? AND c.input_digest = ?
                          AND s.revision_id = c.revision_id
                        """,
                        (
                            token, revision_id, run_id, run_item_key,
                            producer_version, input_digest,
                        ),
                    ).fetchone()
                    if row is None:
                        return {"status": "invalid", "span_ids": []}
                    rows.append(row)
        return {"status": "valid", "span_ids": [row["span_id"] for row in rows]}

    def _get_candidate_mapping(self, candidate_token: str) -> sqlite3.Row:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM source_span_candidate_tokens WHERE candidate_token = ?",
                    (candidate_token,),
                ).fetchone()
        if row is None:
            raise RawEvidenceError("not_found")
        return row

    @guarded_mutation("raw_evidence_import_capture")
    def create_or_reuse_import_evidence(
        self,
        content: bytes | bytearray | memoryview,
        *,
        run_id: str,
        item_key: str = "source_snapshot",
        source_system: str = "dashboard",
        source_kind: str = "import_upload",
        source_scope: str = "dashboard_upload",
        filename: str = "upload",
        media_type: str = "application/octet-stream",
        source_occurrence_key: str | None = None,
        captured_at: str | None = None,
        privacy_class: str = "restricted_admin",
    ) -> dict[str, Any]:
        """Atomically create or reuse one run-bound logical snapshot."""

        self._require_enabled()
        run_id = _validate_id(run_id, "run_id")
        _validate_text(item_key, "item_key", self.limits.max_metadata_chars)
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise RawEvidenceError("invalid_input")
        content_bytes = bytes(content)
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        content_size = len(content_bytes)
        metadata = self._validate_metadata(
            source_system=source_system,
            source_kind=source_kind,
            source_scope=source_scope,
            upstream_source_id=None,
            upstream_item_id=None,
            source_occurrence_key=source_occurrence_key or f"{run_id}:{item_key}",
            identity_origin="local",
            fidelity_level="IMPORT_SNAPSHOT",
            media_type=media_type or "application/octet-stream",
            privacy_class=privacy_class,
            captured_at=captured_at,
        )
        temp_path: Path | None = None
        existing_succeeded = False
        with self._lock:
            try:
                with self._connect() as conn:
                    self._begin_write(conn)
                    run = conn.execute(
                        "SELECT * FROM import_runs WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()
                    if run is None:
                        raise RawEvidenceError("not_found")
                    if (
                        run["source_sha256"] != content_hash
                        or run["source_size_bytes"] != content_size
                    ):
                        raise RawEvidenceError("idempotency_conflict")
                    item = conn.execute(
                        "SELECT * FROM import_run_items WHERE run_id = ? AND item_key = ?",
                        (run_id, item_key),
                    ).fetchone()
                    if item is None:
                        evidence_id = uuid.uuid4().hex
                        revision_id = uuid.uuid4().hex
                        conn.execute(
                            """
                            INSERT INTO import_run_items (
                                run_id, item_key, item_kind, input_digest, status,
                                evidence_id, revision_id, created_at, updated_at,
                                item_schema_version
                            ) VALUES (?, ?, 'snapshot', ?, 'capture_pending', ?, ?, ?, ?, ?)
                            """,
                            (
                                run_id,
                                item_key,
                                content_hash,
                                evidence_id,
                                revision_id,
                                _now_iso(),
                                _now_iso(),
                                IMPORT_RUN_SCHEMA_VERSION,
                            ),
                        )
                    else:
                        if item["input_digest"] != content_hash:
                            raise RawEvidenceError("idempotency_conflict")
                        evidence_id = item["evidence_id"] or uuid.uuid4().hex
                        revision_id = item["revision_id"] or uuid.uuid4().hex
                        if item["status"] == "succeeded":
                            existing_succeeded = True
                        else:
                            conn.execute(
                                """
                                UPDATE import_run_items SET
                                    status = 'capture_pending', evidence_id = ?,
                                    revision_id = ?, error_category = NULL,
                                    updated_at = ?
                                WHERE run_id = ? AND item_key = ?
                                """,
                                (
                                    evidence_id,
                                    revision_id,
                                    _now_iso(),
                                    run_id,
                                    item_key,
                                ),
                            )
                    conn.commit()
            except RawEvidenceError:
                raise
            except sqlite3.Error as exc:
                raise RawEvidenceError("storage_unavailable") from exc

            if existing_succeeded:
                self.verify_content(
                    revision_id,
                    allow_restricted_admin=True,
                )
                return self.get_evidence(
                    evidence_id,
                    allow_restricted_admin=True,
                )

            try:
                temp_path, content_size, content_hash = self._stage_content(content_bytes)
                self._ensure_store_capacity(content_size)

                def finish_import(
                    conn: sqlite3.Connection,
                    registered_evidence_id: str,
                    registered_revision_id: str,
                    registered_at: str,
                ) -> None:
                    item = conn.execute(
                        "SELECT status FROM import_run_items "
                        "WHERE run_id = ? AND item_key = ?",
                        (run_id, item_key),
                    ).fetchone()
                    if item is None:
                        raise RawEvidenceError("not_found")
                    if item["status"] == "succeeded":
                        raise RawEvidenceError("idempotency_conflict")
                    conn.execute(
                        """
                        UPDATE import_run_items SET status = 'succeeded',
                            evidence_id = ?, revision_id = ?, error_category = NULL,
                            updated_at = ?
                        WHERE run_id = ? AND item_key = ?
                        """,
                        (
                            registered_evidence_id, registered_revision_id,
                            registered_at, run_id, item_key,
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE import_runs SET evidence_id = ?, revision_id = ?
                        WHERE run_id = ?
                        """,
                        (registered_evidence_id, registered_revision_id, run_id),
                    )
                    conn.execute(
                        """
                        UPDATE import_runs SET status = 'capture_succeeded',
                            updated_at = ?
                        WHERE run_id = ? AND status = 'capture_pending'
                        """,
                        (registered_at, run_id),
                    )

                self._coordinate_capture(
                    evidence_id=evidence_id,
                    revision_id=revision_id,
                    metadata=metadata,
                    content_hash=content_hash,
                    content_size=content_size,
                    temp_path=temp_path,
                    now=_now_iso(),
                    post_register=finish_import,
                )
                temp_path = None
            except RawEvidenceError:
                raise
            except sqlite3.IntegrityError as exc:
                raise RawEvidenceError("idempotency_conflict") from exc
            except sqlite3.Error as exc:
                raise RawEvidenceError("storage_unavailable") from exc
            finally:
                if temp_path is not None:
                    self._remove_temp(temp_path)

        result = self.get_evidence(
            evidence_id,
            allow_restricted_admin=True,
        )
        self.verify_content(
            revision_id,
            allow_restricted_admin=True,
        )
        return result

    def get_evidence(
        self,
        evidence_id: str,
        *,
        allow_sealed: bool = False,
        allow_restricted_admin: bool = False,
    ) -> dict[str, Any]:
        self._require_enabled()
        evidence_id = _validate_id(evidence_id, "evidence_id")
        row = self._fetch_evidence(evidence_id)
        self._check_visibility(
            row["privacy_class"],
            allow_sealed,
            allow_restricted_admin,
        )
        return dict(row)

    def _fetch_evidence(self, evidence_id: str) -> sqlite3.Row:
        evidence_id = _validate_id(evidence_id, "evidence_id")
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT
                        e.evidence_id, e.source_system, e.source_kind,
                        e.source_scope, e.upstream_source_id,
                        e.upstream_item_id, e.source_occurrence_key,
                        e.identity_origin, e.privacy_class,
                        COALESCE(l.lifecycle_state, e.lifecycle_state) AS lifecycle_state,
                        l.retention_deadline, l.retention_policy_version,
                        l.lifecycle_reason, l.tombstoned_at, l.expired_at,
                        l.purge_started_at, l.purged_at, l.payload_deleted,
                        e.captured_at, e.created_at, e.updated_at,
                        e.record_schema_version, r.revision_id,
                        r.fidelity_level, r.media_type, r.hash_algorithm,
                        r.content_hash, r.content_size_bytes, r.blob_relpath,
                        r.created_at AS revision_created_at,
                        r.verification_state, r.revision_schema_version
                    FROM evidence_objects AS e
                    JOIN evidence_revisions AS r
                      ON r.evidence_id = e.evidence_id
                    LEFT JOIN evidence_lifecycle AS l
                      ON l.revision_id = r.revision_id
                    WHERE e.evidence_id = ?
                    ORDER BY r.created_at DESC, r.revision_id DESC
                    LIMIT 1
                    """,
                    (evidence_id,),
                ).fetchone()
        if row is None:
            raise RawEvidenceError("not_found")
        return row

    def _get_evidence_internal(self, evidence_id: str) -> dict[str, Any]:
        """Return a newly captured record to trusted internal completion code."""

        self._require_enabled()
        return dict(self._fetch_evidence(evidence_id))

    def get_revision(
        self,
        revision_id: str,
        *,
        allow_sealed: bool = False,
        allow_restricted_admin: bool = False,
    ) -> dict[str, Any]:
        self._require_enabled()
        row = self._fetch_revision(revision_id)
        self._check_visibility(
            row["privacy_class"],
            allow_sealed,
            allow_restricted_admin,
        )
        return dict(row)

    def get_content(
        self,
        revision_id: str,
        *,
        allow_sealed: bool = False,
        allow_restricted_admin: bool = False,
    ) -> bytes:
        self._require_enabled()
        row = self._fetch_revision(revision_id)
        self._check_visibility(
            row["privacy_class"],
            allow_sealed,
            allow_restricted_admin,
        )
        return self._read_verified(row, return_content=True)

    def verify_content(
        self,
        revision_id: str,
        *,
        allow_sealed: bool = False,
        allow_restricted_admin: bool = False,
    ) -> bool:
        self._require_enabled()
        row = self._fetch_revision(revision_id)
        self._check_visibility(
            row["privacy_class"],
            allow_sealed,
            allow_restricted_admin,
        )
        self._read_verified(row, return_content=False)
        return True

    def verify(
        self,
        revision_id: str,
        *,
        allow_sealed: bool = False,
        allow_restricted_admin: bool = False,
    ) -> bool:
        """Internal shorthand for :meth:`verify_content`."""

        return self.verify_content(
            revision_id,
            allow_sealed=allow_sealed,
            allow_restricted_admin=allow_restricted_admin,
        )

    @guarded_mutation("raw_evidence_metadata_update")
    def update_metadata(
        self,
        evidence_id: str,
        *,
        privacy_class: str | None = None,
        lifecycle_state: str | None = None,
    ) -> dict[str, Any]:
        """Update only O5A operational metadata; content never changes."""

        self._require_enabled()
        evidence_id = _validate_id(evidence_id, "evidence_id")
        if privacy_class is None and lifecycle_state is None:
            raise RawEvidenceError("invalid_input")
        if privacy_class is not None:
            privacy_class = _validate_choice(
                privacy_class, PRIVACY_CLASSES, "privacy_class"
            )
        if lifecycle_state is not None:
            lifecycle_state = _validate_choice(
                lifecycle_state, MUTABLE_LIFECYCLE_STATES, "lifecycle_state"
            )
        legacy_lifecycle_state = lifecycle_state
        if lifecycle_state in {"expired", "purge_pending", "purged", "missing"}:
            legacy_lifecycle_state = "tombstoned"
        assignments: list[str] = []
        values: list[Any] = []
        if privacy_class is not None:
            assignments.append("privacy_class = ?")
            values.append(privacy_class)
        if lifecycle_state is not None:
            assignments.append("lifecycle_state = ?")
            values.append(legacy_lifecycle_state)
        assignments.append("updated_at = ?")
        values.extend((_now_iso(), evidence_id))

        with self._lock:
            try:
                with self._connect() as conn:
                    self._begin_write(conn)
                    if lifecycle_state is not None:
                        in_progress = conn.execute(
                            """
                            SELECT 1
                            FROM evidence_revisions AS r
                            JOIN cas_objects AS c
                              ON c.hash_algorithm = r.hash_algorithm
                             AND c.content_hash = r.content_hash
                            WHERE r.evidence_id = ?
                              AND c.state IN ('publish_pending', 'gc_pending')
                            LIMIT 1
                            """,
                            (evidence_id,),
                        ).fetchone()
                        if in_progress is not None:
                            conn.rollback()
                            raise RawEvidenceError("lifecycle_operation_in_progress")
                    cursor = conn.execute(
                        f"UPDATE evidence_objects SET {', '.join(assignments)} "
                        "WHERE evidence_id = ?",
                        values,
                    )
                    if cursor.rowcount != 1:
                        conn.rollback()
                        raise RawEvidenceError("not_found")
                    if lifecycle_state is not None:
                        revision = conn.execute(
                            """
                            SELECT revision_id FROM evidence_revisions
                            WHERE evidence_id = ?
                            ORDER BY created_at DESC, revision_id DESC
                            LIMIT 1
                            """,
                            (evidence_id,),
                        ).fetchone()
                        if revision is not None:
                            conn.execute(
                                """
                                UPDATE evidence_lifecycle
                                SET lifecycle_state = ?, payload_deleted = ?, updated_at = ?
                                WHERE revision_id = ?
                                """,
                                (
                                    lifecycle_state,
                                    1 if lifecycle_state == "purged" else 0,
                                    _now_iso(),
                                    revision["revision_id"],
                                ),
                            )
                    conn.commit()
            except RawEvidenceError:
                raise
            except sqlite3.Error as exc:
                raise RawEvidenceError("storage_unavailable") from exc
        return self._get_evidence_internal(evidence_id)

    def update_state(self, evidence_id: str, lifecycle_state: str) -> dict[str, Any]:
        """Internal shorthand for the O5A metadata-only state update."""

        return self.update_metadata(evidence_id, lifecycle_state=lifecycle_state)

    def _require_enabled(self) -> None:
        if not self.enabled or self.root is None:
            raise RawEvidenceError("store_disabled")

    def _prepare_layout(self) -> None:
        assert self.root is not None
        assert self.blobs_root is not None
        assert self.temp_root is not None
        assert self.quarantine_root is not None
        try:
            for path in (
                self.root,
                self.blobs_root,
                self.temp_root,
                self.quarantine_root,
            ):
                path.mkdir(parents=True, exist_ok=True)
                _reject_reparse_components(path)
            _chmod_private(self.root, directory=True)
            _chmod_private(self.blobs_root, directory=True)
            _chmod_private(self.temp_root, directory=True)
            _chmod_private(self.quarantine_root, directory=True)
        except RawEvidenceError:
            raise
        except OSError as exc:
            raise RawEvidenceError("storage_unavailable") from exc

    def _connect(self) -> sqlite3.Connection:
        self._require_enabled()
        assert self.registry_path is not None
        try:
            _reject_reparse_components(self.registry_path)
            conn = sqlite3.connect(str(self.registry_path), timeout=30)
        except (OSError, sqlite3.Error) as exc:
            raise RawEvidenceError("storage_unavailable") from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS store_schema (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        schema_version INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                schema_row = conn.execute(
                    "SELECT schema_version FROM store_schema WHERE singleton = 1"
                ).fetchone()
                previous_schema_version: int | None = None
                if schema_row is None:
                    now = _now_iso()
                    conn.execute(
                        "INSERT INTO store_schema "
                        "(singleton, schema_version, created_at, updated_at) "
                        "VALUES (1, ?, ?, ?)",
                        (SCHEMA_VERSION, now, now),
                    )
                else:
                    try:
                        previous_schema_version = int(schema_row["schema_version"])
                    except (TypeError, ValueError) as exc:
                        raise RawEvidenceError("schema_unsupported") from exc
                    if previous_schema_version < 1 or previous_schema_version > SCHEMA_VERSION:
                        raise RawEvidenceError("schema_unsupported")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS o5e_coordination (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        repository_id TEXT,
                        active_backup_operation_id TEXT,
                        backup_owner_class TEXT,
                        backup_started_at TEXT,
                        backup_heartbeat_at TEXT,
                        backup_expires_at TEXT,
                        backup_state TEXT NOT NULL DEFAULT 'released'
                            CHECK (backup_state IN ('active', 'released')),
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "INSERT OR IGNORE INTO o5e_coordination "
                    "(singleton, backup_state, updated_at) VALUES (1, 'released', ?)",
                    (_now_iso(),),
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS evidence_objects (
                        evidence_id TEXT PRIMARY KEY,
                        source_system TEXT NOT NULL,
                        source_kind TEXT NOT NULL,
                        source_scope TEXT NOT NULL,
                        upstream_source_id TEXT,
                        upstream_item_id TEXT,
                        source_occurrence_key TEXT,
                        identity_origin TEXT NOT NULL
                            CHECK (identity_origin IN ('upstream', 'local', 'unknown')),
                        privacy_class TEXT NOT NULL
                            CHECK (privacy_class IN ('ordinary', 'sealed', 'restricted_admin')),
                        lifecycle_state TEXT NOT NULL
                            CHECK (lifecycle_state IN (
                                'captured', 'available', 'quarantined',
                                'integrity_failed', 'tombstoned'
                            )),
                        captured_at TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        record_schema_version INTEGER NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS evidence_revisions (
                        revision_id TEXT PRIMARY KEY,
                        evidence_id TEXT NOT NULL,
                        fidelity_level TEXT NOT NULL
                            CHECK (fidelity_level IN (
                                'IMPORT_SNAPSHOT', 'SOURCE_TEXT', 'SOURCE_ITEM',
                                'EXACT_SPAN', 'ORIGINAL_BYTES'
                            )),
                        media_type TEXT NOT NULL,
                        hash_algorithm TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        content_size_bytes INTEGER NOT NULL
                            CHECK (content_size_bytes >= 0),
                        blob_relpath TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        verification_state TEXT NOT NULL
                            CHECK (verification_state IN ('verified', 'quarantined', 'failed')),
                        revision_schema_version INTEGER NOT NULL,
                        FOREIGN KEY (evidence_id) REFERENCES evidence_objects(evidence_id)
                            ON DELETE RESTRICT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS evidence_objects_immutable_identity
                    BEFORE UPDATE OF evidence_id, source_system, source_kind,
                        source_scope, upstream_source_id, upstream_item_id,
                        source_occurrence_key, identity_origin, captured_at,
                        created_at, record_schema_version ON evidence_objects
                    BEGIN
                        SELECT RAISE(ABORT, 'immutable_evidence_identity');
                    END
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS evidence_revisions_immutable_content
                    BEFORE UPDATE OF revision_id, evidence_id, fidelity_level,
                        media_type, hash_algorithm, content_hash,
                        content_size_bytes, blob_relpath, created_at,
                        revision_schema_version ON evidence_revisions
                    BEGIN
                        SELECT RAISE(ABORT, 'immutable_evidence_content');
                    END
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_evidence_revisions_evidence "
                    "ON evidence_revisions(evidence_id, created_at)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS import_runs (
                        run_id TEXT PRIMARY KEY,
                        retry_key TEXT NOT NULL UNIQUE,
                        source_sha256 TEXT NOT NULL,
                        source_size_bytes INTEGER NOT NULL CHECK (source_size_bytes >= 0),
                        capture_mode TEXT NOT NULL CHECK (capture_mode = 'IMPORT_SNAPSHOT'),
                        fidelity_level TEXT NOT NULL
                            CHECK (fidelity_level = 'IMPORT_SNAPSHOT'),
                        evidence_id TEXT,
                        revision_id TEXT,
                        filename TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        source_system TEXT NOT NULL,
                        source_kind TEXT NOT NULL,
                        source_scope TEXT NOT NULL,
                        upstream_source_id TEXT,
                        upstream_item_id TEXT,
                        actor_id TEXT NOT NULL,
                        importer_version TEXT NOT NULL,
                        parser_version TEXT NOT NULL,
                        chunker_version TEXT NOT NULL,
                        preserve_raw INTEGER NOT NULL CHECK (preserve_raw IN (0, 1)),
                        status TEXT NOT NULL,
                        error_category TEXT,
                        total_chunks INTEGER NOT NULL DEFAULT 0 CHECK (total_chunks >= 0),
                        processed_chunks INTEGER NOT NULL DEFAULT 0 CHECK (processed_chunks >= 0),
                        started_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT,
                        retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
                        run_schema_version INTEGER NOT NULL
                    )
                    """
                )
                import_run_columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(import_runs)").fetchall()
                }
                for column in ("evidence_id", "revision_id"):
                    if column not in import_run_columns:
                        conn.execute(
                            f"ALTER TABLE import_runs ADD COLUMN {column} TEXT"
                        )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS import_run_items (
                        run_id TEXT NOT NULL,
                        item_key TEXT NOT NULL,
                        item_kind TEXT NOT NULL,
                        input_digest TEXT NOT NULL,
                        status TEXT NOT NULL,
                        evidence_id TEXT,
                        revision_id TEXT,
                        operation_key TEXT,
                        operation_kind TEXT,
                        target_bucket_id TEXT,
                        payload_digest TEXT,
                        result_id TEXT,
                        item_count INTEGER,
                        error_category TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        item_schema_version INTEGER NOT NULL,
                        PRIMARY KEY (run_id, item_key),
                        FOREIGN KEY (run_id) REFERENCES import_runs(run_id)
                            ON DELETE RESTRICT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_import_runs_source "
                    "ON import_runs(source_sha256, filename, updated_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_import_run_items_operation "
                    "ON import_run_items(operation_key)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_lineage (
                        lineage_id TEXT PRIMARY KEY,
                        memory_id TEXT NOT NULL,
                        memory_mutation_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        run_item_key TEXT NOT NULL,
                        operation_key TEXT NOT NULL,
                        evidence_id TEXT NOT NULL,
                        revision_id TEXT NOT NULL,
                        lineage_kind TEXT NOT NULL CHECK (
                            lineage_kind IN (
                                'created', 'preserve_raw_created',
                                'contributed_update'
                            )
                        ),
                        status TEXT NOT NULL CHECK (
                            status IN (
                                'pending', 'complete', 'source_redacted',
                                'source_expired', 'evidence_missing',
                                'integrity_failed', 'memory_deleted',
                                'needs_reconcile', 'provenance_broken'
                            )
                        ),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE (
                            run_id, run_item_key, operation_key, memory_id,
                            evidence_id, revision_id, lineage_kind
                        ),
                        FOREIGN KEY (run_id, run_item_key)
                            REFERENCES import_run_items(run_id, item_key)
                            ON DELETE RESTRICT,
                        FOREIGN KEY (evidence_id)
                            REFERENCES evidence_objects(evidence_id)
                            ON DELETE RESTRICT,
                        FOREIGN KEY (revision_id)
                            REFERENCES evidence_revisions(revision_id)
                            ON DELETE RESTRICT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memory_lineage_memory_time "
                    "ON memory_lineage(memory_id, created_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memory_lineage_evidence "
                    "ON memory_lineage(evidence_id, revision_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memory_lineage_run_item "
                    "ON memory_lineage(run_id, run_item_key)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memory_lineage_status "
                    "ON memory_lineage(status, updated_at)"
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS memory_lineage_validate_revision_evidence_insert
                    BEFORE INSERT ON memory_lineage
                    BEGIN
                        SELECT RAISE(ABORT, 'lineage_revision_evidence_mismatch')
                        WHERE NOT EXISTS (
                            SELECT 1 FROM evidence_revisions AS r
                            WHERE r.revision_id = NEW.revision_id
                              AND r.evidence_id = NEW.evidence_id
                        );
                    END
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS memory_lineage_validate_revision_evidence_update
                    BEFORE UPDATE OF evidence_id, revision_id ON memory_lineage
                    BEGIN
                        SELECT RAISE(ABORT, 'lineage_revision_evidence_mismatch')
                        WHERE NOT EXISTS (
                            SELECT 1 FROM evidence_revisions AS r
                            WHERE r.revision_id = NEW.revision_id
                              AND r.evidence_id = NEW.evidence_id
                        );
                    END
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS source_span_descriptors (
                        span_id TEXT PRIMARY KEY,
                        revision_id TEXT NOT NULL,
                        locator_kind TEXT NOT NULL CHECK (
                            locator_kind = 'raw_byte_range_v1'
                        ),
                        locator_schema_version INTEGER NOT NULL CHECK (
                            locator_schema_version = 1
                        ),
                        raw_byte_start INTEGER NOT NULL CHECK (
                            raw_byte_start >= 0
                        ),
                        raw_byte_end INTEGER NOT NULL CHECK (
                            raw_byte_end > raw_byte_start
                        ),
                        span_hash_algorithm TEXT NOT NULL CHECK (
                            span_hash_algorithm = 'sha256'
                        ),
                        span_hash TEXT NOT NULL CHECK (
                            length(span_hash) = 64 AND
                            span_hash NOT GLOB '*[^0-9a-f]*'
                        ),
                        created_at TEXT NOT NULL,
                        descriptor_schema_version INTEGER NOT NULL CHECK (
                            descriptor_schema_version = 1
                        ),
                        FOREIGN KEY (revision_id)
                            REFERENCES evidence_revisions(revision_id)
                            ON DELETE RESTRICT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS source_span_descriptors_validate_insert
                    BEFORE INSERT ON source_span_descriptors
                    BEGIN
                        SELECT RAISE(ABORT, 'source_span_revision_missing')
                        WHERE NOT EXISTS (
                            SELECT 1 FROM evidence_revisions
                            WHERE revision_id = NEW.revision_id
                              AND NEW.raw_byte_end <= content_size_bytes
                        );
                    END
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS source_span_descriptors_immutable_update
                    BEFORE UPDATE ON source_span_descriptors
                    BEGIN
                        SELECT RAISE(ABORT, 'immutable_source_span_descriptor');
                    END
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS source_span_descriptors_immutable_delete
                    BEFORE DELETE ON source_span_descriptors
                    BEGIN
                        SELECT RAISE(ABORT, 'immutable_source_span_descriptor');
                    END
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_source_span_descriptors_revision
                    ON source_span_descriptors(revision_id, raw_byte_start, raw_byte_end)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_lineage_citations (
                        lineage_id TEXT NOT NULL,
                        span_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        citation_schema_version INTEGER NOT NULL CHECK (
                            citation_schema_version = 1
                        ),
                        PRIMARY KEY (lineage_id, span_id),
                        FOREIGN KEY (lineage_id)
                            REFERENCES memory_lineage(lineage_id)
                            ON DELETE RESTRICT,
                        FOREIGN KEY (span_id)
                            REFERENCES source_span_descriptors(span_id)
                            ON DELETE RESTRICT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS memory_lineage_citations_validate_insert
                    BEFORE INSERT ON memory_lineage_citations
                    BEGIN
                        SELECT RAISE(ABORT, 'lineage_span_revision_mismatch')
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM memory_lineage AS l
                            JOIN source_span_descriptors AS s
                              ON s.span_id = NEW.span_id
                            JOIN evidence_revisions AS r
                              ON r.revision_id = s.revision_id
                            WHERE l.lineage_id = NEW.lineage_id
                              AND l.revision_id = s.revision_id
                              AND l.evidence_id = r.evidence_id
                        );
                    END
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS memory_lineage_citations_immutable_update
                    BEFORE UPDATE ON memory_lineage_citations
                    BEGIN
                        SELECT RAISE(ABORT, 'immutable_memory_lineage_citation');
                    END
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS memory_lineage_citations_immutable_delete
                    BEFORE DELETE ON memory_lineage_citations
                    BEGIN
                        SELECT RAISE(ABORT, 'immutable_memory_lineage_citation');
                    END
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_memory_lineage_citations_span
                    ON memory_lineage_citations(span_id, lineage_id)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS source_span_candidate_tokens (
                        candidate_token TEXT PRIMARY KEY,
                        revision_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        run_item_key TEXT NOT NULL,
                        producer_version TEXT NOT NULL,
                        input_digest TEXT NOT NULL,
                        raw_byte_start INTEGER NOT NULL CHECK (raw_byte_start >= 0),
                        raw_byte_end INTEGER NOT NULL CHECK (raw_byte_end > raw_byte_start),
                        span_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        candidate_schema_version INTEGER NOT NULL CHECK (
                            candidate_schema_version = 1
                        ),
                        UNIQUE (
                            revision_id, run_id, run_item_key, producer_version,
                            input_digest, raw_byte_start, raw_byte_end
                        ),
                        FOREIGN KEY (revision_id)
                            REFERENCES evidence_revisions(revision_id)
                            ON DELETE RESTRICT,
                        FOREIGN KEY (run_id, run_item_key)
                            REFERENCES import_run_items(run_id, item_key)
                            ON DELETE RESTRICT,
                        FOREIGN KEY (span_id)
                            REFERENCES source_span_descriptors(span_id)
                            ON DELETE RESTRICT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS source_span_candidate_tokens_immutable_update
                    BEFORE UPDATE ON source_span_candidate_tokens
                    BEGIN
                        SELECT RAISE(ABORT, 'immutable_source_span_candidate');
                    END
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS source_span_candidate_tokens_immutable_delete
                    BEFORE DELETE ON source_span_candidate_tokens
                    BEGIN
                        SELECT RAISE(ABORT, 'immutable_source_span_candidate');
                    END
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_source_span_candidate_tokens_scope
                    ON source_span_candidate_tokens(
                        run_id, run_item_key, revision_id, producer_version, input_digest
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cas_objects (
                        hash_algorithm TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        content_size_bytes INTEGER NOT NULL CHECK (content_size_bytes >= 0),
                        blob_relpath TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (
                            state IN ('publish_pending', 'live', 'gc_pending', 'purged')
                        ),
                        operation_id TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (hash_algorithm, content_hash)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS evidence_lifecycle (
                        revision_id TEXT PRIMARY KEY,
                        evidence_id TEXT NOT NULL,
                        lifecycle_state TEXT NOT NULL CHECK (
                            lifecycle_state IN (
                                'captured', 'available', 'quarantined',
                                'tombstoned', 'expired', 'purge_pending',
                                'purged', 'integrity_failed', 'missing'
                            )
                        ),
                        retention_deadline TEXT NOT NULL,
                        retention_policy_version TEXT NOT NULL,
                        lifecycle_reason TEXT,
                        tombstoned_at TEXT,
                        expired_at TEXT,
                        purge_started_at TEXT,
                        purged_at TEXT,
                        purge_operation_id TEXT,
                        payload_deleted INTEGER NOT NULL DEFAULT 0
                            CHECK (payload_deleted IN (0, 1)),
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY (revision_id)
                            REFERENCES evidence_revisions(revision_id)
                            ON DELETE RESTRICT,
                        FOREIGN KEY (evidence_id)
                            REFERENCES evidence_objects(evidence_id)
                            ON DELETE RESTRICT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS lifecycle_audit (
                        lifecycle_operation_id TEXT PRIMARY KEY,
                        evidence_id TEXT NOT NULL,
                        revision_id TEXT NOT NULL,
                        from_state TEXT NOT NULL,
                        to_state TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        actor_class TEXT NOT NULL,
                        payload_deleted INTEGER NOT NULL CHECK (payload_deleted IN (0, 1)),
                        reconciliation_result TEXT,
                        FOREIGN KEY (evidence_id)
                            REFERENCES evidence_objects(evidence_id)
                            ON DELETE RESTRICT,
                        FOREIGN KEY (revision_id)
                            REFERENCES evidence_revisions(revision_id)
                            ON DELETE RESTRICT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_evidence_lifecycle_deadline "
                    "ON evidence_lifecycle(lifecycle_state, retention_deadline)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_evidence_lifecycle_purge "
                    "ON evidence_lifecycle(purge_operation_id, lifecycle_state)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_lifecycle_audit_time "
                    "ON lifecycle_audit(occurred_at)"
                )
                self._backfill_lifecycle_v4(conn)
                if previous_schema_version is not None and previous_schema_version < SCHEMA_VERSION:
                    conn.execute(
                        "UPDATE store_schema SET schema_version = ?, updated_at = ? "
                        "WHERE singleton = 1",
                        (SCHEMA_VERSION, _now_iso()),
                    )
                conn.commit()
                assert self.registry_path is not None
                _chmod_private(self.registry_path)
            except RawEvidenceError:
                conn.rollback()
                raise
            except sqlite3.Error as exc:
                conn.rollback()
                raise RawEvidenceError("storage_unavailable") from exc

    def _backfill_lifecycle_v4(self, conn: sqlite3.Connection) -> None:
        """Create v4 lifecycle/CAS rows without applying lifecycle decisions."""

        rows = conn.execute(
            """
            SELECT e.evidence_id, e.lifecycle_state AS legacy_state,
                   e.captured_at, r.revision_id, r.hash_algorithm,
                   r.content_hash, r.content_size_bytes, r.blob_relpath
            FROM evidence_objects AS e
            JOIN evidence_revisions AS r ON r.evidence_id = e.evidence_id
            """
        ).fetchall()
        now = _now_iso()
        for row in rows:
            state = row["legacy_state"]
            if state not in LIFECYCLE_STATES:
                state = "available"
            deadline = _add_days_iso(row["captured_at"], DEFAULT_RETENTION_DAYS)
            conn.execute(
                """
                INSERT OR IGNORE INTO evidence_lifecycle (
                    revision_id, evidence_id, lifecycle_state,
                    retention_deadline, retention_policy_version,
                    lifecycle_reason, payload_deleted, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, 0, ?)
                """,
                (
                    row["revision_id"],
                    row["evidence_id"],
                    state if state in {"available", "quarantined", "integrity_failed", "tombstoned"}
                    else "available",
                    deadline,
                    RETENTION_POLICY_VERSION,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO cas_objects (
                    hash_algorithm, content_hash, content_size_bytes,
                    blob_relpath, state, operation_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'live', NULL, ?, ?)
                """,
                (
                    row["hash_algorithm"],
                    row["content_hash"],
                    row["content_size_bytes"],
                    row["blob_relpath"],
                    now,
                    now,
                ),
            )

    def _begin_write(self, conn: sqlite3.Connection) -> None:
        """Begin a registry write while honoring the bounded backup fence."""

        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT backup_state, backup_expires_at FROM o5e_coordination "
                "WHERE singleton = 1"
            ).fetchone()
        except sqlite3.OperationalError:
            # Schema initialization creates this table transactionally.
            return
        if (
            row is not None
            and row["backup_state"] == "active"
            and row["backup_expires_at"]
            and _parse_timestamp(row["backup_expires_at"]) > _parse_timestamp(_now_iso())
        ):
            conn.rollback()
            raise RawEvidenceError("backup_in_progress")

    def _stage_content(
        self,
        content: bytes | bytearray | memoryview | BinaryIO,
    ) -> tuple[Path, int, str]:
        assert self.temp_root is not None
        data: bytes | None = None
        if isinstance(content, bytes):
            data = content
        elif isinstance(content, (bytearray, memoryview)):
            if len(content) > min(self.limits.max_evidence_bytes, self.limits.max_temp_bytes):
                raise RawEvidenceError("limit_exceeded")
            data = bytes(content)
        elif not hasattr(content, "read"):
            raise RawEvidenceError("invalid_input")
        if data is not None and len(data) > min(
            self.limits.max_evidence_bytes,
            self.limits.max_temp_bytes,
        ):
            raise RawEvidenceError("limit_exceeded")

        temp_path: Path | None = None
        digest = hashlib.sha256()
        size = 0
        try:
            fd, raw_path = tempfile.mkstemp(
                prefix=".evidence-",
                suffix=".part",
                dir=str(self.temp_root),
            )
            temp_path = Path(raw_path)
            _assert_owned_path(temp_path, self.temp_root)
            with os.fdopen(fd, "wb") as handle:
                if data is not None:
                    chunks: Iterable[bytes] = (data,)
                else:
                    chunks = self._read_chunks(content)
                for chunk in chunks:
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise RawEvidenceError("content_invalid")
                    chunk_bytes = bytes(chunk)
                    size += len(chunk_bytes)
                    if size > min(
                        self.limits.max_evidence_bytes,
                        self.limits.max_temp_bytes,
                    ):
                        raise RawEvidenceError("limit_exceeded")
                    handle.write(chunk_bytes)
                    digest.update(chunk_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            _chmod_private(temp_path)
            return temp_path, size, digest.hexdigest()
        except RawEvidenceError:
            if temp_path is not None:
                self._remove_temp(temp_path)
            raise
        except (OSError, ValueError) as exc:
            if temp_path is not None:
                self._remove_temp(temp_path)
            raise RawEvidenceError("content_write_failed") from exc

    def _read_chunks(self, content: Any) -> Iterable[bytes]:
        while True:
            try:
                chunk = content.read(_MAX_READ_CHUNK)
            except Exception as exc:
                raise RawEvidenceError("content_read_failed") from exc
            if chunk in (b"", ""):
                return
            yield chunk

    def _ensure_store_capacity(self, additional_size: int) -> None:
        assert self.blobs_root is not None
        total = 0
        try:
            for path in self.blobs_root.rglob("*"):
                if path.is_dir():
                    _reject_reparse_components(path)
                    continue
                _assert_owned_path(path, self.blobs_root)
                if not path.is_file():
                    raise RawEvidenceError("storage_unavailable")
                total += path.stat().st_size
                if total + additional_size > self.limits.max_store_bytes:
                    raise RawEvidenceError("limit_exceeded")
        except RawEvidenceError:
            raise
        except OSError as exc:
            raise RawEvidenceError("storage_unavailable") from exc

    def _coordinate_capture(
        self,
        *,
        evidence_id: str,
        revision_id: str,
        metadata: dict[str, Any],
        content_hash: str,
        content_size: int,
        temp_path: Path,
        now: str,
        post_register: Any | None = None,
    ) -> str:
        """Coordinate CAS publication and logical registration.

        ``publish_pending`` is committed before the filesystem publish.  A
        later transaction makes the CAS live and registers the revision.  A
        purge worker therefore cannot mistake an unregistered publication for
        the last live reference.
        """

        operation_id = uuid.uuid4().hex
        blob_relpath = self._relative_blob_path(self._cas_path(content_hash))
        with self._connect() as conn:
            self._begin_write(conn)
            cas = conn.execute(
                """
                SELECT * FROM cas_objects
                WHERE hash_algorithm = ? AND content_hash = ?
                """,
                (HASH_ALGORITHM, content_hash),
            ).fetchone()
            if cas is not None:
                if cas["content_size_bytes"] != content_size or cas["blob_relpath"] != blob_relpath:
                    conn.rollback()
                    raise RawEvidenceError("integrity_conflict")
                if cas["state"] == "gc_pending":
                    conn.rollback()
                    raise RawEvidenceError("cas_gc_pending")
                if cas["state"] == "publish_pending":
                    conn.rollback()
                    raise RawEvidenceError("cas_publish_pending")
                if cas["state"] == "live" and self._cas_file_is_valid(
                    cas["blob_relpath"], content_hash, content_size
                ):
                    self._insert_capture_records(
                        conn, evidence_id, revision_id, metadata,
                        content_hash, content_size, blob_relpath, now,
                    )
                    if post_register is not None:
                        post_register(conn, evidence_id, revision_id, now)
                    conn.commit()
                    self._remove_temp(temp_path)
                    return blob_relpath
                if cas["state"] == "live":
                    conn.rollback()
                    raise RawEvidenceError("cas_integrity_conflict")
                conn.execute(
                    """
                    UPDATE cas_objects SET state = 'publish_pending',
                        operation_id = ?, updated_at = ?
                    WHERE hash_algorithm = ? AND content_hash = ?
                    """,
                    (operation_id, now, HASH_ALGORITHM, content_hash),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO cas_objects (
                        hash_algorithm, content_hash, content_size_bytes,
                        blob_relpath, state, operation_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'publish_pending', ?, ?, ?)
                    """,
                    (
                        HASH_ALGORITHM,
                        content_hash,
                        content_size,
                        blob_relpath,
                        operation_id,
                        now,
                        now,
                    ),
                )
            conn.commit()

        self._publish_blob(temp_path, content_hash, content_size)
        with self._connect() as conn:
            self._begin_write(conn)
            cas = conn.execute(
                """
                SELECT * FROM cas_objects
                WHERE hash_algorithm = ? AND content_hash = ?
                """,
                (HASH_ALGORITHM, content_hash),
            ).fetchone()
            if cas is None or cas["state"] != "publish_pending":
                conn.rollback()
                raise RawEvidenceError("cas_publish_conflict")
            if cas["operation_id"] != operation_id:
                if not self._cas_file_is_valid(cas["blob_relpath"], content_hash, content_size):
                    conn.rollback()
                    raise RawEvidenceError("cas_publish_conflict")
            if not self._cas_file_is_valid(cas["blob_relpath"], content_hash, content_size):
                conn.rollback()
                raise RawEvidenceError("integrity_conflict")
            conn.execute(
                """
                UPDATE cas_objects SET state = 'live', operation_id = NULL,
                    updated_at = ?
                WHERE hash_algorithm = ? AND content_hash = ?
                """,
                (now, HASH_ALGORITHM, content_hash),
            )
            self._insert_capture_records(
                conn, evidence_id, revision_id, metadata,
                content_hash, content_size, blob_relpath, now,
            )
            if post_register is not None:
                post_register(conn, evidence_id, revision_id, now)
            conn.commit()
        self._remove_temp(temp_path)
        return blob_relpath

    def _insert_capture_records(
        self,
        conn: sqlite3.Connection,
        evidence_id: str,
        revision_id: str,
        metadata: dict[str, Any],
        content_hash: str,
        content_size: int,
        blob_relpath: str,
        now: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO evidence_objects (
                evidence_id, source_system, source_kind, source_scope,
                upstream_source_id, upstream_item_id,
                source_occurrence_key, identity_origin, privacy_class,
                lifecycle_state, captured_at, created_at, updated_at,
                record_schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'available', ?, ?, ?, ?)
            """,
            (
                evidence_id,
                metadata["source_system"], metadata["source_kind"],
                metadata["source_scope"], metadata["upstream_source_id"],
                metadata["upstream_item_id"], metadata["source_occurrence_key"],
                metadata["identity_origin"], metadata["privacy_class"],
                metadata["captured_at"], now, now, RECORD_SCHEMA_VERSION,
            ),
        )
        conn.execute(
            """
            INSERT INTO evidence_revisions (
                revision_id, evidence_id, fidelity_level, media_type,
                hash_algorithm, content_hash, content_size_bytes,
                blob_relpath, created_at, verification_state,
                revision_schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?)
            """,
            (
                revision_id, evidence_id, metadata["fidelity_level"],
                metadata["media_type"], HASH_ALGORITHM, content_hash,
                content_size, blob_relpath, now, REVISION_SCHEMA_VERSION,
            ),
        )
        conn.execute(
            """
            INSERT INTO evidence_lifecycle (
                revision_id, evidence_id, lifecycle_state,
                retention_deadline, retention_policy_version,
                lifecycle_reason, payload_deleted, updated_at
            ) VALUES (?, ?, 'available', ?, ?, NULL, 0, ?)
            """,
            (
                revision_id,
                evidence_id,
                _add_days_iso(
                    metadata["captured_at"], _configured_retention_days()
                ),
                RETENTION_POLICY_VERSION,
                now,
            ),
        )

    def _cas_file_is_valid(self, relative: str, content_hash: str, size: int) -> bool:
        try:
            path = self._path_from_stored_reference(relative, content_hash)
            return _verify_file(path, content_hash, size)
        except RawEvidenceError:
            return False

    def _publish_blob(
        self,
        temp_path: Path,
        content_hash: str,
        size: int,
        *,
        remove_temp: bool = False,
    ) -> str:
        assert self.blobs_root is not None
        if _HASH_PATTERN.fullmatch(content_hash) is None:
            raise RawEvidenceError("integrity_failed")
        destination = self._cas_path(content_hash)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            _assert_owned_path(destination.parent, self.blobs_root)
            if destination.exists():
                if not _verify_file(destination, content_hash, size):
                    raise RawEvidenceError("integrity_conflict")
                if remove_temp:
                    self._remove_temp(temp_path)
                return self._relative_blob_path(destination)
            try:
                os.link(temp_path, destination)
            except FileExistsError:
                if not _verify_file(destination, content_hash, size):
                    raise RawEvidenceError("integrity_conflict")
            except OSError as exc:
                raise RawEvidenceError("content_publish_failed") from exc
            if remove_temp:
                self._remove_temp(temp_path)
            _chmod_private(destination)
            return self._relative_blob_path(destination)
        except RawEvidenceError:
            raise
        except OSError as exc:
            raise RawEvidenceError("content_publish_failed") from exc

    def _cas_path(self, content_hash: str) -> Path:
        assert self.blobs_root is not None
        if _HASH_PATTERN.fullmatch(content_hash) is None:
            raise RawEvidenceError("integrity_failed")
        destination = self.blobs_root / content_hash[:2] / content_hash
        _assert_owned_path(destination, self.blobs_root)
        return destination

    def _relative_blob_path(self, path: Path) -> str:
        assert self.root is not None
        try:
            relative = path.resolve(strict=False).relative_to(self.root.resolve(strict=False))
        except (ValueError, OSError) as exc:
            raise RawEvidenceError("invalid_stored_path") from exc
        result = PurePosixPath(*relative.parts).as_posix()
        if _RELATIVE_BLOB_PATTERN.fullmatch(result) is None:
            raise RawEvidenceError("invalid_stored_path")
        return result

    def _fetch_revision(self, revision_id: str) -> sqlite3.Row:
        revision_id = _validate_id(revision_id, "revision_id")
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT
                        r.revision_id, r.evidence_id, r.fidelity_level,
                        r.media_type, r.hash_algorithm, r.content_hash,
                        r.content_size_bytes, r.blob_relpath, r.created_at,
                        r.verification_state, r.revision_schema_version,
                        e.privacy_class,
                        COALESCE(l.lifecycle_state, e.lifecycle_state) AS lifecycle_state,
                        l.retention_deadline, l.retention_policy_version,
                        l.lifecycle_reason, l.tombstoned_at, l.expired_at,
                        l.purge_started_at, l.purged_at, l.payload_deleted
                    FROM evidence_revisions AS r
                    JOIN evidence_objects AS e ON e.evidence_id = r.evidence_id
                    LEFT JOIN evidence_lifecycle AS l ON l.revision_id = r.revision_id
                    WHERE r.revision_id = ?
                    """,
                    (revision_id,),
                ).fetchone()
        if row is None:
            raise RawEvidenceError("not_found")
        return row

    def _fetch_span_descriptor(
        self, span_id: str, revision_id: str
    ) -> sqlite3.Row | None:
        with self._lock:
            with self._connect() as conn:
                return conn.execute(
                    """
                    SELECT s.span_id, s.revision_id, s.locator_kind,
                           s.locator_schema_version, s.raw_byte_start,
                           s.raw_byte_end, s.span_hash_algorithm, s.span_hash,
                           s.created_at, s.descriptor_schema_version,
                           r.evidence_id, r.fidelity_level, r.media_type,
                           r.hash_algorithm, r.content_hash,
                           r.content_size_bytes, r.blob_relpath,
                           r.created_at AS revision_created_at,
                           r.verification_state, r.revision_schema_version,
                           e.privacy_class, l.lifecycle_state,
                           l.payload_deleted
                    FROM source_span_descriptors AS s
                    JOIN evidence_revisions AS r ON r.revision_id = s.revision_id
                    JOIN evidence_objects AS e ON e.evidence_id = r.evidence_id
                    JOIN evidence_lifecycle AS l ON l.revision_id = r.revision_id
                    WHERE s.span_id = ? AND s.revision_id = ?
                    """,
                    (span_id, revision_id),
                ).fetchone()

    def _read_verified(self, row: sqlite3.Row, *, return_content: bool) -> bytes:
        assert self.root is not None
        if row["verification_state"] != "verified":
            raise RawEvidenceError("integrity_failed")
        if row["lifecycle_state"] != "available":
            raise RawEvidenceError("evidence_unavailable")
        if row["hash_algorithm"] != HASH_ALGORITHM:
            raise RawEvidenceError("integrity_failed")
        expected_hash = row["content_hash"]
        expected_size = row["content_size_bytes"]
        if (
            not isinstance(expected_hash, str)
            or _HASH_PATTERN.fullmatch(expected_hash) is None
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
            or expected_size > self.limits.max_evidence_bytes
        ):
            self._best_effort_mark_integrity_failed(row["evidence_id"], row["revision_id"])
            raise RawEvidenceError("integrity_failed")
        path = self._path_from_stored_reference(row["blob_relpath"], expected_hash)
        digest = hashlib.sha256()
        size = 0
        output = bytearray() if return_content else None
        try:
            if not path.is_file():
                self._best_effort_mark_missing(row["evidence_id"], row["revision_id"])
                raise RawEvidenceError("evidence_missing")
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(_MAX_READ_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.limits.max_evidence_bytes:
                        raise RawEvidenceError("limit_exceeded")
                    digest.update(chunk)
                    if output is not None:
                        output.extend(chunk)
        except RawEvidenceError as exc:
            if exc.code != "evidence_missing":
                self._best_effort_mark_integrity_failed(
                    row["evidence_id"], row["revision_id"]
                )
            raise
        except (OSError, ValueError):
            self._best_effort_mark_integrity_failed(row["evidence_id"], row["revision_id"])
            raise RawEvidenceError("integrity_failed")
        if size != expected_size or digest.hexdigest() != expected_hash:
            self._best_effort_mark_integrity_failed(row["evidence_id"], row["revision_id"])
            raise RawEvidenceError("integrity_failed")
        return bytes(output or b"")

    def _path_from_stored_reference(self, relative: Any, expected_hash: str) -> Path:
        assert self.root is not None
        if not isinstance(relative, str) or _RELATIVE_BLOB_PATTERN.fullmatch(relative) is None:
            self._best_effort_mark_integrity_failed(None, None)
            raise RawEvidenceError("invalid_stored_path")
        if not relative.endswith(expected_hash):
            self._best_effort_mark_integrity_failed(None, None)
            raise RawEvidenceError("integrity_failed")
        parts = PurePosixPath(relative).parts
        path = self.root.joinpath(*parts)
        try:
            _assert_owned_path(path, self.blobs_root)
        except RawEvidenceError:
            raise RawEvidenceError("invalid_stored_path")
        return path

    def _best_effort_mark_integrity_failed(
        self,
        evidence_id: str | None,
        revision_id: str | None,
    ) -> None:
        if evidence_id is None or revision_id is None:
            return
        try:
            self._mark_integrity_failed(evidence_id, revision_id)
        except Exception:
            return

    def _best_effort_mark_missing(
        self,
        evidence_id: str | None,
        revision_id: str | None,
    ) -> None:
        if evidence_id is None or revision_id is None:
            return
        try:
            self._mark_missing(evidence_id, revision_id)
        except Exception:
            return

    @guarded_mutation("raw_evidence_missing_state")
    def _mark_missing(self, evidence_id: str, revision_id: str) -> None:
        with self._lock:
            try:
                with self._connect() as conn:
                    self._begin_write(conn)
                    conn.execute(
                        "UPDATE evidence_objects SET lifecycle_state = 'tombstoned', "
                        "updated_at = ? WHERE evidence_id = ?",
                        (_now_iso(), evidence_id),
                    )
                    conn.execute(
                        """
                        UPDATE evidence_lifecycle
                        SET lifecycle_state = 'missing', lifecycle_reason = 'payload_missing',
                            updated_at = ?
                        WHERE revision_id = ? AND evidence_id = ?
                        """,
                        (_now_iso(), revision_id, evidence_id),
                    )
                    conn.execute(
                        """
                        UPDATE memory_lineage SET status = 'evidence_missing', updated_at = ?
                        WHERE evidence_id = ? AND status != 'memory_deleted'
                        """,
                        (_now_iso(), evidence_id),
                    )
                    conn.commit()
            except sqlite3.Error as exc:
                raise RawEvidenceError("storage_unavailable") from exc

    @guarded_mutation("raw_evidence_integrity_state")
    def _mark_integrity_failed(self, evidence_id: str, revision_id: str) -> None:
        with self._lock:
            try:
                with self._connect() as conn:
                    self._begin_write(conn)
                    conn.execute(
                        "UPDATE evidence_revisions SET verification_state = 'failed' "
                        "WHERE revision_id = ? AND evidence_id = ?",
                        (revision_id, evidence_id),
                    )
                    conn.execute(
                        "UPDATE evidence_objects SET lifecycle_state = 'integrity_failed', "
                        "updated_at = ? WHERE evidence_id = ?",
                        (_now_iso(), evidence_id),
                    )
                    conn.execute(
                        """
                        UPDATE evidence_lifecycle
                        SET lifecycle_state = 'integrity_failed',
                            lifecycle_reason = 'integrity_failure',
                            updated_at = ?
                        WHERE revision_id = ? AND evidence_id = ?
                        """,
                        (_now_iso(), revision_id, evidence_id),
                    )
                    conn.commit()
            except sqlite3.Error as exc:
                raise RawEvidenceError("storage_unavailable") from exc

    def _remove_temp(self, path: Path) -> None:
        try:
            _assert_owned_path(path, self.temp_root)
            path.unlink(missing_ok=True)
        except (OSError, RawEvidenceError):
            return

    @staticmethod
    def _check_visibility(
        privacy_class: str,
        allow_sealed: bool,
        allow_restricted_admin: bool,
    ) -> None:
        if privacy_class == "ordinary":
            return
        if privacy_class == "sealed":
            if allow_sealed:
                return
            raise RawEvidenceError("sealed_access_denied")
        if privacy_class == "restricted_admin":
            if allow_restricted_admin:
                return
            raise RawEvidenceError("restricted_admin_access_denied")
        raise RawEvidenceError("privacy_class_invalid")

    def _validate_metadata(self, **values: Any) -> dict[str, Any]:
        for name in (
            "source_system",
            "source_kind",
            "source_scope",
            "upstream_source_id",
            "upstream_item_id",
            "source_occurrence_key",
            "captured_at",
        ):
            value = values[name]
            if value is not None:
                _validate_text(value, name, self.limits.max_metadata_chars)
        for name in ("source_system", "source_kind", "source_scope"):
            if not values[name]:
                raise RawEvidenceError("invalid_input")
        values["identity_origin"] = _validate_choice(
            values["identity_origin"], IDENTITY_ORIGINS, "identity_origin"
        )
        values["fidelity_level"] = _validate_choice(
            values["fidelity_level"], FIDELITY_LEVELS, "fidelity_level"
        )
        values["privacy_class"] = _validate_choice(
            values["privacy_class"], PRIVACY_CLASSES, "privacy_class"
        )
        _validate_text(values["media_type"], "media_type", self.limits.max_metadata_chars)
        if not values["media_type"]:
            raise RawEvidenceError("invalid_input")
        if values["identity_origin"] == "upstream" and not (
            values["upstream_source_id"] or values["upstream_item_id"]
        ):
            raise RawEvidenceError("identity_invalid")
        if values["identity_origin"] == "local" and not values["source_occurrence_key"]:
            raise RawEvidenceError("identity_invalid")
        if values["captured_at"] is None:
            values["captured_at"] = _now_iso()
        return values


def _validate_owned_root(
    value: str | Path | None,
    forbidden_roots: Iterable[str | Path],
) -> Path:
    candidate = _canonical_absolute(value, "evidence_root")
    for forbidden in forbidden_roots:
        forbidden_path = _canonical_absolute(forbidden, "forbidden_root")
        if _same_or_within(candidate, forbidden_path) or _same_or_within(
            forbidden_path, candidate
        ):
            raise RawEvidenceError("root_overlap")
    if candidate.exists() and not candidate.is_dir():
        raise RawEvidenceError("root_invalid")
    return candidate


def _canonical_absolute(value: str | Path | None, label: str) -> Path:
    if value is None or isinstance(value, bool):
        raise RawEvidenceError(f"{label}_invalid")
    try:
        candidate = Path(value).expanduser()
    except (TypeError, ValueError, OSError) as exc:
        raise RawEvidenceError(f"{label}_invalid") from exc
    if not candidate.is_absolute():
        raise RawEvidenceError(f"{label}_not_absolute")
    try:
        _reject_reparse_components(candidate)
        resolved = candidate.resolve(strict=False)
        _reject_reparse_components(resolved)
    except RawEvidenceError:
        raise
    except (OSError, RuntimeError) as exc:
        raise RawEvidenceError(f"{label}_unresolvable") from exc
    return resolved


def _same_or_within(candidate: Path, ancestor: Path) -> bool:
    try:
        candidate.relative_to(ancestor)
        return True
    except ValueError:
        return False


def _reject_reparse_components(path: Path) -> None:
    current = Path(path.anchor) if path.anchor else Path()
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current = current / part
        try:
            if current.is_symlink():
                raise RawEvidenceError("path_reparse_unsupported")
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RawEvidenceError("path_inspection_failed") from exc
        if getattr(info, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE:
            raise RawEvidenceError("path_reparse_unsupported")


def _assert_owned_path(path: Path, ancestor: Path | None) -> None:
    if ancestor is None:
        raise RawEvidenceError("storage_unavailable")
    _reject_reparse_components(path)
    try:
        resolved_path = path.resolve(strict=False)
        resolved_ancestor = ancestor.resolve(strict=False)
        resolved_path.relative_to(resolved_ancestor)
    except (ValueError, OSError, RuntimeError) as exc:
        raise RawEvidenceError("path_escape") from exc


def _verify_file(path: Path, expected_hash: str, expected_size: int) -> bool:
    try:
        if not path.is_file() or path.is_symlink():
            return False
        if path.stat().st_size != expected_size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_MAX_READ_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        return secrets.compare_digest(digest.hexdigest(), expected_hash)
    except (OSError, ValueError):
        return False


def _chmod_private(path: Path, *, directory: bool = False) -> None:
    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except OSError:
        # Windows ACL enforcement is deployment-specific.  The path remains
        # isolated and the implementation does not claim ACL equivalence.
        return


def _validate_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise RawEvidenceError(f"{label}_invalid")
    return value


def _validate_memory_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _MEMORY_ID_PATTERN.fullmatch(value) is None:
        raise RawEvidenceError(f"{label}_invalid")
    return value


def _validate_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise RawEvidenceError(f"{label}_invalid")
    return value


def _validate_raw_byte_range(start: Any, end: Any, content_size: Any) -> None:
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or isinstance(content_size, bool)
        or not isinstance(content_size, int)
        or start < 0
        or start >= end
        or end > content_size
    ):
        raise RawEvidenceError("raw_byte_range_invalid")


def _validate_text(value: Any, label: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RawEvidenceError(f"{label}_invalid")
    if len(value) > max_chars:
        raise RawEvidenceError("metadata_too_large")
    return value


def _validate_choice(value: Any, choices: frozenset[str], label: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise RawEvidenceError(f"{label}_invalid")
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RawEvidenceError("timestamp_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _add_days_iso(value: str, days: int) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (parsed + timedelta(days=days)).astimezone(timezone.utc).isoformat(
            timespec="seconds"
        )
    except (TypeError, ValueError) as exc:
        raise RawEvidenceError("captured_at_invalid") from exc


def _configured_retention_days() -> int:
    value = os.environ.get("OMBRE_RAW_EVIDENCE_RETENTION_DAYS")
    if value is None or value == "":
        return DEFAULT_RETENTION_DAYS
    if not value.isdecimal():
        raise RawEvidenceError("retention_config_invalid")
    days = int(value)
    if not 1 <= days <= 365:
        raise RawEvidenceError("retention_config_invalid")
    return days


__all__ = [
    "FIDELITY_LEVELS",
    "HASH_ALGORITHM",
    "LINEAGE_CITATION_SCHEMA_VERSION",
    "CAS_STATES",
    "DEFAULT_RETENTION_DAYS",
    "IDENTITY_ORIGINS",
    "IMPORT_ITEM_STATUSES",
    "IMPORT_RUN_STATUSES",
    "LINEAGE_KINDS",
    "LINEAGE_STATUSES",
    "LIFECYCLE_STATES",
    "PRIVACY_CLASSES",
    "RawEvidenceError",
    "RawEvidenceLimits",
    "RawEvidenceStore",
    "RETENTION_POLICY_VERSION",
    "SCHEMA_VERSION",
    "SOURCE_SPAN_CANDIDATE_SCHEMA_VERSION",
    "SPAN_DESCRIPTOR_SCHEMA_VERSION",
    "SPAN_HASH_ALGORITHM",
    "SPAN_LOCATOR_KIND",
    "SPAN_LOCATOR_SCHEMA_VERSION",
    "SPAN_VERIFICATION_STATES",
    "VERIFICATION_STATES",
]
