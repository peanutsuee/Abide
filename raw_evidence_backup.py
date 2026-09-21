"""O5E encrypted Raw Evidence backup, verification, pruning, and CLI."""

from __future__ import annotations

import base64
import argparse
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import struct
import tarfile
import tempfile
from typing import Any, BinaryIO

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from maintenance_write_gate import guarded_mutation
from raw_evidence_backup_authority import (
    BACKUP_RETENTION_ENV,
    BACKUP_ROOT_ENV,
    BackupAuthorityError,
    BackupRetention,
    RawEvidenceBackupAuthority,
)
from raw_evidence_store import (
    HASH_ALGORITHM,
    REVISION_SCHEMA_VERSION,
    RawEvidenceError,
    LINEAGE_CITATION_SCHEMA_VERSION,
    RawEvidenceStore,
    SCHEMA_VERSION,
    SOURCE_SPAN_CANDIDATE_SCHEMA_VERSION,
    SPAN_DESCRIPTOR_SCHEMA_VERSION,
    SPAN_HASH_ALGORITHM,
    SPAN_LOCATOR_KIND,
    SPAN_LOCATOR_SCHEMA_VERSION,
    _now_iso,
)


BACKUP_FORMAT_VERSION = 2
CONTAINER_VERSION = 1
ENCRYPTION_PROFILE = "X25519-HKDF-SHA256+A256GCM"
MAGIC = b"OBRAW1\n"
GCM_TAG_SIZE = 16
HEADER_SIZE_BYTES = 4
MAX_HEADER_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
BUNDLE_SUFFIX = ".obrawbackup"
CLAIM_TTL_SECONDS = 300


class RawEvidenceBackupError(RawEvidenceError):
    """Stable, content-free O5E failure."""


@dataclass(frozen=True)
class BackupConfig:
    retention: BackupRetention = BackupRetention()
    claim_ttl_seconds: int = CLAIM_TTL_SECONDS

    @classmethod
    def from_env(cls) -> "BackupConfig":
        return cls(retention=BackupRetention.from_env())


