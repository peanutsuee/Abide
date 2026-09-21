"""O5D Raw Evidence lifecycle controls.

The lifecycle runner is deliberately internal and dormant unless a caller
explicitly invokes it.  It owns no public route, scheduler, backup flow, or
ordinary-memory behavior.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from maintenance_write_gate import guarded_mutation
from raw_evidence_store import (
    CAS_STATES,
    HASH_ALGORITHM,
    LIFECYCLE_STATES,
    RawEvidenceError,
    RawEvidenceStore,
    _now_iso,
)
from raw_evidence_backup_authority import (
    BACKUP_ROOT_ENV,
    BackupAuthorityError,
    RawEvidenceBackupAuthority,
)


RETENTION_ENV = "OMBRE_RAW_EVIDENCE_RETENTION_DAYS"
AUDIT_RETENTION_ENV = "OMBRE_RAW_EVIDENCE_AUDIT_RETENTION_DAYS"
PURGE_BATCH_ENV = "OMBRE_RAW_EVIDENCE_PURGE_BATCH_SIZE"
DEFAULT_RETENTION_DAYS = 30
DEFAULT_AUDIT_RETENTION_DAYS = 365
DEFAULT_PURGE_BATCH_SIZE = 100
POLICY_VERSION = "foundation_v1"


@dataclass(frozen=True)
class LifecycleConfig:
    retention_days: int = DEFAULT_RETENTION_DAYS
    audit_retention_days: int = DEFAULT_AUDIT_RETENTION_DAYS
    purge_batch_size: int = DEFAULT_PURGE_BATCH_SIZE
    max_bytes_per_pass: int = 64 * 1024 * 1024
    max_seconds_per_pass: float = 10.0

    def validate(self) -> None:
        if not 1 <= self.retention_days <= 365:
            raise RawEvidenceError("retention_config_invalid")
        if not 30 <= self.audit_retention_days <= 3650:
            raise RawEvidenceError("audit_retention_config_invalid")
        if not 1 <= self.purge_batch_size <= 1000:
            raise RawEvidenceError("purge_batch_config_invalid")
        if self.max_bytes_per_pass <= 0 or self.max_seconds_per_pass <= 0:
            raise RawEvidenceError("lifecycle_bounds_invalid")

    @classmethod
    def from_env(cls) -> "LifecycleConfig":
        values = {
            "retention_days": _env_int(RETENTION_ENV, DEFAULT_RETENTION_DAYS),
            "audit_retention_days": _env_int(
                AUDIT_RETENTION_ENV, DEFAULT_AUDIT_RETENTION_DAYS
            ),
            "purge_batch_size": _env_int(PURGE_BATCH_ENV, DEFAULT_PURGE_BATCH_SIZE),
        }
        config = cls(**values)
        config.validate()
        return config


class RawEvidenceLifecycle:
    """Explicitly invoked lifecycle operations for one evidence store."""

    def __init__(self, store: RawEvidenceStore, config: LifecycleConfig | None = None):
        if not isinstance(store, RawEvidenceStore):
            raise RawEvidenceError("store_invalid")
        self.store = store
        self.config = config or LifecycleConfig.from_env()
        self.config.validate()

    def _bound_backup_authority(self) -> RawEvidenceBackupAuthority | None:
        repository_id = self.store.backup_repository_id()
        if repository_id is None:
            return None
        root = os.environ.get(BACKUP_ROOT_ENV, "")
        if not root:
            raise RawEvidenceError("backup_authority_unavailable")
        try:
            authority = RawEvidenceBackupAuthority.open(
                root,
                live_root=self.store.root,
                forbidden_roots=(self.store.root or Path("."),),
            )
            if authority.repository_id() != repository_id:
                raise BackupAuthorityError("backup_repository_mismatch")
            return authority
        except BackupAuthorityError as exc:
            raise RawEvidenceError(exc.code) from exc

    @guarded_mutation("raw_evidence_lifecycle_redact")
    def redact(
        self,
        evidence_id: str,
        *,
        reason: str = "redaction",
        actor_class: str = "system",
    ) -> dict[str, Any]:
        """Durably tombstone all revisions of one logical evidence object."""

        self.store._require_enabled()
        evidence_id = _validate_id(evidence_id, "evidence_id")
        reason = _bounded_reason(reason)
        actor_class = _bounded_actor(actor_class)
        now = _now_iso()
        revocation = None
        authority = self._bound_backup_authority()
        if authority is not None:
            revocation = authority.begin_revocation(
                uuid.uuid4().hex,
                target_evidence_id=evidence_id,
                reason=reason,
            )
        changed = 0
        with self.store._lock:
            with self.store._connect() as conn:
                self.store._begin_write(conn)
                rows = conn.execute(
                    """
                    SELECT revision_id, lifecycle_state
                    FROM evidence_lifecycle
                    WHERE evidence_id = ?
                    ORDER BY revision_id
                    """,
                    (evidence_id,),
                ).fetchall()
                if not rows:
                    conn.rollback()
                    raise RawEvidenceError("not_found")
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
                for row in rows:
                    if row["lifecycle_state"] == "purged":
                        continue
                    operation_id = uuid.uuid4().hex
                    conn.execute(
                        """
                        UPDATE evidence_lifecycle SET lifecycle_state = 'tombstoned',
                            lifecycle_reason = ?, tombstoned_at = COALESCE(tombstoned_at, ?),
                            purge_operation_id = NULL, payload_deleted = 0,
                            updated_at = ?
                        WHERE revision_id = ?
                        """,
                        (reason, now, now, row["revision_id"]),
                    )
                    conn.execute(
                        """
                        INSERT INTO lifecycle_audit (
                            lifecycle_operation_id, evidence_id, revision_id,
                            from_state, to_state, reason, occurred_at,
                            actor_class, payload_deleted, reconciliation_result
                        ) VALUES (?, ?, ?, ?, 'tombstoned', ?, ?, ?, 0, NULL)
                        """,
                        (
                            operation_id, evidence_id, row["revision_id"],
                            row["lifecycle_state"], reason, now, actor_class,
                        ),
                    )
                    changed += 1
                if changed:
                    conn.execute(
                        """
                        UPDATE evidence_objects SET lifecycle_state = 'tombstoned',
                            updated_at = ? WHERE evidence_id = ?
                        """,
                        (now, evidence_id),
                    )
                if changed:
                    conn.execute(
                        """
                        UPDATE memory_lineage SET status = 'source_redacted', updated_at = ?
                        WHERE evidence_id = ? AND status != 'memory_deleted'
                        """,
                        (now, evidence_id),
                    )
                conn.commit()
        if authority is not None and revocation is not None:
            authority.apply_revocation(revocation["operation_id"])
        return {
            "evidence_id": evidence_id,
            "state": "tombstoned" if changed else "purged",
            "changed": changed,
        }

    @guarded_mutation("raw_evidence_lifecycle_run")
    def run(
        self,
        *,
        now: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Run one bounded expiration, purge, and reconciliation pass."""

        self.store._require_enabled()
        current = now or _now_iso()
        _parse_iso(current)
        started = time.monotonic()
        result: dict[str, Any] = {
            "dry_run": dry_run,
            "publish_recovered": 0,
            "expired": 0,
            "purged": 0,
            "pending": 0,
            "bytes": 0,
            "audit_pruned": 0,
        }
        if dry_run:
            return self._dry_run(current)

        result["publish_recovered"] = self._recover_publish_pending(current)

        while (
            result["expired"] < self.config.purge_batch_size
            and time.monotonic() - started < self.config.max_seconds_per_pass
        ):
            changed = self._expire_one(current)
            if not changed:
                break
            result["expired"] += 1

        while (
            result["purged"] < self.config.purge_batch_size
            and result["bytes"] < self.config.max_bytes_per_pass
            and time.monotonic() - started < self.config.max_seconds_per_pass
        ):
            outcome = self._purge_one(current)
            if outcome is None:
                break
            result["pending"] += 1
            if outcome["purged"]:
                result["purged"] += outcome["count"]
                result["bytes"] += outcome["bytes"]

        result["audit_pruned"] = self._prune_audit(current)
        return result

    def _recover_publish_pending(self, now: str) -> int:
        """Resolve only durable CAS publish claims left by a crashed capture."""

        recovered = 0
        with self.store._lock:
            with self.store._connect() as conn:
                self.store._begin_write(conn)
                rows = conn.execute(
                    """
                    SELECT * FROM cas_objects
                    WHERE state = 'publish_pending'
                    ORDER BY updated_at, content_hash
                    LIMIT ?
                    """,
                    (self.config.purge_batch_size,),
                ).fetchall()
                for row in rows:
                    refs = conn.execute(
                        """
                        SELECT COUNT(*) FROM evidence_revisions
                        WHERE hash_algorithm = ? AND content_hash = ?
                        """,
                        (row["hash_algorithm"], row["content_hash"]),
                    ).fetchone()[0]
                    if refs:
                        if row["hash_algorithm"] != HASH_ALGORITHM:
                            continue
                        try:
                            path = self.store._path_from_stored_reference(
                                row["blob_relpath"], row["content_hash"]
                            )
                            if not _verify_owned_file(
                                path,
                                row["content_hash"],
                                row["content_size_bytes"],
                            ):
                                continue
                        except (OSError, RawEvidenceError):
                            continue
                        conn.execute(
                            """
                            UPDATE cas_objects SET state = 'live', operation_id = NULL,
                                updated_at = ?
                            WHERE hash_algorithm = ? AND content_hash = ?
                              AND state = 'publish_pending'
                            """,
                            (now, row["hash_algorithm"], row["content_hash"]),
                        )
                        recovered += 1
                        continue
                    try:
                        path = self.store._path_from_stored_reference(
                            row["blob_relpath"], row["content_hash"]
                        )
                        if path.exists():
                            if not _verify_owned_file(
                                path, row["content_hash"], row["content_size_bytes"]
                            ):
                                continue
                            path.unlink()
                        conn.execute(
                            """
                            UPDATE cas_objects SET state = 'purged', operation_id = NULL,
                                updated_at = ?
                            WHERE hash_algorithm = ? AND content_hash = ?
                              AND state = 'publish_pending'
                            """,
                            (now, row["hash_algorithm"], row["content_hash"]),
                        )
                        recovered += 1
                    except (OSError, RawEvidenceError):
                        continue
                conn.commit()
        return recovered

    def _dry_run(self, now: str) -> dict[str, Any]:
        with self.store._lock:
            with self.store._connect() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS count, COALESCE(SUM(r.content_size_bytes), 0) AS bytes
                    FROM evidence_lifecycle AS l
                    JOIN evidence_revisions AS r ON r.revision_id = l.revision_id
                    WHERE l.lifecycle_state IN ('available', 'tombstoned', 'expired')
                      AND l.retention_deadline <= ?
                    """,
                    (now,),
                ).fetchone()
                reasons = conn.execute(
                    """
                    SELECT COALESCE(lifecycle_reason, 'none') AS reason, COUNT(*) AS count
                    FROM evidence_lifecycle
                    WHERE lifecycle_state IN ('available', 'tombstoned', 'expired')
                      AND retention_deadline <= ?
                    GROUP BY COALESCE(lifecycle_reason, 'none')
                    ORDER BY reason
                    LIMIT ?
                    """,
                    (now, self.config.purge_batch_size),
                ).fetchall()
        return {
            "dry_run": True,
            "eligible_count": int(row["count"]),
            "eligible_bytes": int(row["bytes"]),
            "reason_counts": {item["reason"]: int(item["count"]) for item in reasons},
            "mutations": 0,
        }

    def _expire_one(self, now: str) -> bool:
        with self.store._lock:
            with self.store._connect() as conn:
                self.store._begin_write(conn)
                row = conn.execute(
                    """
                    SELECT revision_id, evidence_id, lifecycle_state
                    FROM evidence_lifecycle
                    WHERE lifecycle_state = 'available'
                      AND retention_deadline <= ?
                    ORDER BY retention_deadline, revision_id
                    LIMIT 1
                    """,
                    (now,),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return False
                operation_id = uuid.uuid4().hex
                conn.execute(
                    """
                    UPDATE evidence_lifecycle SET lifecycle_state = 'expired',
                        lifecycle_reason = 'retention_expired', expired_at = ?,
                        updated_at = ?
                    WHERE revision_id = ? AND lifecycle_state = 'available'
                    """,
                    (now, now, row["revision_id"]),
                )
                conn.execute(
                    """
                    INSERT INTO lifecycle_audit (
                        lifecycle_operation_id, evidence_id, revision_id,
                        from_state, to_state, reason, occurred_at,
                        actor_class, payload_deleted, reconciliation_result
                    ) VALUES (?, ?, ?, 'available', 'expired',
                        'retention_expired', ?, 'system', 0, NULL)
                    """,
                    (operation_id, row["evidence_id"], row["revision_id"], now),
                )
                conn.execute(
                    """
                    UPDATE memory_lineage SET status = 'source_expired', updated_at = ?
                    WHERE evidence_id = ?
                      AND status NOT IN ('source_redacted', 'memory_deleted')
                    """,
                    (now, row["evidence_id"]),
                )
                conn.execute(
                    """
                    UPDATE evidence_objects SET lifecycle_state = 'tombstoned', updated_at = ?
                    WHERE evidence_id = ? AND lifecycle_state = 'available'
                    """,
                    (now, row["evidence_id"]),
                )
                conn.commit()
                return True

    def _purge_one(self, now: str) -> dict[str, Any] | None:
        claim = self._claim_purge(now)
        if claim is None:
            return None
        cas, revisions = claim
        path = None
        try:
            path = self.store._path_from_stored_reference(
                cas["blob_relpath"], cas["content_hash"]
            )
            if not path.exists():
                self._mark_missing_payload(cas, revisions, now)
                return {"purged": False, "count": 0, "bytes": 0}
            if not _verify_owned_file(
                path,
                cas["content_hash"],
                cas["content_size_bytes"],
            ):
                self._mark_integrity_failure(cas, revisions, now)
                return {"purged": False, "count": 0, "bytes": 0}
            path.unlink()
            return self._finalize_purge(cas, revisions, now)
        except (OSError, RawEvidenceError):
            return {"purged": False, "count": 0, "bytes": 0}

    def _claim_purge(self, now: str):
        with self.store._lock:
            with self.store._connect() as conn:
                self.store._begin_write(conn)
                row = conn.execute(
                    """
                    SELECT l.revision_id, l.evidence_id, r.content_hash,
                           r.hash_algorithm, r.content_size_bytes, r.blob_relpath,
                           c.state AS cas_state, c.operation_id AS cas_operation_id
                    FROM evidence_lifecycle AS l
                    JOIN evidence_revisions AS r ON r.revision_id = l.revision_id
                    JOIN cas_objects AS c
                      ON c.hash_algorithm = r.hash_algorithm
                     AND c.content_hash = r.content_hash
                    WHERE l.lifecycle_state IN ('tombstoned', 'expired', 'purge_pending')
                      AND l.retention_deadline <= ?
                    ORDER BY l.retention_deadline, l.revision_id
                    LIMIT 1
                    """,
                    (now,),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return None
                cas = conn.execute(
                    """
                    SELECT * FROM cas_objects
                    WHERE hash_algorithm = ? AND content_hash = ?
                    """,
                    (row["hash_algorithm"], row["content_hash"]),
                ).fetchone()
                refs = conn.execute(
                    """
                    SELECT l.revision_id, l.evidence_id, l.lifecycle_state,
                           r.content_size_bytes
                    FROM evidence_lifecycle AS l
                    JOIN evidence_revisions AS r ON r.revision_id = l.revision_id
                    WHERE r.hash_algorithm = ? AND r.content_hash = ?
                    """,
                    (row["hash_algorithm"], row["content_hash"]),
                ).fetchall()
                blockers = [
                    item for item in refs
                    if item["lifecycle_state"] not in {
                        "tombstoned", "expired", "purge_pending", "purged"
                    }
                ]
                if blockers:
                    conn.rollback()
                    return None
                operation_id = cas["operation_id"] if cas["state"] == "gc_pending" else uuid.uuid4().hex
                if cas["state"] == "live":
                    conn.execute(
                        """
                        UPDATE cas_objects SET state = 'gc_pending', operation_id = ?, updated_at = ?
                        WHERE hash_algorithm = ? AND content_hash = ? AND state = 'live'
                        """,
                        (operation_id, now, row["hash_algorithm"], row["content_hash"]),
                    )
                elif cas["state"] != "gc_pending":
                    conn.rollback()
                    return None
                for item in refs:
                    if item["lifecycle_state"] == "purged":
                        continue
                    conn.execute(
                        """
                        UPDATE evidence_lifecycle SET lifecycle_state = 'purge_pending',
                            purge_started_at = COALESCE(purge_started_at, ?),
                            purge_operation_id = ?, updated_at = ?
                        WHERE revision_id = ?
                        """,
                        (now, operation_id, now, item["revision_id"]),
                    )
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO lifecycle_audit (
                            lifecycle_operation_id, evidence_id, revision_id,
                            from_state, to_state, reason, occurred_at,
                            actor_class, payload_deleted, reconciliation_result
                        ) VALUES (?, ?, ?, ?, 'purge_pending', 'policy_purge', ?,
                            'system', 0, NULL)
                        """,
                        (
                            uuid.uuid4().hex, item["evidence_id"], item["revision_id"],
                            item["lifecycle_state"], now,
                        ),
                    )
                conn.commit()
                claimed_cas = dict(cas)
                claimed_cas["state"] = "gc_pending"
                claimed_cas["operation_id"] = operation_id
                return claimed_cas, refs

    def _finalize_purge(self, cas, revisions, now: str) -> dict[str, Any]:
        with self.store._lock:
            with self.store._connect() as conn:
                self.store._begin_write(conn)
                current = conn.execute(
                    """
                    SELECT state, operation_id FROM cas_objects
                    WHERE hash_algorithm = ? AND content_hash = ?
                    """,
                    (cas["hash_algorithm"], cas["content_hash"]),
                ).fetchone()
                if current is None or current["state"] != "gc_pending" or current["operation_id"] != cas["operation_id"]:
                    conn.rollback()
                    return {"purged": False, "count": 0, "bytes": 0}
                purged_count = 0
                for item in revisions:
                    if item["lifecycle_state"] == "purged":
                        continue
                    cursor = conn.execute(
                        """
                        UPDATE evidence_lifecycle SET lifecycle_state = 'purged',
                            purged_at = ?, payload_deleted = 1, updated_at = ?
                        WHERE revision_id = ? AND lifecycle_state = 'purge_pending'
                          AND purge_operation_id = ?
                        """,
                        (now, now, item["revision_id"], cas["operation_id"]),
                    )
                    purged_count += max(cursor.rowcount, 0)
                    conn.execute(
                        """
                        INSERT INTO lifecycle_audit (
                            lifecycle_operation_id, evidence_id, revision_id,
                            from_state, to_state, reason, occurred_at,
                            actor_class, payload_deleted, reconciliation_result
                        ) VALUES (?, ?, ?, 'purge_pending', 'purged',
                            'policy_purge', ?, 'system', 1, 'payload_deleted')
                        """,
                        (uuid.uuid4().hex, item["evidence_id"], item["revision_id"], now),
                    )
                conn.execute(
                    """
                    UPDATE cas_objects SET state = 'purged', operation_id = NULL,
                        updated_at = ?
                    WHERE hash_algorithm = ? AND content_hash = ?
                      AND state = 'gc_pending' AND operation_id = ?
                    """,
                    (now, cas["hash_algorithm"], cas["content_hash"], cas["operation_id"]),
                )
                conn.commit()
        return {
            "purged": True,
            "count": purged_count,
            "bytes": int(cas["content_size_bytes"]),
        }

    def _mark_integrity_failure(self, cas, revisions, now: str) -> None:
        with self.store._lock:
            with self.store._connect() as conn:
                self.store._begin_write(conn)
                conn.execute(
                    """
                    UPDATE cas_objects SET state = 'live', operation_id = NULL, updated_at = ?
                    WHERE hash_algorithm = ? AND content_hash = ?
                      AND state = 'gc_pending' AND operation_id = ?
                    """,
                    (now, cas["hash_algorithm"], cas["content_hash"], cas["operation_id"]),
                )
                for item in revisions:
                    conn.execute(
                        """
                        UPDATE evidence_lifecycle SET lifecycle_state = 'integrity_failed',
                            lifecycle_reason = 'integrity_failure',
                            purge_operation_id = NULL, updated_at = ?
                        WHERE revision_id = ? AND purge_operation_id = ?
                        """,
                        (now, item["revision_id"], cas["operation_id"]),
                    )
                    conn.execute(
                        """
                        UPDATE memory_lineage SET status = 'integrity_failed', updated_at = ?
                        WHERE evidence_id = ? AND status != 'memory_deleted'
                        """,
                        (now, item["evidence_id"]),
                    )
                conn.commit()

    def _mark_missing_payload(self, cas, revisions, now: str) -> None:
        """Leave a missing payload non-purged and record source loss."""

        with self.store._lock:
            with self.store._connect() as conn:
                self.store._begin_write(conn)
                conn.execute(
                    """
                    UPDATE cas_objects SET state = 'live', operation_id = NULL,
                        updated_at = ?
                    WHERE hash_algorithm = ? AND content_hash = ?
                      AND state = 'gc_pending' AND operation_id = ?
                    """,
                    (
                        now,
                        cas["hash_algorithm"],
                        cas["content_hash"],
                        cas["operation_id"],
                    ),
                )
                for item in revisions:
                    if item["lifecycle_state"] == "purged":
                        continue
                    conn.execute(
                        """
                        UPDATE evidence_objects SET lifecycle_state = 'tombstoned',
                            updated_at = ?
                        WHERE evidence_id = ?
                        """,
                        (now, item["evidence_id"]),
                    )
                    conn.execute(
                        """
                        UPDATE evidence_lifecycle SET lifecycle_state = 'missing',
                            lifecycle_reason = 'payload_missing',
                            purge_operation_id = NULL, payload_deleted = 0,
                            updated_at = ?
                        WHERE revision_id = ? AND purge_operation_id = ?
                        """,
                        (now, item["revision_id"], cas["operation_id"]),
                    )
                    conn.execute(
                        """
                        UPDATE memory_lineage SET status = 'evidence_missing',
                            updated_at = ?
                        WHERE evidence_id = ? AND status != 'memory_deleted'
                        """,
                        (now, item["evidence_id"]),
                    )
                    conn.execute(
                        """
                        INSERT INTO lifecycle_audit (
                            lifecycle_operation_id, evidence_id, revision_id,
                            from_state, to_state, reason, occurred_at,
                            actor_class, payload_deleted, reconciliation_result
                        ) VALUES (?, ?, ?, 'purge_pending', 'missing',
                            'payload_missing', ?, 'system', 0, 'payload_missing')
                        """,
                        (
                            uuid.uuid4().hex,
                            item["evidence_id"],
                            item["revision_id"],
                            now,
                        ),
                    )
                conn.commit()

    @guarded_mutation("raw_evidence_lifecycle_audit_prune")
    def prune_audit(self, *, now: str | None = None, dry_run: bool = False) -> int:
        current = now or _now_iso()
        cutoff = _parse_iso(current) - timedelta(days=self.config.audit_retention_days)
        cutoff_iso = cutoff.astimezone(timezone.utc).isoformat(timespec="seconds")
        with self.store._lock:
            with self.store._connect() as conn:
                if dry_run:
                    return int(conn.execute(
                        "SELECT COUNT(*) FROM lifecycle_audit WHERE occurred_at < ?",
                        (cutoff_iso,),
                    ).fetchone()[0])
                self.store._begin_write(conn)
                cursor = conn.execute(
                    """
                    DELETE FROM lifecycle_audit
                    WHERE lifecycle_operation_id IN (
                        SELECT lifecycle_operation_id FROM lifecycle_audit
                        WHERE occurred_at < ?
                        ORDER BY occurred_at, lifecycle_operation_id
                        LIMIT ?
                    )
                    """,
                    (cutoff_iso, self.config.purge_batch_size),
                )
                conn.commit()
                return int(cursor.rowcount)

    def _prune_audit(self, now: str) -> int:
        return self.prune_audit(now=now)


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    if not value.isdecimal():
        raise RawEvidenceError("config_invalid")
    try:
        return int(value)
    except ValueError as exc:
        raise RawEvidenceError("config_invalid") from exc


def _parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise RawEvidenceError("timestamp_invalid") from exc


def _validate_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 32:
        raise RawEvidenceError(f"{label}_invalid")
    try:
        int(value, 16)
    except ValueError as exc:
        raise RawEvidenceError(f"{label}_invalid") from exc
    return value


def _bounded_reason(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64 or not value.isascii():
        raise RawEvidenceError("lifecycle_reason_invalid")
    return value


def _bounded_actor(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 32 or not value.isascii():
        raise RawEvidenceError("actor_invalid")
    return value


def _verify_owned_file(path, expected_hash: str, expected_size: int) -> bool:
    from raw_evidence_store import _verify_file

    return _verify_file(path, expected_hash, expected_size)


__all__ = ["LifecycleConfig", "RawEvidenceLifecycle", "POLICY_VERSION"]
