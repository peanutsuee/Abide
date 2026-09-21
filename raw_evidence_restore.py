"""O5E new-root-only Raw Evidence restore operations."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
from typing import Any

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from maintenance_write_gate import guarded_mutation
from raw_evidence_backup import (
    BUNDLE_SUFFIX,
    RawEvidenceBackupError,
    RawEvidenceBackupService,
    _canonical_json,
    _cleanup_owned_staging,
    _copy_verified,
    _create_owned_staging,
    _decrypt_and_validate,
    _parse_time,
    _strict_within,
    _timestamp,
    _validate_archive_member,
    _verify_registry_snapshot,
)
from raw_evidence_backup_authority import (
    BACKUP_ROOT_ENV,
    BackupAuthorityError,
    RawEvidenceBackupAuthority,
)
from raw_evidence_store import HASH_ALGORITHM, RawEvidenceError, RawEvidenceStore, SCHEMA_VERSION


class RawEvidenceRestoreError(RawEvidenceError):
    """Stable, content-free restore failure."""


class RawEvidenceRestoreService:
    """Verify and publish a backup only into a new, operator-selected root."""

    def __init__(
        self,
        store: RawEvidenceStore,
        authority: RawEvidenceBackupAuthority,
        recipient_private_key: X25519PrivateKey,
    ) -> None:
        if not isinstance(store, RawEvidenceStore) or store.is_disabled:
            raise RawEvidenceRestoreError("store_disabled")
        if not isinstance(authority, RawEvidenceBackupAuthority):
            raise RawEvidenceRestoreError("backup_authority_invalid")
        if not isinstance(recipient_private_key, X25519PrivateKey):
            raise RawEvidenceRestoreError("backup_key_invalid")
        bound = store.backup_repository_id()
        if bound is None:
            raise RawEvidenceRestoreError("backup_repository_unbound")
        if bound != authority.repository_id():
            raise RawEvidenceRestoreError("backup_repository_mismatch")
        self.store = store
        self.authority = authority
        self.recipient_private_key = recipient_private_key
        self.backup = RawEvidenceBackupService(
            store,
            authority,
            recipient_private_key.public_key(),
        )

    @classmethod
    def from_env(
        cls,
        store: RawEvidenceStore,
        recipient_private_key: X25519PrivateKey,
        *,
        forbidden_roots: tuple[str | Path, ...] = (),
    ) -> "RawEvidenceRestoreService":
        root = os.environ.get(BACKUP_ROOT_ENV, "")
        if not root:
            raise RawEvidenceRestoreError("backup_repository_missing")
        try:
            authority = RawEvidenceBackupAuthority.open(
                root,
                live_root=store.root,
                forbidden_roots=forbidden_roots,
            )
        except BackupAuthorityError as exc:
            raise RawEvidenceRestoreError(exc.code) from exc
        return cls(store, authority, recipient_private_key)

    def verify(self, bundle_name: str, *, now: str | None = None) -> dict[str, Any]:
        """Verify a bundle and authority state without changing live state."""

        try:
            result = self.backup.verify(
                bundle_name,
                self.recipient_private_key,
                now=now,
            )
        except RawEvidenceBackupError as exc:
            raise RawEvidenceRestoreError(exc.code) from exc
        return result

    @guarded_mutation("raw_evidence_restore_stage")
    def stage(
        self,
        bundle_name: str,
        target_root: str | Path,
        *,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Validate a bundle and prepare an owned staging root.

        The returned stage can only be published to the exact absent target
        recorded in its marker.  It is never a live-root replacement.
        """

        target = _validate_new_target(target_root, self.store, self.authority)
        operation_id = os.urandom(16).hex()
        work = self.authority.staging_root / f"restore-{operation_id}"
        _create_owned_staging(work, operation_id)
        stage_container = target.parent / f".{target.name}.o5e-{operation_id}"
        stage_root = stage_container / "root"
        try:
            loaded = self._load_activatable(bundle_name, now=now, work=work)
            manifest = loaded["manifest"]
            _create_owned_staging(stage_container, operation_id)
            _prepare_staged_root(
                loaded["restore_root"],
                stage_root,
                manifest,
                self.authority.repository_id(),
                now=now or _timestamp(),
            )
            marker = {
                "operation_id": operation_id,
                "target_root": str(target),
                "backup_id": manifest["backup_id"],
                "bundle_name": bundle_name,
                "restore_epoch": manifest["restore_epoch"],
            }
            (stage_root / ".o5e-restore-marker").write_text(
                json.dumps(marker, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            return {
                "status": "staged",
                "operation_id": operation_id,
                "backup_id": manifest["backup_id"],
                "stage_root": str(stage_root),
                "target_root": str(target),
                "restore_epoch": manifest["restore_epoch"],
            }
        except (RawEvidenceBackupError, BackupAuthorityError) as exc:
            _cleanup_owned_staging(work, operation_id)
            _cleanup_owned_staging(stage_container, operation_id)
            raise RawEvidenceRestoreError(exc.code) from exc
        except RawEvidenceRestoreError:
            _cleanup_owned_staging(work, operation_id)
            _cleanup_owned_staging(stage_container, operation_id)
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            _cleanup_owned_staging(work, operation_id)
            _cleanup_owned_staging(stage_container, operation_id)
            raise RawEvidenceRestoreError("restore_staging_failed") from exc
        finally:
            _cleanup_owned_staging(loaded["operation_root"], loaded["operation_id"]) if "loaded" in locals() else None

    @guarded_mutation("raw_evidence_restore_create_root")
    def create_root(self, stage_root: str | Path, target_root: str | Path) -> dict[str, Any]:
        """Publish one validated stage into an absent new root."""

        stage = Path(stage_root)
        target = _validate_new_target(target_root, self.store, self.authority)
        marker_path = stage / ".o5e-restore-marker"
        owner_path = stage.parent / ".o5e-owner"
        try:
            if not stage.is_dir() or not marker_path.is_file() or not owner_path.is_file():
                raise RawEvidenceRestoreError("restore_stage_invalid")
            if _contains_reparse(stage) or _contains_reparse(stage.parent):
                raise RawEvidenceRestoreError("restore_stage_invalid")
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
            if not isinstance(marker, dict) or not isinstance(owner, dict):
                raise RawEvidenceRestoreError("restore_stage_invalid")
            if marker.get("target_root") != str(target) or owner.get("operation_id") != marker.get("operation_id"):
                raise RawEvidenceRestoreError("restore_stage_invalid")
            _verify_staged_root(stage, self.authority.repository_id())
            try:
                os.rename(stage, target)
            except FileExistsError as exc:
                raise RawEvidenceRestoreError("restore_destination_exists") from exc
            except PermissionError:
                # Some Windows volumes deny a directory move across ACL
                # boundaries even when both parents are writable.  The
                # destination is still required to be absent; copy into that
                # new root, verify it, then remove only this owned stage.
                if target.exists():
                    raise RawEvidenceRestoreError("restore_destination_exists")
                try:
                    shutil.copytree(stage, target, dirs_exist_ok=False)
                    _verify_staged_root(target, self.authority.repository_id())
                except (OSError, sqlite3.Error) as exc:
                    with suppress(OSError):
                        shutil.rmtree(target)
                    raise RawEvidenceRestoreError("restore_publish_failed") from exc
            except OSError as exc:
                raise RawEvidenceRestoreError("restore_publish_failed") from exc
            with suppress(OSError):
                (target / ".o5e-restore-marker").unlink()
            with suppress(OSError):
                shutil.rmtree(stage.parent)
            return {
                "status": "created",
                "target_root": str(target),
                "backup_id": marker["backup_id"],
                "restore_epoch": marker["restore_epoch"],
            }
        except RawEvidenceRestoreError:
            raise
        except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
            raise RawEvidenceRestoreError("restore_stage_invalid") from exc

    @guarded_mutation("raw_evidence_restore_create_root")
    def create_root_from_bundle(
        self,
        bundle_name: str,
        target_root: str | Path,
        *,
        now: str | None = None,
    ) -> dict[str, Any]:
        staged = self.stage(bundle_name, target_root, now=now)
        try:
            return self.create_root(staged["stage_root"], target_root)
        except Exception:
            # A failed publication intentionally leaves the owned stage for
            # operator inspection/recovery; live state remains untouched.
            raise

    def _load_activatable(
        self,
        bundle_name: str,
        *,
        now: str | None,
        work: Path,
    ) -> dict[str, Any]:
        bundle = self.backup._bundle_path(bundle_name)
        operation_root = work / "verify"
        _create_owned_staging(operation_root, operation_root.name)
        loaded = _decrypt_and_validate(bundle, self.recipient_private_key, operation_root)
        state = self.backup._activation_state(loaded["manifest"], bundle_name, now=now)
        if not state["activatable"]:
            raise RawEvidenceRestoreError(state.get("error", "restore_not_activatable"))
        loaded["operation_root"] = operation_root
        loaded["operation_id"] = operation_root.name
        return loaded


def _prepare_staged_root(
    extracted_root: Path,
    stage_root: Path,
    manifest: dict[str, Any],
    repository_id: str,
    *,
    now: str,
) -> None:
    stage_root.mkdir(parents=True, exist_ok=False)
    registry_source = extracted_root / "registry.sqlite3"
    registry_target = stage_root / "registry.sqlite3"
    shutil.copy2(registry_source, registry_target)
    (stage_root / "blobs" / "sha256").mkdir(parents=True, exist_ok=False)
    (stage_root / ".tmp").mkdir()
    (stage_root / ".quarantine").mkdir()
    for entry in manifest["cas_entries"]:
        relative = entry["backup_relative_path"]
        if not relative:
            continue
        source = extracted_root / Path(*PurePosixPath(relative).parts)
        target = stage_root / "blobs" / "sha256" / entry["content_hash"][:2] / entry["content_hash"]
        _copy_verified(source, target, entry["content_hash"], entry["content_size_bytes"])
    with sqlite3.connect(str(registry_target)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT repository_id FROM o5e_coordination WHERE singleton = 1"
        ).fetchone()
        if row is None or row[0] != repository_id:
            conn.rollback()
            raise RawEvidenceRestoreError("backup_repository_mismatch")
        conn.execute(
            """UPDATE o5e_coordination SET
                active_backup_operation_id = NULL, backup_owner_class = NULL,
                backup_started_at = NULL, backup_heartbeat_at = NULL,
                backup_expires_at = NULL, backup_state = 'released', updated_at = ?
                WHERE singleton = 1""",
            (now,),
        )
        _expire_staged_rows(conn, now)
        conn.commit()
    _verify_registry_snapshot(
        registry_target, cas_root=stage_root / "blobs" / "sha256"
    )
    _verify_staged_cas(stage_root, manifest)


def _expire_staged_rows(conn: sqlite3.Connection, now: str) -> None:
    rows = conn.execute(
        """SELECT revision_id, evidence_id, lifecycle_state
           FROM evidence_lifecycle
           WHERE retention_deadline <= ? AND lifecycle_state = 'available'
           ORDER BY revision_id""",
        (now,),
    ).fetchall()
    for revision_id, evidence_id, state in rows:
        operation_id = os.urandom(16).hex()
        conn.execute(
            """UPDATE evidence_lifecycle SET lifecycle_state = 'expired',
                lifecycle_reason = 'retention_expired', expired_at = ?, updated_at = ?
                WHERE revision_id = ? AND lifecycle_state = 'available'""",
            (now, now, revision_id),
        )
        conn.execute(
            """INSERT INTO lifecycle_audit (
                lifecycle_operation_id, evidence_id, revision_id, from_state,
                to_state, reason, occurred_at, actor_class, payload_deleted,
                reconciliation_result
            ) VALUES (?, ?, ?, ?, 'expired', 'retention_expired', ?,
                      'restore_reconciliation', 0, NULL)""",
            (operation_id, evidence_id, revision_id, state, now),
        )
        conn.execute(
            """UPDATE evidence_objects SET lifecycle_state = 'tombstoned', updated_at = ?
               WHERE evidence_id = ? AND lifecycle_state = 'available'""",
            (now, evidence_id),
        )
        conn.execute(
            """UPDATE memory_lineage SET status = 'source_expired', updated_at = ?
               WHERE evidence_id = ? AND status NOT IN ('source_redacted', 'memory_deleted')""",
            (now, evidence_id),
        )


def _verify_staged_root(root: Path, repository_id: str) -> None:
    registry = root / "registry.sqlite3"
    _verify_registry_snapshot(
        registry, cas_root=root / "blobs" / "sha256"
    )
    with sqlite3.connect(str(registry)) as conn:
        row = conn.execute(
            "SELECT repository_id, backup_state, active_backup_operation_id "
            "FROM o5e_coordination WHERE singleton = 1"
        ).fetchone()
        if row is None or row[0] != repository_id or row[1] != "released" or row[2] is not None:
            raise RawEvidenceRestoreError("restore_claim_state_invalid")
        rows = conn.execute(
            """SELECT r.content_hash, r.content_size_bytes, c.state
               FROM evidence_revisions r
               JOIN cas_objects c ON c.hash_algorithm = r.hash_algorithm
                                  AND c.content_hash = r.content_hash
               WHERE c.state IN ('live', 'gc_pending')"""
        ).fetchall()
    for content_hash, content_size, state in rows:
        del state
        path = root / "blobs" / "sha256" / content_hash[:2] / content_hash
        _verify_file(path, content_hash, int(content_size))


def _verify_staged_cas(root: Path, manifest: dict[str, Any]) -> None:
    for entry in manifest["cas_entries"]:
        relative = entry["backup_relative_path"]
        if not relative:
            continue
        path = root / "blobs" / "sha256" / entry["content_hash"][:2] / entry["content_hash"]
        if not path.is_file():
            raise RawEvidenceRestoreError("restore_cas_missing")
        _verify_file(path, entry["content_hash"], int(entry["content_size_bytes"]))


def _verify_file(path: Path, expected_hash: str, expected_size: int) -> None:
    import hashlib

    if not path.is_file():
        raise RawEvidenceRestoreError("restore_cas_missing")
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                digest.update(block)
    except OSError as exc:
        raise RawEvidenceRestoreError("restore_cas_unreadable") from exc
    if size != expected_size or digest.hexdigest() != expected_hash:
        raise RawEvidenceRestoreError("restore_cas_integrity_failed")


def _validate_new_target(
    value: str | Path,
    store: RawEvidenceStore,
    authority: RawEvidenceBackupAuthority,
) -> Path:
    raw_target = Path(value)
    if not raw_target.is_absolute():
        raise RawEvidenceRestoreError("restore_target_invalid")
    if _contains_reparse(raw_target):
        raise RawEvidenceRestoreError("restore_target_invalid")
    if any(not _valid_windows_component(part) for part in raw_target.parts if part not in {raw_target.anchor, ""}):
        raise RawEvidenceRestoreError("restore_target_invalid")
    if raw_target.exists() or raw_target.is_symlink():
        raise RawEvidenceRestoreError("restore_destination_exists")
    target = raw_target.resolve(strict=False)
    parent = target.parent
    if not parent.exists() or _contains_reparse(parent):
        raise RawEvidenceRestoreError("restore_target_invalid")
    forbidden = [store.root, authority.root, Path(__file__).resolve().parent]
    for root in forbidden:
        if root is None:
            continue
        root = Path(root).resolve(strict=False)
        if _strict_within(root, target) or _strict_within(target, root) or target == root:
            raise RawEvidenceRestoreError("restore_target_overlap")
    return target


def _contains_reparse(path: Path) -> bool:
    current = Path(path.anchor)
    try:
        relative = path.relative_to(current)
    except ValueError:
        return True
    for part in relative.parts:
        current /= part
        if not current.exists():
            continue
        try:
            stat_value = current.lstat()
        except OSError:
            return True
        if current.is_symlink() or getattr(stat_value, "st_file_attributes", 0) & 0x400:
            return True
    return False


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


__all__ = ["RawEvidenceRestoreError", "RawEvidenceRestoreService"]