class RawEvidenceBackupService:
    """Operate on one explicitly opened Raw Evidence store and repository."""

    def __init__(
        self,
        store: RawEvidenceStore,
        authority: RawEvidenceBackupAuthority,
        recipient_public_key: X25519PublicKey,
        *,
        config: BackupConfig | None = None,
    ) -> None:
        if not isinstance(store, RawEvidenceStore) or store.is_disabled:
            raise RawEvidenceBackupError("store_disabled")
        if not isinstance(authority, RawEvidenceBackupAuthority):
            raise RawEvidenceBackupError("backup_authority_invalid")
        if not isinstance(recipient_public_key, X25519PublicKey):
            raise RawEvidenceBackupError("backup_key_invalid")
        self.store = store
        self.authority = authority
        self.recipient_public_key = recipient_public_key
        self.config = config or BackupConfig.from_env()
        self.config.retention.validate()
        bound = store.backup_repository_id()
        if bound is not None and bound != authority.repository_id():
            raise RawEvidenceBackupError("backup_repository_mismatch")
        self.repository_id = authority.repository_id()

    @classmethod
    def from_env(
        cls,
        store: RawEvidenceStore,
        recipient_public_key: X25519PublicKey,
        *,
        forbidden_roots: tuple[str | Path, ...] = (),
        config: BackupConfig | None = None,
    ) -> "RawEvidenceBackupService":
        authority = RawEvidenceBackupAuthority.from_env(
            live_root=store.root,
            forbidden_roots=forbidden_roots,
        )
        return cls(store, authority, recipient_public_key, config=config)

    @guarded_mutation("raw_evidence_backup_create")
    def create(self, *, clock=None) -> dict[str, Any]:
        """Create one complete encrypted bundle or publish nothing."""

        self._reject_pending_revocation()
        self.repository_id = self.authority.bind_store(self.store)
        now = _timestamp(clock)
        backup_id = os.urandom(16).hex()
        operation_id = os.urandom(16).hex()
        expires_at = (
            _parse_time(now) + timedelta(days=self.config.retention.days)
        ).isoformat(timespec="seconds")
        self.authority.mark_revoked(
            current_epoch=self.authority.current_restore_epoch()
        )
        claim = False
        operation_root = self.authority.staging_root / operation_id
        final_path = self.authority.bundles_root / f"{backup_id}{BUNDLE_SUFFIX}"
        published = False
        try:
            self.store.acquire_backup_claim(
                operation_id,
                ttl_seconds=self.config.claim_ttl_seconds,
                now=now,
            )
            claim = True
            _create_owned_staging(operation_root, operation_id)
            self._assert_claim(operation_id, now=now)
            restore_epoch = self.authority.current_restore_epoch()
            registry_path = operation_root / "registry.sqlite3"
            _snapshot_registry(self.store, registry_path)
            self._assert_claim(operation_id, now=now)
            registry_info = _verify_registry_snapshot(registry_path)
            self._heartbeat_claim(operation_id, now)
            cas_entries = self._copy_cas_snapshot(
                registry_path, operation_root / "cas", operation_id, claim_now=now
            )
            self._assert_claim(operation_id, now=now)
            registry_info = _verify_registry_snapshot(
                registry_path, cas_root=operation_root / "cas"
            )
            manifest = _build_manifest(
                backup_id=backup_id,
                operation_id=operation_id,
                repository_id=self.repository_id,
                restore_epoch=restore_epoch,
                created_at=now,
                expires_at=expires_at,
                registry_info=registry_info,
                cas_entries=cas_entries,
                recipient_fingerprint=_fingerprint(self.recipient_public_key),
            )
            archive_path = operation_root / "payload.tar"
            _build_archive(archive_path, registry_path, operation_root / "cas", manifest)
            encrypted_staging = operation_root / "bundle.tmp"
            _encrypt_archive(
                archive_path,
                encrypted_staging,
                self.recipient_public_key,
                backup_id=backup_id,
                repository_id=self.repository_id,
                created_at=now,
                expires_at=expires_at,
            )
            self._heartbeat_claim(operation_id, now)
            self._assert_claim(operation_id, now=now)
            _publish_no_replace(encrypted_staging, final_path, self.authority.bundles_root)
            published = True
            try:
                self._assert_claim(operation_id, now=now)
            except RawEvidenceBackupError:
                _remove_file(final_path)
                published = False
                raise
            encrypted_size = final_path.stat().st_size
            self.authority.register_bundle(
                backup_id=backup_id,
                bundle_name=final_path.name,
                created_at=now,
                expires_at=expires_at,
                restore_epoch=restore_epoch,
                format_version=BACKUP_FORMAT_VERSION,
                encrypted_size_bytes=encrypted_size,
            )
            return {
                "status": "success",
                "backup_id": backup_id,
                "operation_id": operation_id,
                "created_at": now,
                "expires_at": expires_at,
                "restore_epoch": restore_epoch,
                "format_version": BACKUP_FORMAT_VERSION,
                "encrypted_size_bytes": encrypted_size,
                "cas_entry_count": len(cas_entries),
            }
        except (RawEvidenceError, BackupAuthorityError) as exc:
            if published:
                with suppress(Exception):
                    if self.authority.catalog_by_name(final_path.name) is None:
                        _remove_file(final_path)
            raise RawEvidenceBackupError(exc.code) from exc
        except (OSError, sqlite3.Error, tarfile.TarError, ValueError, TypeError) as exc:
            if published:
                with suppress(Exception):
                    if self.authority.catalog_by_name(final_path.name) is None:
                        _remove_file(final_path)
            raise RawEvidenceBackupError("backup_failed") from exc
        finally:
            if claim:
                with suppress(Exception):
                    self.store.release_backup_claim(operation_id)
            _cleanup_owned_staging(operation_root, operation_id)

    def verify(
        self,
        bundle_name: str,
        recipient_private_key: X25519PrivateKey,
        *,
        now: str | None = None,
    ) -> dict[str, Any]:
        if self.store.backup_repository_id() is None:
            raise RawEvidenceBackupError("backup_repository_unbound")
        bundle = self._bundle_path(bundle_name)
        if not isinstance(recipient_private_key, X25519PrivateKey):
            raise RawEvidenceBackupError("backup_key_invalid")
        operation_root = self.authority.temp_root / f"verify-{os.urandom(8).hex()}"
        _create_owned_staging(operation_root, operation_root.name)
        try:
            result = _decrypt_and_validate(bundle, recipient_private_key, operation_root)
            manifest = result["manifest"]
            authority_state = self._activation_state(manifest, bundle_name, now=now)
            return {
                "status": "success",
                "backup_id": manifest["backup_id"],
                "format_version": manifest["backup_format_version"],
                "registry_schema": manifest["source_registry_schema"],
                "restore_epoch": manifest["restore_epoch"],
                "created_at": manifest["created_at"],
                "expires_at": manifest["expires_at"],
                "cas_entry_count": len(manifest["cas_entries"]),
                "activatable": authority_state["activatable"],
                "activation_error": authority_state.get("error"),
            }
        except (RawEvidenceError, BackupAuthorityError) as exc:
            raise RawEvidenceBackupError(exc.code) from exc
        except (OSError, sqlite3.Error, tarfile.TarError, ValueError, TypeError) as exc:
            raise RawEvidenceBackupError("backup_invalid") from exc
        finally:
            _cleanup_owned_staging(operation_root, operation_root.name)

    @guarded_mutation("raw_evidence_backup_prune")
    def prune(self, *, now: str | None = None) -> dict[str, Any]:
        current = now or _now_iso()
        self.authority.expire_catalog(now=current)
        current_epoch = self.authority.current_restore_epoch()
        self.authority.mark_revoked(current_epoch=current_epoch)
        pruned = 0
        missing = 0
        for entry in self.authority.catalog_entries():
            if entry["status"] not in {"expired", "revoked"}:
                continue
            path = self._bundle_path(entry["bundle_name"])
            if path.exists():
                try:
                    path.unlink()
                    pruned += 1
                except OSError as exc:
                    raise RawEvidenceBackupError("backup_prune_failed") from exc
            else:
                missing += 1
            self.authority.mark_pruned(entry["backup_id"], reason="exact_operator_prune")
        return {"status": "success", "pruned": pruned, "missing": missing}

    def integrity_audit(self, *, now: str | None = None) -> dict[str, Any]:
        del now
        report = self.authority.integrity_check()
        claim = self.store.backup_claim_status()
        report.update(
            {
                "status": "success" if report["quick_check"] == "ok" and not report["foreign_key_errors"] else "invalid",
                "catalog_count": len(self.authority.catalog_entries()),
                "active_claim": bool(claim and claim["backup_state"] == "active"),
            }
        )
        return report

    def _reject_pending_revocation(self) -> None:
        if self.authority.has_pending_revocation():
            raise RawEvidenceBackupError("revocation_pending")

    def _assert_claim(self, operation_id: str, *, now: str | None = None) -> None:
        try:
            self.store.assert_backup_claim(operation_id, now=now)
        except RawEvidenceError as exc:
            raise RawEvidenceBackupError(exc.code) from exc
        if self.authority.has_pending_revocation():
            raise RawEvidenceBackupError("revocation_pending")

    def _heartbeat_claim(self, operation_id: str, now: str) -> None:
        try:
            self.store.heartbeat_backup_claim(
                operation_id,
                ttl_seconds=self.config.claim_ttl_seconds,
                now=now,
            )
        except RawEvidenceError as exc:
            raise RawEvidenceBackupError(exc.code) from exc

    def _bundle_path(self, bundle_name: str) -> Path:
        if (
            not isinstance(bundle_name, str)
            or not bundle_name.endswith(BUNDLE_SUFFIX)
            or len(bundle_name) != 32 + len(BUNDLE_SUFFIX)
            or any(c not in "0123456789abcdef" for c in bundle_name[:-len(BUNDLE_SUFFIX)])
        ):
            raise RawEvidenceBackupError("backup_bundle_invalid")
        raw_path = self.authority.bundles_root / bundle_name
        if raw_path.is_symlink() or _contains_reparse(raw_path):
            raise RawEvidenceBackupError("backup_bundle_invalid")
        path = raw_path.resolve(strict=False)
        if not _strict_within(self.authority.bundles_root, path):
            raise RawEvidenceBackupError("backup_bundle_invalid")
        return path

    def _activation_state(
        self,
        manifest: dict[str, Any],
        bundle_name: str,
        *,
        now: str | None,
    ) -> dict[str, Any]:
        try:
            catalog = self.authority.catalog_by_name(bundle_name)
            current = self.authority.current_restore_epoch()
            if self.authority.has_pending_revocation():
                return {"activatable": False, "error": "revocation_pending"}
            if catalog is None or catalog["status"] != "active":
                return {"activatable": False, "error": "backup_not_active"}
            if (
                catalog["backup_id"] != manifest["backup_id"]
                or catalog["format_version"] != manifest["backup_format_version"]
                or catalog["created_at"] != manifest["created_at"]
                or catalog["expires_at"] != manifest["expires_at"]
                or int(catalog["restore_epoch"]) != manifest["restore_epoch"]
            ):
                return {"activatable": False, "error": "backup_catalog_mismatch"}
            if catalog["repository_id"] != self.repository_id:
                return {"activatable": False, "error": "backup_repository_mismatch"}
            if manifest["repository_id"] != self.repository_id:
                return {"activatable": False, "error": "backup_repository_mismatch"}
            if manifest["restore_epoch"] != current:
                return {"activatable": False, "error": "restore_epoch_revoked"}
            if _parse_time(now or _now_iso()) >= _parse_time(manifest["expires_at"]):
                return {"activatable": False, "error": "backup_expired"}
            return {"activatable": True}
        except BackupAuthorityError as exc:
            return {"activatable": False, "error": exc.code}

    def _copy_cas_snapshot(
        self,
        registry_path: Path,
        destination_root: Path,
        operation_id: str,
        *,
        claim_now: str | None = None,
    ) -> list[dict[str, Any]]:
        destination_root.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, Any]] = []
        with sqlite3.connect(str(registry_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM cas_objects ORDER BY content_hash").fetchall()
            refs = {
                row["content_hash"]
                for row in conn.execute(
                    "SELECT DISTINCT content_hash FROM evidence_revisions"
                ).fetchall()
            }
        for row in rows:
            content_hash = row["content_hash"]
            state = row["state"]
            required = content_hash in refs and state in {"live", "gc_pending"}
            source = self.store._path_from_stored_reference(
                row["blob_relpath"], content_hash
            )
            present = source.exists()
            backup_relative = f"cas/{content_hash[:2]}/{content_hash}"
            item = {
                "hash_algorithm": row["hash_algorithm"],
                "content_hash": content_hash,
                "content_size_bytes": int(row["content_size_bytes"]),
                "cas_state": state,
                "backup_relative_path": backup_relative if present else None,
                "payload_present": bool(present),
            }
            if required and not present:
                raise RawEvidenceBackupError("cas_missing")
            if present and state in {"live", "gc_pending", "publish_pending"}:
                target = destination_root / Path(*PurePosixPath(backup_relative).parts[1:])
                _copy_verified(source, target, content_hash, int(row["content_size_bytes"]))
                item["sha256"] = content_hash
            entries.append(item)
            self._assert_claim(operation_id, now=claim_now)
        return entries


def _snapshot_registry(store: RawEvidenceStore, destination: Path) -> None:
    source = store.registry_path
    if source is None:
        raise RawEvidenceBackupError("store_disabled")
    source_connection = None
    try:
        source_connection = sqlite3.connect(str(source), timeout=30)
        source_connection.execute("PRAGMA query_only = ON")
        with sqlite3.connect(str(destination)) as destination_connection:
            source_connection.backup(destination_connection, pages=128)
    except (OSError, sqlite3.Error) as exc:
        raise RawEvidenceBackupError("sqlite_snapshot_failed") from exc
    finally:
        if source_connection is not None:
            source_connection.close()


def _verify_registry_snapshot(
    path: Path, *, cas_root: Path | None = None
) -> dict[str, Any]:
    try:
        with sqlite3.connect(str(path)) as conn:
            conn.row_factory = sqlite3.Row
            quick = conn.execute("PRAGMA quick_check").fetchone()[0]
            if quick != "ok":
                raise RawEvidenceBackupError("registry_integrity_failed")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise RawEvidenceBackupError("registry_foreign_key_failed")
            schema = conn.execute(
                "SELECT schema_version FROM store_schema WHERE singleton = 1"
            ).fetchone()
            if schema is None or int(schema[0]) != SCHEMA_VERSION:
                raise RawEvidenceBackupError("schema_unsupported")
            tables = [
                "store_schema", "evidence_objects", "evidence_revisions",
                "import_runs", "import_run_items", "memory_lineage",
                "source_span_descriptors", "memory_lineage_citations",
                "source_span_candidate_tokens", "cas_objects",
                "evidence_lifecycle", "lifecycle_audit", "o5e_coordination",
            ]
            counts = {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in tables
            }
            _validate_span_citation_semantics(conn, cas_root=cas_root)
            raw = path.read_bytes()
    except RawEvidenceBackupError:
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise RawEvidenceBackupError("registry_integrity_failed") from exc
    return {
        "path": "registry.sqlite3",
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "schema_version": SCHEMA_VERSION,
        "table_counts": counts,
    }


def _validate_span_citation_semantics(
    conn: sqlite3.Connection, *, cas_root: Path | None
) -> None:
    descriptors = conn.execute(
        """
        SELECT s.*, r.content_hash, r.content_size_bytes,
               r.verification_state, r.hash_algorithm AS revision_hash_algorithm,
               r.revision_schema_version, l.lifecycle_state
        FROM source_span_descriptors AS s
        JOIN evidence_revisions AS r ON r.revision_id = s.revision_id
        JOIN evidence_lifecycle AS l ON l.revision_id = r.revision_id
        ORDER BY s.span_id
        """
    ).fetchall()
    for descriptor in descriptors:
        if (
            descriptor["revision_hash_algorithm"] != HASH_ALGORITHM
            or descriptor["revision_schema_version"] != REVISION_SCHEMA_VERSION
        ):
            raise RawEvidenceBackupError("span_revision_metadata_invalid")
        if (
            descriptor["descriptor_schema_version"] != SPAN_DESCRIPTOR_SCHEMA_VERSION
            or descriptor["locator_kind"] != SPAN_LOCATOR_KIND
            or descriptor["locator_schema_version"] != SPAN_LOCATOR_SCHEMA_VERSION
            or descriptor["span_hash_algorithm"] != SPAN_HASH_ALGORITHM
            or not _valid_hash(descriptor["span_hash"])
            or isinstance(descriptor["raw_byte_start"], bool)
            or not isinstance(descriptor["raw_byte_start"], int)
            or isinstance(descriptor["raw_byte_end"], bool)
            or not isinstance(descriptor["raw_byte_end"], int)
            or isinstance(descriptor["content_size_bytes"], bool)
            or not isinstance(descriptor["content_size_bytes"], int)
            or not 0 <= descriptor["raw_byte_start"] < descriptor["raw_byte_end"]
            or descriptor["raw_byte_end"] > descriptor["content_size_bytes"]
        ):
            raise RawEvidenceBackupError("span_semantic_invalid")
        if cas_root is None:
            continue
        payload = cas_root / descriptor["content_hash"][:2] / descriptor["content_hash"]
        if not payload.is_file():
            if (
                descriptor["lifecycle_state"] == "available"
                and descriptor["verification_state"] == "verified"
            ):
                raise RawEvidenceBackupError("span_payload_missing")
            continue
        try:
            raw = payload.read_bytes()
        except OSError as exc:
            raise RawEvidenceBackupError("span_payload_unreadable") from exc
        if (
            len(raw) != descriptor["content_size_bytes"]
            or hashlib.sha256(raw).hexdigest() != descriptor["content_hash"]
            or hashlib.sha256(
                raw[descriptor["raw_byte_start"] : descriptor["raw_byte_end"]]
            ).hexdigest()
            != descriptor["span_hash"]
        ):
            raise RawEvidenceBackupError("span_semantic_invalid")

    lineages = conn.execute(
        """
        SELECT l.lineage_id, l.evidence_id, l.revision_id,
               r.evidence_id AS revision_evidence_id
        FROM memory_lineage AS l
        LEFT JOIN evidence_revisions AS r ON r.revision_id = l.revision_id
        ORDER BY l.lineage_id
        """
    ).fetchall()
    for lineage in lineages:
        if (
            lineage["revision_evidence_id"] is None
            or lineage["evidence_id"] != lineage["revision_evidence_id"]
        ):
            raise RawEvidenceBackupError("lineage_semantic_invalid")

    citations = conn.execute(
        """
        SELECT c.lineage_id, c.span_id, c.citation_schema_version,
               l.revision_id AS lineage_revision_id,
               l.evidence_id AS lineage_evidence_id,
               s.revision_id AS span_revision_id,
               r.evidence_id AS span_evidence_id
        FROM memory_lineage_citations AS c
        JOIN memory_lineage AS l ON l.lineage_id = c.lineage_id
        JOIN source_span_descriptors AS s ON s.span_id = c.span_id
        JOIN evidence_revisions AS r ON r.revision_id = s.revision_id
        ORDER BY c.lineage_id, c.span_id
        """
    ).fetchall()
    for citation in citations:
        if (
            citation["citation_schema_version"] != LINEAGE_CITATION_SCHEMA_VERSION
            or citation["lineage_revision_id"] != citation["span_revision_id"]
            or citation["lineage_evidence_id"] != citation["span_evidence_id"]
        ):
            raise RawEvidenceBackupError("citation_semantic_invalid")

    candidates = conn.execute(
        """
        SELECT c.revision_id, c.run_id, c.run_item_key,
               c.raw_byte_start, c.raw_byte_end,
               c.span_id, c.candidate_schema_version,
               s.revision_id AS span_revision_id,
               s.raw_byte_start AS span_start,
               s.raw_byte_end AS span_end,
               i.revision_id AS item_revision_id,
               i.evidence_id AS item_evidence_id,
               r.evidence_id AS revision_evidence_id
        FROM source_span_candidate_tokens AS c
        JOIN source_span_descriptors AS s ON s.span_id = c.span_id
        JOIN import_run_items AS i
          ON i.run_id = c.run_id AND i.item_key = c.run_item_key
        JOIN evidence_revisions AS r ON r.revision_id = c.revision_id
        ORDER BY c.candidate_token
        """
    ).fetchall()
    for candidate in candidates:
        if (
            candidate["candidate_schema_version"] != SOURCE_SPAN_CANDIDATE_SCHEMA_VERSION
            or candidate["revision_id"] != candidate["span_revision_id"]
            or candidate["revision_id"] != candidate["item_revision_id"]
            or candidate["item_evidence_id"] != candidate["revision_evidence_id"]
            or candidate["raw_byte_start"] != candidate["span_start"]
            or candidate["raw_byte_end"] != candidate["span_end"]
        ):
            raise RawEvidenceBackupError("candidate_semantic_invalid")


def _build_manifest(
    *,
    backup_id: str,
    operation_id: str,
    repository_id: str,
    restore_epoch: int,
    created_at: str,
    expires_at: str,
    registry_info: dict[str, Any],
    cas_entries: list[dict[str, Any]],
    recipient_fingerprint: str,
) -> dict[str, Any]:
    manifest = {
        "backup_format_version": BACKUP_FORMAT_VERSION,
        "source_registry_schema": SCHEMA_VERSION,
        "repository_id": repository_id,
        "backup_operation_id": operation_id,
        "backup_id": backup_id,
        "restore_epoch": restore_epoch,
        "created_at": created_at,
        "expires_at": expires_at,
        "policy_version": "foundation_v1",
        "registry_snapshot": registry_info,
        "table_counts": registry_info["table_counts"],
        "cas_entries": cas_entries,
        "logical_counts": {
            "evidence_objects": registry_info["table_counts"]["evidence_objects"],
            "revisions": registry_info["table_counts"]["evidence_revisions"],
            "imports": registry_info["table_counts"]["import_runs"],
            "lineage": registry_info["table_counts"]["memory_lineage"],
            "span_descriptors": registry_info["table_counts"]["source_span_descriptors"],
            "citations": registry_info["table_counts"]["memory_lineage_citations"],
        },
        "span_descriptors": {
            "count": registry_info["table_counts"]["source_span_descriptors"],
            "schema_version": SPAN_DESCRIPTOR_SCHEMA_VERSION,
        },
        "citations": {
            "count": registry_info["table_counts"]["memory_lineage_citations"],
            "schema_version": LINEAGE_CITATION_SCHEMA_VERSION,
        },
        "encryption_profile": ENCRYPTION_PROFILE,
        "recipient_key_fingerprint": recipient_fingerprint,
        "completeness_state": "complete",
    }
    manifest["manifest_sha256"] = hashlib.sha256(_canonical_json(manifest)).hexdigest()
    return manifest


def _build_archive(archive_path: Path, registry_path: Path, cas_root: Path, manifest: dict[str, Any]) -> None:
    try:
        with tarfile.open(archive_path, mode="w", format=tarfile.PAX_FORMAT) as archive:
            _add_bytes(archive, "manifest.json", _canonical_json(manifest))
            _add_file(archive, registry_path, "data/registry.sqlite3")
            for entry in manifest["cas_entries"]:
                relative = entry["backup_relative_path"]
                if not relative:
                    continue
                source = cas_root / Path(*PurePosixPath(relative).parts[1:])
                _add_file(archive, source, f"data/{relative}")
        _fsync_file(archive_path)
    except (OSError, tarfile.TarError) as exc:
        raise RawEvidenceBackupError("backup_archive_failed") from exc


def _encrypt_archive(
    archive_path: Path,
    destination: Path,
    recipient_public_key: X25519PublicKey,
    *,
    backup_id: str,
    repository_id: str,
    created_at: str,
    expires_at: str,
) -> None:
    ephemeral_private = X25519PrivateKey.generate()
    ephemeral_public = ephemeral_private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    salt = os.urandom(32)
    wrap_nonce = os.urandom(12)
    payload_nonce = os.urandom(12)
    content_key = os.urandom(32)
    header_base = {
        "container_version": CONTAINER_VERSION,
        "backup_format_version": BACKUP_FORMAT_VERSION,
        "backup_id": backup_id,
        "repository_id": repository_id,
        "created_at": created_at,
        "expires_at": expires_at,
        "encryption_profile": ENCRYPTION_PROFILE,
        "recipient_key_fingerprint": _fingerprint(recipient_public_key),
        "ephemeral_public_key": _b64(ephemeral_public),
        "hkdf_salt": _b64(salt),
        "wrap_nonce": _b64(wrap_nonce),
        "payload_nonce": _b64(payload_nonce),
    }
    kek = _derive_kek(ephemeral_private.exchange(recipient_public_key), salt)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    wrapped_key = AESGCM(kek).encrypt(
        wrap_nonce, content_key, _canonical_json(header_base)
    )
    header = {**header_base, "wrapped_content_key": _b64(wrapped_key)}
    header_bytes = _canonical_json(header)
    if len(header_bytes) > MAX_HEADER_BYTES:
        raise RawEvidenceBackupError("backup_header_invalid")
    encryptor = Cipher(algorithms.AES(content_key), modes.GCM(payload_nonce)).encryptor()
    encryptor.authenticate_additional_data(header_bytes)
    try:
        with archive_path.open("rb") as reader, destination.open("xb") as writer:
            writer.write(MAGIC)
            writer.write(struct.pack(">I", len(header_bytes)))
            writer.write(header_bytes)
            while True:
                block = reader.read(CHUNK_SIZE)
                if not block:
                    break
                writer.write(encryptor.update(block))
            writer.write(encryptor.finalize())
            writer.write(encryptor.tag)
            writer.flush()
            os.fsync(writer.fileno())
    except (OSError, ValueError) as exc:
        _remove_file(destination)
        raise RawEvidenceBackupError("backup_encryption_failed") from exc


def _decrypt_and_validate(bundle: Path, private_key: X25519PrivateKey, operation_root: Path) -> dict[str, Any]:
    header, header_bytes, offset, ciphertext_size = _read_header(bundle)
    if _fingerprint(private_key.public_key()) != header["recipient_key_fingerprint"]:
        raise RawEvidenceBackupError("backup_key_invalid")
    try:
        ephemeral_public = X25519PublicKey.from_public_bytes(_unb64(header["ephemeral_public_key"], 32))
        salt = _unb64(header["hkdf_salt"], 32)
        wrap_nonce = _unb64(header["wrap_nonce"], 12)
        payload_nonce = _unb64(header["payload_nonce"], 12)
        wrapped = _unb64(header["wrapped_content_key"], 48)
        base = dict(header)
        del base["wrapped_content_key"]
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        kek = _derive_kek(private_key.exchange(ephemeral_public), salt)
        content_key = AESGCM(kek).decrypt(wrap_nonce, wrapped, _canonical_json(base))
    except Exception as exc:
        if isinstance(exc, RawEvidenceBackupError):
            raise
        raise RawEvidenceBackupError("backup_authentication_failed") from exc
    archive_path = operation_root / "payload.tar"
    try:
        with bundle.open("rb") as reader:
            reader.seek(offset + ciphertext_size)
            tag = reader.read(GCM_TAG_SIZE)
            if len(tag) != GCM_TAG_SIZE or reader.read(1):
                raise RawEvidenceBackupError("backup_container_invalid")
            reader.seek(offset)
            decryptor = Cipher(algorithms.AES(content_key), modes.GCM(payload_nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(header_bytes)
            remaining = ciphertext_size
            with archive_path.open("xb") as writer:
                while remaining:
                    block = reader.read(min(CHUNK_SIZE, remaining))
                    if not block:
                        raise RawEvidenceBackupError("backup_container_invalid")
                    remaining -= len(block)
                    writer.write(decryptor.update(block))
                writer.write(decryptor.finalize())
                writer.flush()
                os.fsync(writer.fileno())
    except RawEvidenceBackupError:
        raise
    except (OSError, ValueError) as exc:
        raise RawEvidenceBackupError("backup_authentication_failed") from exc
    manifest = _extract_and_validate_archive(archive_path, operation_root / "restored")
    if (
        manifest["backup_id"] != header["backup_id"]
        or manifest["repository_id"] != header["repository_id"]
        or manifest["created_at"] != header["created_at"]
        or manifest["expires_at"] != header["expires_at"]
        or manifest["recipient_key_fingerprint"] != header["recipient_key_fingerprint"]
    ):
        raise RawEvidenceBackupError("backup_manifest_invalid")
    return {"header": header, "manifest": manifest, "restore_root": operation_root / "restored"}


def _read_header(bundle: Path) -> tuple[dict[str, Any], bytes, int, int]:
    try:
        size = bundle.stat().st_size
        with bundle.open("rb") as handle:
            if handle.read(len(MAGIC)) != MAGIC:
                raise RawEvidenceBackupError("backup_container_invalid")
            length = handle.read(HEADER_SIZE_BYTES)
            if len(length) != HEADER_SIZE_BYTES:
                raise RawEvidenceBackupError("backup_container_invalid")
            header_length = struct.unpack(">I", length)[0]
            if not 1 <= header_length <= MAX_HEADER_BYTES:
                raise RawEvidenceBackupError("backup_container_invalid")
            header_bytes = handle.read(header_length)
        header = json.loads(header_bytes.decode("utf-8"))
    except RawEvidenceBackupError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise RawEvidenceBackupError("backup_container_invalid") from exc
    expected = {
        "container_version", "backup_format_version", "backup_id", "repository_id",
        "created_at", "expires_at", "encryption_profile", "recipient_key_fingerprint",
        "ephemeral_public_key", "hkdf_salt", "wrap_nonce", "payload_nonce",
        "wrapped_content_key",
    }
    if set(header) != expected or header_bytes != _canonical_json(header):
        raise RawEvidenceBackupError("backup_container_invalid")
    if header["container_version"] != CONTAINER_VERSION or header["backup_format_version"] != BACKUP_FORMAT_VERSION:
        raise RawEvidenceBackupError("backup_format_unsupported")
    if not _valid_id(header["backup_id"]) or not _valid_id(header["repository_id"]):
        raise RawEvidenceBackupError("backup_container_invalid")
    offset = len(MAGIC) + HEADER_SIZE_BYTES + len(header_bytes)
    ciphertext_size = size - offset - GCM_TAG_SIZE
    if ciphertext_size <= 0 or ciphertext_size > MAX_ARCHIVE_BYTES:
        raise RawEvidenceBackupError("backup_container_invalid")
    for key, length in (("ephemeral_public_key", 32), ("hkdf_salt", 32), ("wrap_nonce", 12), ("payload_nonce", 12), ("wrapped_content_key", 48)):
        _unb64(header[key], length)
    return header, header_bytes, offset, ciphertext_size


def _extract_and_validate_archive(archive_path: Path, restore_root: Path) -> dict[str, Any]:
    restore_root.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            members = archive.getmembers()
            if not members or members[0].name != "manifest.json":
                raise RawEvidenceBackupError("backup_manifest_invalid")
            names: set[str] = set()
            for member in members:
                _validate_archive_member(member)
                if member.name in names:
                    raise RawEvidenceBackupError("backup_manifest_invalid")
                names.add(member.name)
            manifest_stream = archive.extractfile(members[0])
            if manifest_stream is None:
                raise RawEvidenceBackupError("backup_manifest_invalid")
            raw_manifest = manifest_stream.read(MAX_MANIFEST_BYTES + 1)
            if len(raw_manifest) > MAX_MANIFEST_BYTES:
                raise RawEvidenceBackupError("backup_manifest_invalid")
            manifest = json.loads(raw_manifest.decode("utf-8"))
            _validate_manifest(manifest, raw_manifest)
            expected = {"manifest.json", "data/registry.sqlite3"}
            expected.update(
                f"data/{entry['backup_relative_path']}"
                for entry in manifest["cas_entries"]
                if entry["backup_relative_path"]
            )
            if set(names) != expected:
                raise RawEvidenceBackupError("backup_manifest_invalid")
            by_name = {member.name: member for member in members}
            for name in sorted(expected - {"manifest.json"}):
                member = by_name[name]
                relative_name = name[len("data/"):]
                destination = restore_root / Path(*PurePosixPath(relative_name).parts)
                if not _strict_within(restore_root, destination):
                    raise RawEvidenceBackupError("backup_path_invalid")
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise RawEvidenceBackupError("backup_manifest_invalid")
                digest = hashlib.sha256()
                size = 0
                with destination.open("xb") as writer:
                    while True:
                        block = source.read(CHUNK_SIZE)
                        if not block:
                            break
                        size += len(block)
                        if size > MAX_ARCHIVE_BYTES:
                            raise RawEvidenceBackupError("backup_size_limit")
                        digest.update(block)
                        writer.write(block)
                    writer.flush()
                    os.fsync(writer.fileno())
                if name == "data/registry.sqlite3":
                    expected_size = manifest["registry_snapshot"]["size_bytes"]
                    expected_hash = manifest["registry_snapshot"]["sha256"]
                else:
                    relative = name[len("data/"):]
                    entry = next(item for item in manifest["cas_entries"] if item["backup_relative_path"] == relative)
                    expected_size = entry["content_size_bytes"]
                    expected_hash = entry["content_hash"]
                if size != expected_size or digest.hexdigest() != expected_hash:
                    raise RawEvidenceBackupError("backup_integrity_failed")
            _verify_registry_snapshot(
                restore_root / "registry.sqlite3", cas_root=restore_root / "cas"
            )
        return manifest
    except RawEvidenceBackupError:
        raise
    except (OSError, tarfile.TarError, UnicodeDecodeError, ValueError, StopIteration) as exc:
        raise RawEvidenceBackupError("backup_manifest_invalid") from exc


def _validate_manifest(manifest: Any, raw: bytes) -> None:
    if not isinstance(manifest, dict) or manifest.get("backup_format_version") != BACKUP_FORMAT_VERSION:
        raise RawEvidenceBackupError("backup_format_unsupported")
    required = {
        "backup_format_version", "source_registry_schema", "repository_id",
        "backup_operation_id", "backup_id", "restore_epoch", "created_at",
        "expires_at", "policy_version", "registry_snapshot", "table_counts",
        "cas_entries", "logical_counts", "span_descriptors", "citations",
        "encryption_profile", "recipient_key_fingerprint", "completeness_state",
        "manifest_sha256",
    }
    if set(manifest) != required or manifest["completeness_state"] != "complete":
        raise RawEvidenceBackupError("backup_manifest_invalid")
    digest_input = dict(manifest)
    digest = digest_input.pop("manifest_sha256")
    if not isinstance(digest, str) or hashlib.sha256(_canonical_json(digest_input)).hexdigest() != digest:
        raise RawEvidenceBackupError("backup_manifest_invalid")
    if manifest["source_registry_schema"] != SCHEMA_VERSION or manifest["encryption_profile"] != ENCRYPTION_PROFILE:
        raise RawEvidenceBackupError("backup_manifest_invalid")
    for field in ("repository_id", "backup_operation_id", "backup_id"):
        if not _valid_id(manifest[field]):
            raise RawEvidenceBackupError("backup_manifest_invalid")
    if isinstance(manifest["restore_epoch"], bool) or not isinstance(manifest["restore_epoch"], int) or manifest["restore_epoch"] < 0:
        raise RawEvidenceBackupError("backup_manifest_invalid")
    snapshot = manifest["registry_snapshot"]
    if not isinstance(snapshot, dict) or set(snapshot) != {"path", "size_bytes", "sha256", "schema_version", "table_counts"}:
        raise RawEvidenceBackupError("backup_manifest_invalid")
    if snapshot["path"] != "registry.sqlite3" or snapshot["schema_version"] != SCHEMA_VERSION:
        raise RawEvidenceBackupError("backup_manifest_invalid")
    if not _valid_hash(snapshot["sha256"]) or not _bounded_int(snapshot["size_bytes"], 0, MAX_ARCHIVE_BYTES):
        raise RawEvidenceBackupError("backup_manifest_invalid")
    for field, table, schema_version in (
        ("span_descriptors", "source_span_descriptors", SPAN_DESCRIPTOR_SCHEMA_VERSION),
        ("citations", "memory_lineage_citations", LINEAGE_CITATION_SCHEMA_VERSION),
    ):
        value = manifest[field]
        if (
            not isinstance(value, dict)
            or set(value) != {"count", "schema_version"}
            or not _bounded_int(value["count"], 0, 1_000_000)
            or value["schema_version"] != schema_version
            or manifest["table_counts"].get(table) != value["count"]
        ):
            raise RawEvidenceBackupError("backup_manifest_invalid")
    logical = manifest["logical_counts"]
    if (
        not isinstance(logical, dict)
        or logical.get("span_descriptors") != manifest["span_descriptors"]["count"]
        or logical.get("citations") != manifest["citations"]["count"]
    ):
        raise RawEvidenceBackupError("backup_manifest_invalid")
    if not isinstance(manifest["cas_entries"], list) or len(manifest["cas_entries"]) > 1_000_000:
        raise RawEvidenceBackupError("backup_manifest_invalid")
    for entry in manifest["cas_entries"]:
        if not isinstance(entry, dict) or set(entry) != {
            "hash_algorithm", "content_hash", "content_size_bytes", "cas_state",
            "backup_relative_path", "payload_present", *( ["sha256"] if entry.get("payload_present") else [] )
        }:
            raise RawEvidenceBackupError("backup_manifest_invalid")
        if entry["hash_algorithm"] != HASH_ALGORITHM or not _valid_hash(entry["content_hash"]):
            raise RawEvidenceBackupError("backup_manifest_invalid")
        if not _bounded_int(entry["content_size_bytes"], 0, MAX_ARCHIVE_BYTES):
            raise RawEvidenceBackupError("backup_manifest_invalid")
        if entry["payload_present"] and entry["sha256"] != entry["content_hash"]:
            raise RawEvidenceBackupError("backup_manifest_invalid")
        path = entry["backup_relative_path"]
        if path is not None and (not isinstance(path, str) or path != f"cas/{entry['content_hash'][:2]}/{entry['content_hash']}"):
            raise RawEvidenceBackupError("backup_manifest_invalid")
    if raw != _canonical_json(manifest):
        raise RawEvidenceBackupError("backup_manifest_invalid")


def _validate_archive_member(member: tarfile.TarInfo) -> None:
    if not member.isfile() or member.name in {"", "."} or "\\" in member.name or "\x00" in member.name:
        raise RawEvidenceBackupError("backup_path_invalid")
    path = PurePosixPath(member.name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RawEvidenceBackupError("backup_path_invalid")
    if member.size < 0 or member.size > MAX_ARCHIVE_BYTES:
        raise RawEvidenceBackupError("backup_size_limit")
    if member.name != "manifest.json" and not member.name.startswith("data/"):
        raise RawEvidenceBackupError("backup_path_invalid")


def _create_owned_staging(path: Path, operation_id: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=False)
        marker = path / ".o5e-owner"
        marker.write_text(json.dumps({"operation_id": operation_id}, sort_keys=True), encoding="utf-8")
        _fsync_file(marker)
    except (OSError, ValueError) as exc:
        raise RawEvidenceBackupError("backup_staging_failed") from exc


def _cleanup_owned_staging(path: Path, operation_id: str) -> None:
    try:
        marker = path / ".o5e-owner"
        if not path.is_dir() or not marker.is_file():
            return
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if payload != {"operation_id": operation_id} or not _strict_within(path.parent, path):
            return
        if _contains_reparse(path):
            return
        shutil.rmtree(path)
    except (OSError, ValueError, TypeError):
        return


def _publish_no_replace(source: Path, destination: Path, parent: Path) -> None:
    if destination.exists() or not _strict_within(parent, destination):
        raise RawEvidenceBackupError("backup_destination_exists")
    try:
        os.link(source, destination)
        _fsync_file(destination)
        _fsync_directory(destination.parent)
        source.unlink()
    except FileExistsError as exc:
        raise RawEvidenceBackupError("backup_destination_exists") from exc
    except OSError as exc:
        with suppress(OSError):
            destination.unlink()
        raise RawEvidenceBackupError("backup_publish_failed") from exc


def _copy_verified(source: Path, destination: Path, expected_hash: str, expected_size: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as reader, destination.open("xb") as writer:
            while True:
                block = reader.read(CHUNK_SIZE)
                if not block:
                    break
                size += len(block)
                digest.update(block)
                writer.write(block)
            writer.flush()
            os.fsync(writer.fileno())
    except OSError as exc:
        _remove_file(destination)
        raise RawEvidenceBackupError("cas_copy_failed") from exc
    if size != expected_size or digest.hexdigest() != expected_hash:
        _remove_file(destination)
        raise RawEvidenceBackupError("cas_integrity_failed")


def _add_file(archive: tarfile.TarFile, source: Path, name: str) -> None:
    info = tarfile.TarInfo(name)
    info.size = source.stat().st_size
    info.mode = 0o600
    info.mtime = 0
    with source.open("rb") as handle:
        archive.addfile(info, handle)


def _add_bytes(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    import io

    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o600
    info.mtime = 0
    archive.addfile(info, io.BytesIO(data))


def _derive_kek(shared_secret: bytes, salt: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=salt,
        info=b"ombre-backup-key-wrap-v1",
    ).derive(shared_secret)


def _fingerprint(key: X25519PublicKey) -> str:
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "x25519-sha256:" + hashlib.sha256(raw).hexdigest()


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: Any, expected_length: int) -> bytes:
    if not isinstance(value, str):
        raise RawEvidenceBackupError("backup_container_invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise RawEvidenceBackupError("backup_container_invalid") from exc
    if len(decoded) != expected_length or _b64(decoded) != value:
        raise RawEvidenceBackupError("backup_container_invalid")
    return decoded


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _canonical_json(value: Any) -> bytes:
    return _canonical(value)


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value)


def _valid_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _bounded_int(value: Any, minimum: int, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def _timestamp(clock=None) -> str:
    value = (clock or (lambda: datetime.now(timezone.utc)))()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RawEvidenceBackupError("timestamp_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _strict_within(parent: Path, child: Path) -> bool:
    try:
        parent_resolved = parent.resolve(strict=False)
        child_resolved = child.resolve(strict=False)
        if child_resolved == parent_resolved:
            return False
        child_resolved.relative_to(parent_resolved)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


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
            value = current.lstat()
        except OSError:
            return True
        if current.is_symlink() or getattr(value, "st_file_attributes", 0) & 0x400:
            return True
    return False


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_file(path: Path) -> None:
    with suppress(OSError):
        path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    """Run the bounded operator-only O5E command surface."""

    parser = argparse.ArgumentParser(description="O5E Raw Evidence backup operations")
    sub = parser.add_subparsers(dest="operation", required=True)

    def common(command):
        command.add_argument("--live-root", required=True)
        command.add_argument("--backup-root", default=os.environ.get(BACKUP_ROOT_ENV, ""))
        command.add_argument("--forbidden-root", action="append", default=[])

    preflight = sub.add_parser("backup-preflight")
    common(preflight)
    create = sub.add_parser("backup-create")
    common(create)
    create.add_argument("--recipient-public-key", required=True)
    verify = sub.add_parser("backup-verify")
    common(verify)
    verify.add_argument("bundle")
    verify.add_argument("--private-key", required=True)
    prune = sub.add_parser("backup-prune")
    common(prune)
    audit = sub.add_parser("integrity-audit")
    common(audit)
    claim = sub.add_parser("claim-status")
    claim.add_argument("--live-root", required=True)
    authority_status = sub.add_parser("authority-status")
    authority_status.add_argument("--backup-root", default=os.environ.get(BACKUP_ROOT_ENV, ""))
    restore_verify = sub.add_parser("restore-verify")
    common(restore_verify)
    restore_verify.add_argument("bundle")
    restore_verify.add_argument("--private-key", required=True)
    restore_stage = sub.add_parser("restore-stage")
    common(restore_stage)
    restore_stage.add_argument("bundle")
    restore_stage.add_argument("target_root")
    restore_stage.add_argument("--private-key", required=True)
    restore_root = sub.add_parser("restore-create-root")
    common(restore_root)
    restore_root.add_argument("stage_root")
    restore_root.add_argument("target_root")
    restore_root.add_argument("--private-key", required=True)

    args = parser.parse_args(argv)
    try:
        if args.operation == "authority-status":
            authority = RawEvidenceBackupAuthority.from_env() if not args.backup_root else RawEvidenceBackupAuthority.open(args.backup_root)
            payload = {
                "status": "success",
                "repository_id": authority.repository_id(),
                "restore_epoch": authority.current_restore_epoch(),
                "pending_revocations": len(authority.pending_revocations()),
                "catalog_count": len(authority.catalog_entries()),
            }
        elif args.operation == "claim-status":
            store = RawEvidenceStore(args.live_root)
            payload = {"status": "success", "claim": store.backup_claim_status()}
        else:
            if not args.backup_root:
                raise RawEvidenceBackupError("backup_repository_missing")
            forbidden = tuple(args.forbidden_root)
            store = RawEvidenceStore(args.live_root, forbidden_roots=forbidden)
            authority = RawEvidenceBackupAuthority.open(
                args.backup_root,
                live_root=store.root,
                forbidden_roots=forbidden,
            )
            if args.operation == "backup-preflight":
                payload = {
                    "status": "success",
                    "repository_id": authority.repository_id(),
                    "restore_epoch": authority.current_restore_epoch(),
                    "retention_days": BackupConfig.from_env().retention.days,
                    "claim": store.backup_claim_status(),
                }
            elif args.operation == "backup-create":
                service = RawEvidenceBackupService(
                    store, authority, _load_cli_public_key(args.recipient_public_key)
                )
                payload = service.create()
            elif args.operation == "backup-verify":
                private = _load_cli_private_key(args.private_key)
                payload = RawEvidenceBackupService(store, authority, private.public_key()).verify(args.bundle, private)
            elif args.operation in {"backup-prune", "integrity-audit"}:
                service = RawEvidenceBackupService(store, authority, X25519PrivateKey.generate().public_key())
                payload = service.prune() if args.operation == "backup-prune" else service.integrity_audit()
            else:
                from raw_evidence_restore import RawEvidenceRestoreService

                private = _load_cli_private_key(args.private_key)
                service = RawEvidenceRestoreService(store, authority, private)
                if args.operation == "restore-verify":
                    payload = service.verify(args.bundle)
                elif args.operation == "restore-stage":
                    payload = service.stage(args.bundle, args.target_root)
                else:
                    payload = service.create_root(args.stage_root, args.target_root)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (RawEvidenceError, BackupAuthorityError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"status": getattr(exc, "code", "operation_failed")}, sort_keys=True))
        return 1


def _load_cli_public_key(path: str) -> X25519PublicKey:
    try:
        value = serialization.load_pem_public_key(Path(path).read_bytes())
    except (OSError, TypeError, ValueError) as exc:
        raise RawEvidenceBackupError("backup_key_invalid") from exc
    if not isinstance(value, X25519PublicKey):
        raise RawEvidenceBackupError("backup_key_invalid")
    return value


def _load_cli_private_key(path: str) -> X25519PrivateKey:
    try:
        value = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    except (OSError, TypeError, ValueError) as exc:
        raise RawEvidenceBackupError("backup_key_invalid") from exc
    if not isinstance(value, X25519PrivateKey):
        raise RawEvidenceBackupError("backup_key_invalid")
    return value


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BACKUP_FORMAT_VERSION",
    "BACKUP_RETENTION_ENV",
    "BACKUP_ROOT_ENV",
    "BUNDLE_SUFFIX",
    "BackupConfig",
    "RawEvidenceBackupError",
    "RawEvidenceBackupService",
]
