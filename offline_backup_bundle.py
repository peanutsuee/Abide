"""Offline encrypted backup bundles and isolated restore verification."""

from __future__ import annotations

import argparse
import base64
from contextlib import closing, contextmanager
import ctypes
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import stat
import struct
import sys
import tarfile
import tempfile
import threading
import time
import unicodedata
from typing import Any, Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from maintenance_write_gate import FreezeLease, MaintenanceWriteCoordinator


WORKSPACE_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
BUNDLE_FORMAT_VERSION = 1
CONTAINER_VERSION = 1
WORKSPACE_MANIFEST = "workspace-manifest.json"
WORKSPACE_MARKER = ".ombre-stage8h-g1b-backup"
ARCHIVE_MANIFEST_PATH = "manifest.json"
BUNDLE_SUFFIX = ".obbackup"
PRIVATE_KEY_SUFFIX = ".obx25519-private"
PUBLIC_KEY_SUFFIX = ".obx25519-public"
CAPTURE_MODE = "offline_quiesced_source_required"
ENCRYPTION_PROFILE = "X25519-HKDF-SHA256+A256GCM"
MAGIC = b"OBBNDL1\n"
HEADER_LENGTH_SIZE = 4
GCM_TAG_SIZE = 16
MAX_HEADER_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
EXPECTED_REMEMBER_ME_VERSION = "0.1.0.dev7"

_FIXED_PATHS = {
    "source": "source",
    "bundles": "bundles",
    "restored": "restored",
    "reports": "reports",
    "temp": "temp",
}
_WORKSPACE_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_NONCE_PATTERN = re.compile(r"[0-9a-f]{64}")
_BUNDLE_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_GIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_EXACT_EXCLUSIONS = {
    ".dashboard_auth.json": "authentication_material",
    ".backup_state.json": "backup_state",
    ".env": "credential_material",
    "credentials.json": "credential_material",
    "secrets.json": "credential_material",
}
_EXCLUDED_DIRECTORY_NAMES = {
    ".tmp": "temporary_file",
    "__pycache__": "temporary_file",
    ".gnupg": "credential_material",
    ".ssh": "credential_material",
}
_SQLITE_SIDECAR_SUFFIXES = {
    "-wal": "sqlite_sidecar",
    "-shm": "sqlite_sidecar",
    "-journal": "sqlite_sidecar",
}
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_STABLE_STATUSES = {
    "success",
    "workspace_invalid",
    "source_changed",
    "source_unsupported",
    "sqlite_snapshot_failed",
    "bundle_invalid",
    "key_invalid",
    "authentication_failed",
    "manifest_invalid",
    "restore_target_invalid",
    "restore_failed",
    "capture_cancelled",
    "freeze_lease_expired",
    "capture_source_too_large",
    "capture_space_insufficient",
    "capture_bundle_too_large",
    "internal_error",
}
_EXIT_CODES = {
    "success": 0,
    "workspace_invalid": 2,
    "source_changed": 3,
    "source_unsupported": 4,
    "sqlite_snapshot_failed": 5,
    "bundle_invalid": 6,
    "key_invalid": 7,
    "authentication_failed": 8,
    "manifest_invalid": 9,
    "restore_target_invalid": 10,
    "restore_failed": 11,
    "internal_error": 12,
    "capture_cancelled": 13,
    "freeze_lease_expired": 14,
    "capture_source_too_large": 15,
    "capture_space_insufficient": 16,
    "capture_bundle_too_large": 17,
}


class BackupBundleError(RuntimeError):
    """Stable public failure that carries no internal exception text."""

    def __init__(self, status: str):
        if status not in _STABLE_STATUSES:
            status = "internal_error"
        self.status = status
        super().__init__(status)


class CaptureAbortSignal:
    """Thread-safe cooperative stop signal for internal capture workers."""

    def __init__(
        self,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._deadline = deadline
        self._monotonic = monotonic
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._code: str | None = None

    def abort(self, code: str = "capture_cancelled") -> None:
        if code not in {"capture_cancelled", "freeze_lease_expired"}:
            code = "capture_cancelled"
        with self._lock:
            if self._code is None:
                self._code = code
                self._event.set()

    def raise_if_aborted(self) -> None:
        with self._lock:
            code = self._code
        if code is not None:
            raise BackupBundleError(code)
        if self._deadline is not None and self._monotonic() >= self._deadline:
            self.abort("freeze_lease_expired")
            raise BackupBundleError("freeze_lease_expired")


def _check_abort(abort_signal: CaptureAbortSignal | None) -> None:
    if abort_signal is not None:
        abort_signal.raise_if_aborted()


def _call_abortable(function, *args, abort_signal=None, **kwargs):
    if abort_signal is not None:
        kwargs["abort_signal"] = abort_signal
    return function(*args, **kwargs)


@dataclass(frozen=True)
class BackupWorkspace:
    root: Path
    workspace_id: str
    nonce: str
    source_root: Path
    bundles_root: Path
    restored_root: Path
    reports_root: Path
    temp_root: Path


@dataclass(frozen=True)
class CaptureResult:
    status: str
    bundle_id: str
    bundle_name: str
    manifest_sha256: str
    entry_count: int
    ordinary_file_count: int
    sqlite_snapshot_count: int
    total_plaintext_bytes: int
    exclusion_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SourceInventoryRecord:
    relative_path: str
    item_type: str
    exclusion_reason: str | None
    collision_key: str
    identity: tuple[int, int, int, int] | None
    size_bytes: int | None
    sha256: str | None


@dataclass(frozen=True)
class SourceInventory:
    root_identity: tuple[int, int, int, int]
    records: tuple[SourceInventoryRecord, ...]


@dataclass(frozen=True)
class FrozenCaptureLimits:
    """Bounds enforced against the exact inventory captured under freeze."""

    max_source_bytes: int
    max_bundle_bytes: int
    minimum_free_bytes: int

    def validate(self) -> None:
        if (
            self.max_source_bytes <= 0
            or self.max_bundle_bytes <= 0
            or self.minimum_free_bytes < 0
        ):
            raise BackupBundleError("internal_error")


def prepare_backup_workspace(path: str | Path) -> BackupWorkspace:
    """Create the fixed offline workspace without copying source data."""
    root = _validate_prepare_root(path)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise BackupBundleError("workspace_invalid")
    root.mkdir(parents=True, exist_ok=True)
    for relative in _FIXED_PATHS.values():
        (root / relative).mkdir()
    workspace_id = secrets.token_hex(16)
    nonce = secrets.token_hex(32)
    manifest = {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "workspace_id": workspace_id,
        "nonce": nonce,
        "paths": dict(_FIXED_PATHS),
        "created_at": _timestamp(),
    }
    marker = {"workspace_id": workspace_id, "nonce": nonce}
    _atomic_write_json(root / WORKSPACE_MANIFEST, manifest)
    _atomic_write_json(root / WORKSPACE_MARKER, marker)
    return load_backup_workspace(root)


def load_backup_workspace(path: str | Path) -> BackupWorkspace:
    """Validate workspace identity, containment, and reparse boundaries."""
    try:
        candidate = Path(path)
        if not candidate.is_absolute() or _path_contains_reparse_point(candidate):
            raise BackupBundleError("workspace_invalid")
        root = candidate.resolve(strict=True)
        _validate_root_location(root)
        manifest = json.loads(
            (root / WORKSPACE_MANIFEST).read_text(encoding="utf-8")
        )
        marker = json.loads(
            (root / WORKSPACE_MARKER).read_text(encoding="utf-8")
        )
    except BackupBundleError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise BackupBundleError("workspace_invalid") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != WORKSPACE_SCHEMA_VERSION
        or _WORKSPACE_ID_PATTERN.fullmatch(
            str(manifest.get("workspace_id", ""))
        ) is None
        or _NONCE_PATTERN.fullmatch(str(manifest.get("nonce", ""))) is None
        or manifest.get("paths") != _FIXED_PATHS
        or marker != {
            "workspace_id": manifest["workspace_id"],
            "nonce": manifest["nonce"],
        }
    ):
        raise BackupBundleError("workspace_invalid")
    roots = {
        key: (root / relative).resolve(strict=True)
        for key, relative in _FIXED_PATHS.items()
    }
    _validate_workspace_paths(root, roots)
    return BackupWorkspace(
        root=root,
        workspace_id=manifest["workspace_id"],
        nonce=manifest["nonce"],
        source_root=roots["source"],
        bundles_root=roots["bundles"],
        restored_root=roots["restored"],
        reports_root=roots["reports"],
        temp_root=roots["temp"],
    )


def capture_bundle(
    workspace_path: str | Path,
    recipient_public_key: X25519PublicKey,
    *,
    ob_commit_sha: str,
    remember_me_version: str = EXPECTED_REMEMBER_ME_VERSION,
    clock: Callable[[], datetime] | None = None,
    chunk_size: int = CHUNK_SIZE,
) -> CaptureResult:
    """Capture one synthetic, offline-quiesced source into an encrypted bundle."""
    workspace = load_backup_workspace(workspace_path)
    _validate_public_key(recipient_public_key)
    if _GIT_SHA_PATTERN.fullmatch(ob_commit_sha) is None:
        raise BackupBundleError("workspace_invalid")
    if remember_me_version != EXPECTED_REMEMBER_ME_VERSION:
        raise BackupBundleError("workspace_invalid")
    _validate_chunk_size(chunk_size)
    return _capture_source_into_bundle(
        workspace,
        workspace.source_root,
        recipient_public_key,
        ob_commit_sha=ob_commit_sha,
        remember_me_version=remember_me_version,
        clock=clock,
        chunk_size=chunk_size,
    )


def capture_external_source(
    workspace_path: str | Path,
    source_root: str | Path,
    authorized_source_root: str | Path,
    recipient_public_key: X25519PublicKey,
    *,
    coordinator: MaintenanceWriteCoordinator,
    freeze_lease: FreezeLease,
    ob_commit_sha: str,
    remember_me_version: str = EXPECTED_REMEMBER_ME_VERSION,
    clock: Callable[[], datetime] | None = None,
    chunk_size: int = CHUNK_SIZE,
    abort_signal: CaptureAbortSignal | None = None,
    frozen_limits: FrozenCaptureLimits | None = None,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
) -> CaptureResult:
    """Capture one explicitly authorized external root under a live freeze."""
    workspace = load_backup_workspace(workspace_path)
    coordinator.validate_lease(freeze_lease)
    source = _validate_external_source(
        workspace,
        source_root,
        authorized_source_root,
    )
    _validate_public_key(recipient_public_key)
    if _GIT_SHA_PATTERN.fullmatch(ob_commit_sha) is None:
        raise BackupBundleError("workspace_invalid")
    if remember_me_version != EXPECTED_REMEMBER_ME_VERSION:
        raise BackupBundleError("workspace_invalid")
    _validate_chunk_size(chunk_size)
    _check_abort(abort_signal)
    result = _capture_source_into_bundle(
        workspace,
        source,
        recipient_public_key,
        ob_commit_sha=ob_commit_sha,
        remember_me_version=remember_me_version,
        clock=clock,
        chunk_size=chunk_size,
        abort_signal=abort_signal,
        frozen_limits=frozen_limits,
        disk_usage=disk_usage,
    )
    try:
        coordinator.validate_lease(freeze_lease)
    except BaseException:
        try:
            (workspace.bundles_root / result.bundle_name).unlink(missing_ok=True)
        except OSError as cleanup_exc:
            raise BackupBundleError("internal_error") from cleanup_exc
        raise
    return result


def _capture_source_into_bundle(
    workspace: BackupWorkspace,
    source_root: Path,
    recipient_public_key: X25519PublicKey,
    *,
    ob_commit_sha: str,
    remember_me_version: str,
    clock: Callable[[], datetime] | None,
    chunk_size: int,
    abort_signal: CaptureAbortSignal | None = None,
    frozen_limits: FrozenCaptureLimits | None = None,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
) -> CaptureResult:
    bundle_id = secrets.token_hex(16)
    bundle_name = f"{bundle_id}{BUNDLE_SUFFIX}"
    final_bundle = workspace.bundles_root / bundle_name
    if final_bundle.exists():
        raise BackupBundleError("bundle_invalid")
    operation_root = Path(tempfile.mkdtemp(
        prefix=f"capture-{bundle_id}-", dir=workspace.temp_root
    ))
    staging_root = operation_root / "staging"
    archive_path = operation_root / "payload.tar"
    staging_root.mkdir()
    try:
        initial_inventory = _call_abortable(
            _inventory_source,
            source_root,
            chunk_size=chunk_size,
            abort_signal=abort_signal,
        )
        if frozen_limits is not None:
            _validate_frozen_capture_limits(
                initial_inventory,
                workspace.temp_root,
                frozen_limits,
                disk_usage=disk_usage,
            )
        _check_abort(abort_signal)
        entries, exclusions = _call_abortable(
            _capture_source,
            source_root,
            staging_root,
            initial_inventory,
            chunk_size=chunk_size,
            abort_signal=abort_signal,
        )
        try:
            final_inventory = _call_abortable(
                _inventory_source,
                source_root,
                chunk_size=chunk_size,
                abort_signal=abort_signal,
            )
        except BackupBundleError as exc:
            if exc.status == "source_unsupported":
                raise BackupBundleError("source_changed") from exc
            raise
        _validate_capture_stability(
            initial_inventory,
            final_inventory,
            entries,
        )
        created_at = _timestamp(clock)
        manifest = _build_manifest(
            workspace=workspace,
            source_root=source_root,
            bundle_id=bundle_id,
            created_at=created_at,
            ob_commit_sha=ob_commit_sha,
            remember_me_version=remember_me_version,
            recipient_fingerprint=_public_key_fingerprint(recipient_public_key),
            entries=entries,
            exclusions=exclusions,
        )
        manifest_bytes = _canonical_json_bytes(manifest)
        _validate_manifest(manifest_bytes)
        _call_abortable(
            _build_archive,
            archive_path,
            staging_root,
            manifest_bytes,
            entries,
            chunk_size=chunk_size,
            abort_signal=abort_signal,
        )
        _check_abort(abort_signal)
        _call_abortable(
            _encrypt_archive,
            archive_path,
            final_bundle,
            recipient_public_key,
            capture_workspace_id=workspace.workspace_id,
            bundle_id=bundle_id,
            created_at=created_at,
            chunk_size=chunk_size,
            abort_signal=abort_signal,
            max_output_bytes=(
                frozen_limits.max_bundle_bytes
                if frozen_limits is not None
                else None
            ),
        )
        try:
            _check_abort(abort_signal)
        except BackupBundleError:
            _remove_file(final_bundle)
            raise
        ordinary_count = sum(
            entry["entry_type"] == "regular" for entry in entries
        )
        sqlite_count = len(entries) - ordinary_count
        return CaptureResult(
            status="success",
            bundle_id=bundle_id,
            bundle_name=bundle_name,
            manifest_sha256=manifest["manifest_sha256"],
            entry_count=len(entries),
            ordinary_file_count=ordinary_count,
            sqlite_snapshot_count=sqlite_count,
            total_plaintext_bytes=sum(entry["size_bytes"] for entry in entries),
            exclusion_count=len(exclusions),
        )
    except BackupBundleError:
        raise
    except Exception as exc:
        raise BackupBundleError("internal_error") from exc
    finally:
        _safe_rmtree(workspace, operation_root)


def inspect_bundle(
    workspace_path: str | Path,
    bundle_name: str,
) -> dict[str, Any]:
    """Read and validate only the public container header."""
    workspace = load_backup_workspace(workspace_path)
    bundle = _bundle_path(workspace, bundle_name)
    header, _, _, _ = _read_header(bundle)
    return {
        "status": "success",
        "authenticated": False,
        "metadata_trust": "unverified_header",
        "container_version": header["container_version"],
        "bundle_format_version": header["bundle_format_version"],
        "bundle_id": header["bundle_id"],
        "capture_workspace_id": header["capture_workspace_id"],
        "created_at": header["created_at"],
        "encryption_profile": header["encryption_profile"],
        "recipient_key_fingerprint": header["recipient_key_fingerprint"],
    }


def verify_bundle(
    workspace_path: str | Path,
    bundle_name: str,
    recipient_private_key: X25519PrivateKey,
    *,
    chunk_size: int = CHUNK_SIZE,
) -> dict[str, Any]:
    """Authenticate and verify a bundle without publishing restored data."""
    workspace = load_backup_workspace(workspace_path)
    _validate_private_key(recipient_private_key)
    _validate_chunk_size(chunk_size)
    bundle = _bundle_path(workspace, bundle_name)
    operation_root = Path(tempfile.mkdtemp(prefix="verify-", dir=workspace.temp_root))
    try:
        result = _decrypt_and_validate(
            workspace,
            bundle,
            recipient_private_key,
            operation_root,
            chunk_size=chunk_size,
        )
        return {
            "status": "success",
            "authenticated": True,
            "metadata_trust": "authenticated_bundle",
            "bundle_id": result["manifest"]["bundle_id"],
            "capture_workspace_id": result["manifest"]["capture_workspace_id"],
            "manifest_sha256": result["manifest"]["manifest_sha256"],
            "entry_count": result["manifest"]["entry_count"],
            "total_plaintext_bytes": result["manifest"]["total_plaintext_bytes"],
        }
    finally:
        _safe_rmtree(workspace, operation_root)


def restore_bundle(
    workspace_path: str | Path,
    bundle_name: str,
    recipient_private_key: X25519PrivateKey,
    *,
    restore_name: str | None = None,
    chunk_size: int = CHUNK_SIZE,
) -> dict[str, Any]:
    """Restore only after complete authentication into an isolated root."""
    workspace = load_backup_workspace(workspace_path)
    _validate_private_key(recipient_private_key)
    _validate_chunk_size(chunk_size)
    bundle = _bundle_path(workspace, bundle_name)
    header, _, _, _ = _read_header(bundle)
    target_name = restore_name or header["bundle_id"]
    if _BUNDLE_ID_PATTERN.fullmatch(target_name) is None:
        raise BackupBundleError("restore_target_invalid")
    final_root = workspace.restored_root / target_name
    with _exclusive_operation_lock(workspace, "restore"):
        if final_root.exists():
            raise BackupBundleError("restore_target_invalid")
        operation_root = Path(tempfile.mkdtemp(
            prefix=f"restore-{target_name}-", dir=workspace.temp_root
        ))
        try:
            result = _decrypt_and_validate(
                workspace,
                bundle,
                recipient_private_key,
                operation_root,
                chunk_size=chunk_size,
            )
            staged_restore = result["restore_root"]
            _publish_directory_no_replace(staged_restore, final_root)
            return {
                "status": "success",
                "authenticated": True,
                "metadata_trust": "authenticated_bundle",
                "bundle_id": result["manifest"]["bundle_id"],
                "capture_workspace_id": result["manifest"][
                    "capture_workspace_id"
                ],
                "manifest_sha256": result["manifest"]["manifest_sha256"],
                "entry_count": result["manifest"]["entry_count"],
                "restore_name": target_name,
            }
        except BackupBundleError:
            raise
        except Exception as exc:
            raise BackupBundleError("restore_failed") from exc
        finally:
            _safe_rmtree(workspace, operation_root)


def generate_test_keypair() -> tuple[X25519PrivateKey, X25519PublicKey]:
    """Generate an ephemeral test-only recipient keypair."""
    private_key = X25519PrivateKey.generate()
    return private_key, private_key.public_key()


def write_test_private_key(path: str | Path, key: X25519PrivateKey) -> None:
    """Write a synthetic test key using the dedicated ignored extension."""
    destination = Path(path)
    if destination.suffix != PRIVATE_KEY_SUFFIX:
        raise BackupBundleError("key_invalid")
    data = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _exclusive_key_write(destination, data)


def write_test_public_key(path: str | Path, key: X25519PublicKey) -> None:
    destination = Path(path)
    if destination.suffix != PUBLIC_KEY_SUFFIX:
        raise BackupBundleError("key_invalid")
    data = key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    _exclusive_key_write(destination, data)


def load_private_key(path: str | Path) -> X25519PrivateKey:
    data = _read_key_file(path, PRIVATE_KEY_SUFFIX)
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (TypeError, ValueError) as exc:
        raise BackupBundleError("key_invalid") from exc
    if not isinstance(key, X25519PrivateKey):
        raise BackupBundleError("key_invalid")
    return key


def load_public_key(path: str | Path) -> X25519PublicKey:
    data = _read_key_file(path, PUBLIC_KEY_SUFFIX)
    try:
        key = serialization.load_pem_public_key(data)
    except (TypeError, ValueError) as exc:
        raise BackupBundleError("key_invalid") from exc
    if not isinstance(key, X25519PublicKey):
        raise BackupBundleError("key_invalid")
    return key


def _capture_source(
    source_root: Path,
    staging_root: Path,
    inventory: SourceInventory,
    *,
    chunk_size: int,
    abort_signal: CaptureAbortSignal | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    entries: list[dict[str, Any]] = []
    exclusions = [
        {
            "relative_path": record.relative_path,
            "reason": record.exclusion_reason,
        }
        for record in inventory.records
        if record.exclusion_reason is not None
    ]
    for record in inventory.records:
        _check_abort(abort_signal)
        if record.item_type not in {"regular", "sqlite"}:
            continue
        relative = record.relative_path
        path = source_root / Path(*PurePosixPath(relative).parts)
        destination = staging_root / Path(*PurePosixPath(relative).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if record.item_type == "sqlite":
            evidence = _call_abortable(
                _snapshot_sqlite,
                path,
                destination,
                chunk_size=chunk_size,
                abort_signal=abort_signal,
            )
            entry = {
                "relative_path": relative,
                "entry_type": "sqlite_snapshot",
                "category": _category(relative),
                "size_bytes": evidence.pop("snapshot_file_size"),
                "sha256": evidence.pop("snapshot_sha256"),
                **evidence,
            }
        else:
            size, digest = _call_abortable(
                _copy_regular_file_stable,
                path,
                destination,
                chunk_size=chunk_size,
                abort_signal=abort_signal,
            )
            entry = {
                "relative_path": relative,
                "entry_type": "regular",
                "category": _category(relative),
                "size_bytes": size,
                "sha256": digest,
            }
        entries.append(entry)
    entries.sort(key=lambda item: item["relative_path"])
    exclusions.sort(key=lambda item: item["relative_path"])
    return entries, exclusions


def _inventory_source(
    source_root: Path,
    *,
    chunk_size: int,
    abort_signal: CaptureAbortSignal | None = None,
) -> SourceInventory:
    _check_abort(abort_signal)
    first = _call_abortable(
        _scan_source_metadata, source_root, abort_signal=abort_signal
    )
    first = _call_abortable(
        _add_inventory_hashes,
        source_root, first, chunk_size=chunk_size, abort_signal=abort_signal
    )
    second = _call_abortable(
        _scan_source_metadata, source_root, abort_signal=abort_signal
    )
    second = _call_abortable(
        _add_inventory_hashes,
        source_root, second, chunk_size=chunk_size, abort_signal=abort_signal
    )
    if first != second:
        raise BackupBundleError("source_changed")
    return second


def _validate_frozen_capture_limits(
    inventory: SourceInventory,
    work_root: Path,
    limits: FrozenCaptureLimits,
    *,
    disk_usage: Callable[[Path], Any],
) -> None:
    limits.validate()
    source_bytes = sum(
        record.size_bytes or 0
        for record in inventory.records
        if record.item_type in {"regular", "sqlite", "sqlite_content_sidecar"}
    )
    if source_bytes > limits.max_source_bytes:
        raise BackupBundleError("capture_source_too_large")
    required = source_bytes * 3 + limits.minimum_free_bytes
    try:
        free_bytes = int(disk_usage(work_root).free)
    except Exception as exc:
        raise BackupBundleError("internal_error") from exc
    if free_bytes < required:
        raise BackupBundleError("capture_space_insufficient")


def _scan_source_metadata(
    source_root: Path,
    *,
    abort_signal: CaptureAbortSignal | None = None,
) -> SourceInventory:
    root_before = _file_identity(source_root)
    records: list[SourceInventoryRecord] = []
    stack = [source_root]
    while stack:
        _check_abort(abort_signal)
        directory = stack.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise BackupBundleError("source_unsupported") from exc
        directories: list[Path] = []
        for child in children:
            _check_abort(abort_signal)
            relative = child.relative_to(source_root).as_posix()
            try:
                _validate_relative_path(relative)
            except BackupBundleError as exc:
                raise BackupBundleError("source_unsupported") from exc
            try:
                metadata = child.lstat()
            except OSError as exc:
                raise BackupBundleError("source_unsupported") from exc
            if child.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
                raise BackupBundleError("source_unsupported")
            if stat.S_ISDIR(metadata.st_mode):
                reason = _EXCLUDED_DIRECTORY_NAMES.get(child.name)
                records.append(SourceInventoryRecord(
                    relative_path=relative,
                    item_type="excluded_directory" if reason else "directory",
                    exclusion_reason=reason,
                    collision_key=_path_collision_key(relative),
                    identity=_file_identity(child),
                    size_bytes=None,
                    sha256=None,
                ))
                if reason is None:
                    directories.append(child)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise BackupBundleError("source_unsupported")
            reason = _exclusion_reason(child.name)
            lowered = child.name.casefold()
            if lowered.endswith("-shm"):
                item_type = "sqlite_coordination_sidecar"
                identity = None
            elif lowered.endswith(("-wal", "-journal")):
                item_type = "sqlite_content_sidecar"
                identity = _file_identity(child)
            elif reason is not None:
                item_type = "excluded_file"
                identity = _file_identity(child)
            elif _is_sqlite_path(child):
                item_type = "sqlite"
                identity = _file_identity(child)
            else:
                item_type = "regular"
                identity = _file_identity(child)
            records.append(SourceInventoryRecord(
                relative_path=relative,
                item_type=item_type,
                exclusion_reason=reason,
                collision_key=_path_collision_key(relative),
                identity=identity,
                size_bytes=None,
                sha256=None,
            ))
        stack.extend(reversed(directories))
    root_after = _file_identity(source_root)
    if root_before != root_after:
        raise BackupBundleError("source_changed")
    records.sort(key=lambda record: record.relative_path)
    collision_keys = [record.collision_key for record in records]
    if len(collision_keys) != len(set(collision_keys)):
        raise BackupBundleError("source_unsupported")
    return SourceInventory(root_identity=root_after, records=tuple(records))


def _add_inventory_hashes(
    source_root: Path,
    inventory: SourceInventory,
    *,
    chunk_size: int,
    abort_signal: CaptureAbortSignal | None = None,
) -> SourceInventory:
    records: list[SourceInventoryRecord] = []
    for record in inventory.records:
        _check_abort(abort_signal)
        if record.item_type not in {
            "regular",
            "sqlite",
            "sqlite_content_sidecar",
        }:
            records.append(record)
            continue
        path = source_root / Path(*PurePosixPath(record.relative_path).parts)
        identity, size, digest = _call_abortable(
            _stable_source_file_evidence,
            path,
            chunk_size=chunk_size,
            abort_signal=abort_signal,
        )
        if identity != record.identity:
            raise BackupBundleError("source_changed")
        records.append(SourceInventoryRecord(
            relative_path=record.relative_path,
            item_type=record.item_type,
            exclusion_reason=record.exclusion_reason,
            collision_key=record.collision_key,
            identity=identity,
            size_bytes=size,
            sha256=digest,
        ))
    if _file_identity(source_root) != inventory.root_identity:
        raise BackupBundleError("source_changed")
    return SourceInventory(
        root_identity=inventory.root_identity,
        records=tuple(records),
    )


def _validate_capture_stability(
    initial: SourceInventory,
    final: SourceInventory,
    entries: list[dict[str, Any]],
) -> None:
    if initial != final:
        raise BackupBundleError("source_changed")
    captured = {
        entry["relative_path"]: entry
        for entry in entries
    }
    expected_paths = {
        record.relative_path
        for record in final.records
        if record.item_type in {"regular", "sqlite"}
    }
    if set(captured) != expected_paths:
        raise BackupBundleError("source_changed")
    for record in final.records:
        if record.item_type != "regular":
            continue
        entry = captured[record.relative_path]
        if (
            entry["entry_type"] != "regular"
            or entry["size_bytes"] != record.size_bytes
            or entry["sha256"] != record.sha256
        ):
            raise BackupBundleError("source_changed")


def _stable_source_file_evidence(
    path: Path,
    *,
    chunk_size: int,
    abort_signal: CaptureAbortSignal | None = None,
) -> tuple[tuple[int, int, int, int], int, str]:
    before = _file_identity(path)
    try:
        size, digest = _call_abortable(
            _hash_file,
            path, chunk_size=chunk_size, abort_signal=abort_signal
        )
    except BackupBundleError as exc:
        if exc.status in {"capture_cancelled", "freeze_lease_expired"}:
            raise
        raise BackupBundleError("source_changed") from exc
    after = _file_identity(path)
    if before != after or size != after[2]:
        raise BackupBundleError("source_changed")
    return after, size, digest


def _exclusion_reason(name: str) -> str | None:
    if name in _EXACT_EXCLUSIONS:
        return _EXACT_EXCLUSIONS[name]
    lowered = name.casefold()
    if lowered == ".env" or lowered.startswith(".env."):
        return "credential_material"
    if lowered in {"id_dsa", "id_ecdsa", "id_ed25519", "id_rsa"}:
        return "credential_material"
    for suffix, reason in _SQLITE_SIDECAR_SUFFIXES.items():
        if lowered.endswith(suffix):
            return reason
    if lowered.endswith((".bak", ".part", ".partial", ".swp", ".tmp", ".temp", "~")):
        return "temporary_file"
    if lowered.endswith((".lock", ".lck")):
        return "lock_file"
    if lowered.endswith((PRIVATE_KEY_SUFFIX, ".pem", ".key")):
        return "credential_material"
    return None


def _is_sqlite_path(path: Path) -> bool:
    if path.suffix.casefold() in {".sqlite3", ".db"}:
        return True
    try:
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError as exc:
        raise BackupBundleError("source_unsupported") from exc


def _copy_regular_file_stable(
    source: Path,
    destination: Path,
    *,
    chunk_size: int,
    abort_signal: CaptureAbortSignal | None = None,
) -> tuple[int, str]:
    before = _file_identity(source)
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as reader, destination.open("xb") as writer:
            while True:
                _check_abort(abort_signal)
                block = reader.read(chunk_size)
                if not block:
                    break
                writer.write(block)
                digest.update(block)
                size += len(block)
            writer.flush()
            os.fsync(writer.fileno())
    except OSError as exc:
        raise BackupBundleError("source_unsupported") from exc
    after = _file_identity(source)
    if before != after or size != before[2]:
        raise BackupBundleError("source_changed")
    confirm_size, confirm_digest = _call_abortable(
        _hash_file,
        source, chunk_size=chunk_size, abort_signal=abort_signal
    )
    if confirm_size != size or confirm_digest != digest.hexdigest():
        raise BackupBundleError("source_changed")
    return size, digest.hexdigest()


def _snapshot_sqlite(
    source: Path,
    destination: Path,
    *,
    chunk_size: int,
    abort_signal: CaptureAbortSignal | None = None,
) -> dict[str, Any]:
    before = _call_abortable(
        _sqlite_content_signature,
        source, chunk_size=chunk_size, abort_signal=abort_signal
    )
    try:
        source_connection = sqlite3.connect(
            f"{source.as_uri()}?mode=ro", uri=True, timeout=5
        )
        try:
            source_connection.execute("PRAGMA query_only = ON")
            if source_connection.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise BackupBundleError("sqlite_snapshot_failed")
            data_version_before = source_connection.execute(
                "PRAGMA data_version"
            ).fetchone()[0]
            with closing(sqlite3.connect(destination)) as target_connection:
                def progress(status, remaining, total):
                    del status, remaining, total
                    _check_abort(abort_signal)

                source_connection.backup(
                    target_connection,
                    pages=64,
                    progress=progress,
                )
                target_connection.execute("PRAGMA journal_mode = DELETE")
                target_connection.commit()
            data_version_after = source_connection.execute(
                "PRAGMA data_version"
            ).fetchone()[0]
        finally:
            source_connection.close()
    except BackupBundleError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise BackupBundleError("sqlite_snapshot_failed") from exc
    after = _call_abortable(
        _sqlite_content_signature,
        source, chunk_size=chunk_size, abort_signal=abort_signal
    )
    if before != after or data_version_before != data_version_after:
        raise BackupBundleError("source_changed")
    if any(
        Path(str(destination) + suffix).exists()
        for suffix in ("-wal", "-shm", "-journal")
    ):
        raise BackupBundleError("sqlite_snapshot_failed")
    try:
        connection = sqlite3.connect(
            f"{destination.as_uri()}?mode=ro", uri=True, timeout=5
        )
        try:
            connection.execute("PRAGMA query_only = ON")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise BackupBundleError("sqlite_snapshot_failed")
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            schema_rows = connection.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name, tbl_name
                """
            ).fetchall()
        finally:
            connection.close()
    except BackupBundleError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise BackupBundleError("sqlite_snapshot_failed") from exc
    size, digest = _hash_file(destination, chunk_size=chunk_size)
    schema_payload = [list(row) for row in schema_rows]
    return {
        "page_size": page_size,
        "page_count": page_count,
        "user_version": user_version,
        "schema_sha256": hashlib.sha256(
            _canonical_json_bytes(schema_payload)
        ).hexdigest(),
        "snapshot_file_size": size,
        "snapshot_sha256": digest,
    }


def _build_manifest(
    *,
    workspace: BackupWorkspace,
    source_root: Path | None = None,
    bundle_id: str,
    created_at: str,
    ob_commit_sha: str,
    remember_me_version: str,
    recipient_fingerprint: str,
    entries: list[dict[str, Any]],
    exclusions: list[dict[str, str]],
) -> dict[str, Any]:
    ordinary_count = sum(entry["entry_type"] == "regular" for entry in entries)
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "bundle_format_version": BUNDLE_FORMAT_VERSION,
        "bundle_id": bundle_id,
        "capture_workspace_id": workspace.workspace_id,
        "created_at": created_at,
        "ob_commit_sha": ob_commit_sha,
        "remember_me_version": remember_me_version,
        "capture_mode": CAPTURE_MODE,
        "encryption_profile": ENCRYPTION_PROFILE,
        "recipient_key_fingerprint": recipient_fingerprint,
        "source_identity": _path_identity(source_root or workspace.source_root),
        "entry_count": len(entries),
        "total_plaintext_bytes": sum(entry["size_bytes"] for entry in entries),
        "sqlite_snapshot_count": len(entries) - ordinary_count,
        "ordinary_file_count": ordinary_count,
        "entries": entries,
        "exclusions": exclusions,
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        _canonical_json_bytes(manifest)
    ).hexdigest()
    return manifest


def _build_archive(
    archive_path: Path,
    staging_root: Path,
    manifest_bytes: bytes,
    entries: list[dict[str, Any]],
    *,
    chunk_size: int,
    abort_signal: CaptureAbortSignal | None = None,
) -> None:
    try:
        with tarfile.open(archive_path, mode="w", format=tarfile.PAX_FORMAT) as archive:
            _add_bytes_member(archive, ARCHIVE_MANIFEST_PATH, manifest_bytes)
            for entry in entries:
                _check_abort(abort_signal)
                relative = entry["relative_path"]
                source = staging_root / Path(*PurePosixPath(relative).parts)
                info = _tar_info(f"data/{relative}", entry["size_bytes"])
                with source.open("rb") as handle:
                    archive.addfile(
                        info,
                        _AbortableReader(handle, abort_signal, chunk_size),
                    )
        _fsync_file(archive_path)
    except (OSError, tarfile.TarError) as exc:
        raise BackupBundleError("bundle_invalid") from exc


def _add_bytes_member(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    import io

    archive.addfile(_tar_info(name, len(content)), io.BytesIO(content))


class _AbortableReader:
    def __init__(self, handle, abort_signal, chunk_size: int) -> None:
        self._handle = handle
        self._abort_signal = abort_signal
        self._chunk_size = chunk_size

    def read(self, size: int = -1) -> bytes:
        _check_abort(self._abort_signal)
        if size < 0:
            size = self._chunk_size
        remaining = size
        chunks: list[bytes] = []
        while remaining:
            _check_abort(self._abort_signal)
            block = self._handle.read(min(remaining, self._chunk_size))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)


def _tar_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o600
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _encrypt_archive(
    archive_path: Path,
    final_bundle: Path,
    recipient_public_key: X25519PublicKey,
    *,
    capture_workspace_id: str,
    bundle_id: str,
    created_at: str,
    chunk_size: int,
    abort_signal: CaptureAbortSignal | None = None,
    max_output_bytes: int | None = None,
) -> None:
    ephemeral_private = X25519PrivateKey.generate()
    ephemeral_public = ephemeral_private.public_key().public_bytes_raw()
    salt = os.urandom(32)
    wrap_nonce = os.urandom(12)
    payload_nonce = os.urandom(12)
    content_key = os.urandom(32)
    header_base = {
        "container_version": CONTAINER_VERSION,
        "bundle_format_version": BUNDLE_FORMAT_VERSION,
        "bundle_id": bundle_id,
        "capture_workspace_id": capture_workspace_id,
        "created_at": created_at,
        "encryption_profile": ENCRYPTION_PROFILE,
        "recipient_key_fingerprint": _public_key_fingerprint(recipient_public_key),
        "ephemeral_public_key": _b64(ephemeral_public),
        "hkdf_salt": _b64(salt),
        "wrap_nonce": _b64(wrap_nonce),
        "payload_nonce": _b64(payload_nonce),
    }
    kek = _derive_kek(ephemeral_private.exchange(recipient_public_key), salt)
    wrapped_key = AESGCM(kek).encrypt(
        wrap_nonce, content_key, _canonical_json_bytes(header_base)
    )
    header = {**header_base, "wrapped_content_key": _b64(wrapped_key)}
    header_bytes = _canonical_json_bytes(header)
    if len(header_bytes) > MAX_HEADER_BYTES:
        raise BackupBundleError("bundle_invalid")
    temporary = final_bundle.parent / f".{final_bundle.name}.{secrets.token_hex(8)}.tmp"
    try:
        _check_abort(abort_signal)
        encryptor = Cipher(
            algorithms.AES(content_key), modes.GCM(payload_nonce)
        ).encryptor()
        encryptor.authenticate_additional_data(header_bytes)
        with archive_path.open("rb") as reader, temporary.open("xb") as writer:
            written = 0

            def write_bounded(block: bytes) -> None:
                nonlocal written
                _check_abort(abort_signal)
                if (
                    max_output_bytes is not None
                    and written + len(block) > max_output_bytes
                ):
                    raise BackupBundleError("capture_bundle_too_large")
                writer.write(block)
                written += len(block)

            write_bounded(MAGIC)
            write_bounded(struct.pack(">I", len(header_bytes)))
            write_bounded(header_bytes)
            while True:
                _check_abort(abort_signal)
                block = reader.read(chunk_size)
                if not block:
                    break
                write_bounded(encryptor.update(block))
            write_bounded(encryptor.finalize())
            write_bounded(encryptor.tag)
            writer.flush()
            os.fsync(writer.fileno())
        _check_abort(abort_signal)
        _publish_file_no_replace(temporary, final_bundle)
        try:
            _check_abort(abort_signal)
        except BackupBundleError:
            _remove_file(final_bundle)
            raise
    except BackupBundleError:
        raise
    except Exception as exc:
        raise BackupBundleError("bundle_invalid") from exc
    finally:
        _remove_file(temporary)


def _decrypt_and_validate(
    workspace: BackupWorkspace,
    bundle: Path,
    recipient_private_key: X25519PrivateKey,
    operation_root: Path,
    *,
    chunk_size: int,
) -> dict[str, Any]:
    archive_path = operation_root / "payload.tar"
    restore_root = operation_root / "restored"
    header, header_bytes, ciphertext_offset, ciphertext_size = _read_header(bundle)
    expected_fingerprint = _public_key_fingerprint(
        recipient_private_key.public_key()
    )
    if expected_fingerprint != header["recipient_key_fingerprint"]:
        raise BackupBundleError("key_invalid")
    try:
        ephemeral_public = X25519PublicKey.from_public_bytes(
            _unb64(header["ephemeral_public_key"], 32)
        )
        salt = _unb64(header["hkdf_salt"], 32)
        wrap_nonce = _unb64(header["wrap_nonce"], 12)
        payload_nonce = _unb64(header["payload_nonce"], 12)
        wrapped_key = _unb64(header["wrapped_content_key"], 48)
        header_base = dict(header)
        del header_base["wrapped_content_key"]
        kek = _derive_kek(
            recipient_private_key.exchange(ephemeral_public), salt
        )
        content_key = AESGCM(kek).decrypt(
            wrap_nonce, wrapped_key, _canonical_json_bytes(header_base)
        )
    except InvalidTag as exc:
        raise BackupBundleError("authentication_failed") from exc
    except (TypeError, ValueError) as exc:
        raise BackupBundleError("bundle_invalid") from exc
    try:
        with bundle.open("rb") as reader:
            reader.seek(ciphertext_offset + ciphertext_size)
            tag = reader.read(GCM_TAG_SIZE)
            if len(tag) != GCM_TAG_SIZE or reader.read(1):
                raise BackupBundleError("bundle_invalid")
            reader.seek(ciphertext_offset)
            decryptor = Cipher(
                algorithms.AES(content_key), modes.GCM(payload_nonce, tag)
            ).decryptor()
            decryptor.authenticate_additional_data(header_bytes)
            remaining = ciphertext_size
            with archive_path.open("xb") as writer:
                while remaining:
                    block = reader.read(min(chunk_size, remaining))
                    if not block:
                        raise BackupBundleError("bundle_invalid")
                    remaining -= len(block)
                    writer.write(decryptor.update(block))
                writer.write(decryptor.finalize())
                writer.flush()
                os.fsync(writer.fileno())
    except InvalidTag as exc:
        raise BackupBundleError("authentication_failed") from exc
    except BackupBundleError:
        raise
    except OSError as exc:
        raise BackupBundleError("bundle_invalid") from exc
    restore_root.mkdir()
    manifest = _validate_and_extract_archive(
        archive_path, restore_root, chunk_size=chunk_size
    )
    matching_fields = (
        "bundle_format_version",
        "bundle_id",
        "capture_workspace_id",
        "created_at",
        "encryption_profile",
        "recipient_key_fingerprint",
    )
    if any(manifest[field] != header[field] for field in matching_fields):
        raise BackupBundleError("manifest_invalid")
    if manifest["recipient_key_fingerprint"] != expected_fingerprint:
        raise BackupBundleError("manifest_invalid")
    return {"header": header, "manifest": manifest, "restore_root": restore_root}


def _read_header(bundle: Path) -> tuple[dict[str, Any], bytes, int, int]:
    try:
        size = bundle.stat().st_size
        with bundle.open("rb") as handle:
            if handle.read(len(MAGIC)) != MAGIC:
                raise BackupBundleError("bundle_invalid")
            raw_length = handle.read(HEADER_LENGTH_SIZE)
            if len(raw_length) != HEADER_LENGTH_SIZE:
                raise BackupBundleError("bundle_invalid")
            header_length = struct.unpack(">I", raw_length)[0]
            if not 1 <= header_length <= MAX_HEADER_BYTES:
                raise BackupBundleError("bundle_invalid")
            header_bytes = handle.read(header_length)
            if len(header_bytes) != header_length:
                raise BackupBundleError("bundle_invalid")
        header = json.loads(header_bytes.decode("utf-8"))
    except BackupBundleError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise BackupBundleError("bundle_invalid") from exc
    _validate_header(header, header_bytes)
    offset = len(MAGIC) + HEADER_LENGTH_SIZE + len(header_bytes)
    ciphertext_size = size - offset - GCM_TAG_SIZE
    if ciphertext_size <= 0:
        raise BackupBundleError("bundle_invalid")
    return header, header_bytes, offset, ciphertext_size


def _validate_header(header: Any, raw: bytes) -> None:
    expected_keys = {
        "container_version", "bundle_format_version", "bundle_id",
        "capture_workspace_id", "created_at", "encryption_profile",
        "recipient_key_fingerprint", "ephemeral_public_key", "hkdf_salt",
        "wrap_nonce", "payload_nonce", "wrapped_content_key",
    }
    if not isinstance(header, dict) or set(header) != expected_keys:
        raise BackupBundleError("bundle_invalid")
    if raw != _canonical_json_bytes(header):
        raise BackupBundleError("bundle_invalid")
    if (
        header["container_version"] != CONTAINER_VERSION
        or header["bundle_format_version"] != BUNDLE_FORMAT_VERSION
        or header["encryption_profile"] != ENCRYPTION_PROFILE
        or _BUNDLE_ID_PATTERN.fullmatch(str(header["bundle_id"])) is None
        or _WORKSPACE_ID_PATTERN.fullmatch(
            str(header["capture_workspace_id"])
        ) is None
        or not _is_utc_timestamp(header["created_at"])
        or not isinstance(header["recipient_key_fingerprint"], str)
        or _SHA256_PATTERN.fullmatch(
            header["recipient_key_fingerprint"].removeprefix("x25519-sha256:")
        ) is None
        or not header["recipient_key_fingerprint"].startswith("x25519-sha256:")
    ):
        raise BackupBundleError("bundle_invalid")
    try:
        _unb64(header["ephemeral_public_key"], 32)
        _unb64(header["hkdf_salt"], 32)
        _unb64(header["wrap_nonce"], 12)
        _unb64(header["payload_nonce"], 12)
        _unb64(header["wrapped_content_key"], 48)
    except (TypeError, ValueError) as exc:
        raise BackupBundleError("bundle_invalid") from exc


def _validate_and_extract_archive(
    archive_path: Path,
    restore_root: Path,
    *,
    chunk_size: int,
) -> dict[str, Any]:
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            members = archive.getmembers()
            if not members or members[0].name != ARCHIVE_MANIFEST_PATH:
                raise BackupBundleError("manifest_invalid")
            names: set[str] = set()
            collision_keys: set[str] = set()
            for member in members:
                _validate_archive_member(member)
                if member.name in names:
                    raise BackupBundleError("manifest_invalid")
                names.add(member.name)
                collision_key = _path_collision_key(member.name)
                if collision_key in collision_keys:
                    raise BackupBundleError("manifest_invalid")
                collision_keys.add(collision_key)
            manifest_members = [
                member for member in members
                if member.name == ARCHIVE_MANIFEST_PATH
            ]
            if len(manifest_members) != 1:
                raise BackupBundleError("manifest_invalid")
            manifest_stream = archive.extractfile(manifest_members[0])
            if manifest_stream is None:
                raise BackupBundleError("manifest_invalid")
            manifest_bytes = manifest_stream.read(MAX_MANIFEST_BYTES + 1)
            if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                raise BackupBundleError("manifest_invalid")
            manifest = _validate_manifest(manifest_bytes)
            expected_names = [ARCHIVE_MANIFEST_PATH] + [
                f"data/{entry['relative_path']}" for entry in manifest["entries"]
            ]
            if [member.name for member in members] != expected_names:
                raise BackupBundleError("manifest_invalid")
            by_path = {
                f"data/{entry['relative_path']}": entry
                for entry in manifest["entries"]
            }
            for member in members[1:]:
                entry = by_path.get(member.name)
                if entry is None or member.size != entry["size_bytes"]:
                    raise BackupBundleError("manifest_invalid")
                relative = member.name.removeprefix("data/")
                destination = restore_root / Path(*PurePosixPath(relative).parts)
                if not _is_strict_within(restore_root, destination.resolve(strict=False)):
                    raise BackupBundleError("manifest_invalid")
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise BackupBundleError("manifest_invalid")
                digest = hashlib.sha256()
                size = 0
                with destination.open("xb") as writer:
                    while True:
                        block = source.read(chunk_size)
                        if not block:
                            break
                        writer.write(block)
                        digest.update(block)
                        size += len(block)
                    writer.flush()
                    os.fsync(writer.fileno())
                if size != entry["size_bytes"] or digest.hexdigest() != entry["sha256"]:
                    raise BackupBundleError("manifest_invalid")
                if entry["entry_type"] == "sqlite_snapshot":
                    _verify_restored_sqlite(destination, entry)
            return manifest
    except BackupBundleError:
        raise
    except (OSError, tarfile.TarError, UnicodeDecodeError, ValueError) as exc:
        raise BackupBundleError("manifest_invalid") from exc


def _validate_archive_member(member: tarfile.TarInfo) -> None:
    _validate_relative_path(member.name)
    if not member.isreg():
        raise BackupBundleError("manifest_invalid")
    if member.name != ARCHIVE_MANIFEST_PATH and not member.name.startswith("data/"):
        raise BackupBundleError("manifest_invalid")


def _validate_manifest(raw: bytes) -> dict[str, Any]:
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise BackupBundleError("manifest_invalid") from exc
    if not isinstance(manifest, dict) or raw != _canonical_json_bytes(manifest):
        raise BackupBundleError("manifest_invalid")
    required = {
        "schema_version", "bundle_format_version", "bundle_id",
        "capture_workspace_id",
        "created_at", "ob_commit_sha", "remember_me_version", "capture_mode",
        "encryption_profile", "recipient_key_fingerprint", "source_identity",
        "entry_count", "total_plaintext_bytes", "sqlite_snapshot_count",
        "ordinary_file_count", "entries", "exclusions", "manifest_sha256",
    }
    if set(manifest) != required:
        raise BackupBundleError("manifest_invalid")
    digest = manifest.get("manifest_sha256")
    without_digest = dict(manifest)
    without_digest.pop("manifest_sha256", None)
    if (
        not isinstance(digest, str)
        or _SHA256_PATTERN.fullmatch(digest) is None
        or hashlib.sha256(_canonical_json_bytes(without_digest)).hexdigest() != digest
        or manifest["schema_version"] != MANIFEST_SCHEMA_VERSION
        or manifest["bundle_format_version"] != BUNDLE_FORMAT_VERSION
        or manifest["capture_mode"] != CAPTURE_MODE
        or manifest["encryption_profile"] != ENCRYPTION_PROFILE
        or manifest["remember_me_version"] != EXPECTED_REMEMBER_ME_VERSION
        or _BUNDLE_ID_PATTERN.fullmatch(str(manifest["bundle_id"])) is None
        or _WORKSPACE_ID_PATTERN.fullmatch(
            str(manifest["capture_workspace_id"])
        ) is None
        or _GIT_SHA_PATTERN.fullmatch(str(manifest["ob_commit_sha"])) is None
        or not _is_utc_timestamp(manifest["created_at"])
        or not isinstance(manifest["recipient_key_fingerprint"], str)
        or not manifest["recipient_key_fingerprint"].startswith("x25519-sha256:")
        or _SHA256_PATTERN.fullmatch(
            manifest["recipient_key_fingerprint"].removeprefix("x25519-sha256:")
        ) is None
        or not isinstance(manifest["source_identity"], str)
        or not manifest["source_identity"].startswith("path-sha256:")
        or _SHA256_PATTERN.fullmatch(
            manifest["source_identity"].removeprefix("path-sha256:")
        ) is None
        or not isinstance(manifest["entries"], list)
        or not isinstance(manifest["exclusions"], list)
    ):
        raise BackupBundleError("manifest_invalid")
    _validate_manifest_entries(manifest)
    return manifest


def _validate_manifest_entries(manifest: dict[str, Any]) -> None:
    entries = manifest["entries"]
    for field in (
        "entry_count",
        "total_plaintext_bytes",
        "sqlite_snapshot_count",
        "ordinary_file_count",
    ):
        if (
            isinstance(manifest[field], bool)
            or not isinstance(manifest[field], int)
            or manifest[field] < 0
        ):
            raise BackupBundleError("manifest_invalid")
    paths: list[str] = []
    ordinary = sqlite_count = total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise BackupBundleError("manifest_invalid")
        entry_type = entry.get("entry_type")
        required = {"relative_path", "entry_type", "category", "size_bytes", "sha256"}
        if entry_type == "sqlite_snapshot":
            required |= {"page_size", "page_count", "user_version", "schema_sha256"}
            sqlite_count += 1
        elif entry_type == "regular":
            ordinary += 1
        else:
            raise BackupBundleError("manifest_invalid")
        if set(entry) != required:
            raise BackupBundleError("manifest_invalid")
        relative = entry["relative_path"]
        _validate_relative_path(relative)
        if not isinstance(entry["category"], str) or not entry["category"]:
            raise BackupBundleError("manifest_invalid")
        if (
            isinstance(entry["size_bytes"], bool)
            or not isinstance(entry["size_bytes"], int)
            or entry["size_bytes"] < 0
            or not isinstance(entry["sha256"], str)
            or _SHA256_PATTERN.fullmatch(entry["sha256"]) is None
        ):
            raise BackupBundleError("manifest_invalid")
        if entry_type == "sqlite_snapshot":
            for field in ("page_size", "page_count", "user_version"):
                if (
                    isinstance(entry[field], bool)
                    or not isinstance(entry[field], int)
                    or entry[field] < 0
                ):
                    raise BackupBundleError("manifest_invalid")
            if _SHA256_PATTERN.fullmatch(str(entry["schema_sha256"])) is None:
                raise BackupBundleError("manifest_invalid")
        paths.append(relative)
        total += entry["size_bytes"]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise BackupBundleError("manifest_invalid")
    if len({_path_collision_key(path) for path in paths}) != len(paths):
        raise BackupBundleError("manifest_invalid")
    if (
        manifest["entry_count"] != len(entries)
        or manifest["ordinary_file_count"] != ordinary
        or manifest["sqlite_snapshot_count"] != sqlite_count
        or manifest["total_plaintext_bytes"] != total
    ):
        raise BackupBundleError("manifest_invalid")
    exclusion_paths: list[str] = []
    allowed_reasons = {
        *_EXACT_EXCLUSIONS.values(),
        *_EXCLUDED_DIRECTORY_NAMES.values(),
        *_SQLITE_SIDECAR_SUFFIXES.values(),
        "temporary_file",
        "lock_file",
        "credential_material",
    }
    for exclusion in manifest["exclusions"]:
        if (
            not isinstance(exclusion, dict)
            or set(exclusion) != {"relative_path", "reason"}
            or not isinstance(exclusion["reason"], str)
            or exclusion["reason"] not in allowed_reasons
        ):
            raise BackupBundleError("manifest_invalid")
        _validate_relative_path(exclusion["relative_path"])
        exclusion_paths.append(exclusion["relative_path"])
    if (
        exclusion_paths != sorted(exclusion_paths)
        or len(exclusion_paths) != len(set(exclusion_paths))
        or len({_path_collision_key(path) for path in exclusion_paths})
        != len(exclusion_paths)
        or set(paths).intersection(exclusion_paths)
        or {
            _path_collision_key(path) for path in paths
        }.intersection(_path_collision_key(path) for path in exclusion_paths)
    ):
        raise BackupBundleError("manifest_invalid")


def _verify_restored_sqlite(path: Path, entry: dict[str, Any]) -> None:
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise BackupBundleError("manifest_invalid")
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            schema_rows = connection.execute(
                """
                SELECT type, name, tbl_name, sql FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name, tbl_name
                """
            ).fetchall()
        finally:
            connection.close()
    except BackupBundleError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise BackupBundleError("manifest_invalid") from exc
    schema_hash = hashlib.sha256(
        _canonical_json_bytes([list(row) for row in schema_rows])
    ).hexdigest()
    if (
        page_size != entry["page_size"]
        or page_count != entry["page_count"]
        or user_version != entry["user_version"]
        or schema_hash != entry["schema_sha256"]
    ):
        raise BackupBundleError("manifest_invalid")


def _validate_prepare_root(path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or _path_contains_reparse_point(candidate):
        raise BackupBundleError("workspace_invalid")
    resolved = candidate.resolve(strict=False)
    _validate_root_location(resolved)
    return resolved


def _validate_root_location(root: Path) -> None:
    repository = Path(__file__).resolve().parent
    home = Path.home().resolve()
    if (
        root == root.parent
        or root == home
        or root == repository
        or _is_within(repository, root)
        or _is_within(root, repository)
    ):
        raise BackupBundleError("workspace_invalid")


def _validate_workspace_paths(root: Path, roots: dict[str, Path]) -> None:
    values = tuple(roots.values())
    if len(values) != len(set(values)):
        raise BackupBundleError("workspace_invalid")
    for value in values:
        if not _is_strict_within(root, value) or _contains_reparse_point(root, value):
            raise BackupBundleError("workspace_invalid")
    for index, left in enumerate(values):
        for right in values[index + 1:]:
            if _is_within(left, right) or _is_within(right, left):
                raise BackupBundleError("workspace_invalid")


def _validate_external_source(
    workspace: BackupWorkspace,
    source_root: str | Path,
    authorized_source_root: str | Path,
) -> Path:
    try:
        candidate = Path(source_root)
        authorized = Path(authorized_source_root)
        if (
            not candidate.is_absolute()
            or not authorized.is_absolute()
            or _path_contains_reparse_point(candidate)
            or _path_contains_reparse_point(authorized)
        ):
            raise BackupBundleError("workspace_invalid")
        source = candidate.resolve(strict=True)
        expected = authorized.resolve(strict=True)
        if source != expected or not source.is_dir():
            raise BackupBundleError("workspace_invalid")
        _validate_root_location(source)
        workspace_roots = (
            workspace.root,
            workspace.bundles_root,
            workspace.restored_root,
            workspace.reports_root,
            workspace.temp_root,
        )
        if any(
            _is_within(source, root) or _is_within(root, source)
            for root in workspace_roots
        ):
            raise BackupBundleError("workspace_invalid")
        return source
    except BackupBundleError:
        raise
    except (OSError, RuntimeError, TypeError) as exc:
        raise BackupBundleError("workspace_invalid") from exc


def _validate_relative_path(value: Any) -> None:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise BackupBundleError("manifest_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BackupBundleError("manifest_invalid")
    for part in path.parts:
        if unicodedata.normalize("NFC", part) != part:
            raise BackupBundleError("manifest_invalid")
        if part.endswith((" ", ".")) or any(character in part for character in '<>:"|?*'):
            raise BackupBundleError("manifest_invalid")
        stem = part.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            raise BackupBundleError("manifest_invalid")


def _path_collision_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _bundle_path(workspace: BackupWorkspace, bundle_name: str) -> Path:
    if (
        not isinstance(bundle_name, str)
        or not bundle_name.endswith(BUNDLE_SUFFIX)
        or _BUNDLE_ID_PATTERN.fullmatch(bundle_name[:-len(BUNDLE_SUFFIX)]) is None
    ):
        raise BackupBundleError("bundle_invalid")
    try:
        path = (workspace.bundles_root / bundle_name).resolve(strict=True)
    except OSError as exc:
        raise BackupBundleError("bundle_invalid") from exc
    if (
        not path.is_file()
        or path.is_symlink()
        or not _is_strict_within(workspace.bundles_root, path)
    ):
        raise BackupBundleError("bundle_invalid")
    return path


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        value = path.stat()
    except OSError as exc:
        raise BackupBundleError("source_changed") from exc
    return (int(value.st_dev), int(value.st_ino), int(value.st_size), int(value.st_mtime_ns))


def _sqlite_content_signature(
    path: Path,
    *,
    chunk_size: int,
    abort_signal: CaptureAbortSignal | None = None,
) -> tuple[Any, ...]:
    signature: list[Any] = [
        _call_abortable(
            _stable_source_file_evidence,
            path, chunk_size=chunk_size, abort_signal=abort_signal
        )
    ]
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        signature.append(
            _call_abortable(
                _stable_source_file_evidence,
                sidecar, chunk_size=chunk_size, abort_signal=abort_signal
            )
            if sidecar.exists()
            else None
        )
    return tuple(signature)


def _hash_file(
    path: Path,
    *,
    chunk_size: int,
    abort_signal: CaptureAbortSignal | None = None,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                _check_abort(abort_signal)
                block = handle.read(chunk_size)
                if not block:
                    break
                digest.update(block)
                size += len(block)
    except OSError as exc:
        raise BackupBundleError("source_unsupported") from exc
    return size, digest.hexdigest()


def _category(relative: str) -> str:
    first = PurePosixPath(relative).parts[0]
    if first in {"permanent", "dynamic", "archive", "feel", "assets"}:
        return "asset_blob" if first == "assets" else first
    if relative == ".emotion_timeline.json":
        return "emotion"
    if first in {"remember-me", ".remember-me"}:
        return "remember_me"
    if first in {"migration", "state"} or relative.endswith("migration.sqlite3"):
        return "migration_state"
    return "supporting"


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _path_identity(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve())).encode("utf-8")
    return "path-sha256:" + hashlib.sha256(normalized).hexdigest()


def _public_key_fingerprint(key: X25519PublicKey) -> str:
    return "x25519-sha256:" + hashlib.sha256(key.public_bytes_raw()).hexdigest()


def _derive_kek(shared_secret: bytes, salt: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"ombre-backup-key-wrap-v1",
    ).derive(shared_secret)


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: Any, expected_length: int) -> bytes:
    if not isinstance(value, str):
        raise ValueError("invalid")
    decoded = base64.b64decode(value, validate=True)
    if len(decoded) != expected_length or _b64(decoded) != value:
        raise ValueError("invalid")
    return decoded


def _validate_public_key(key: Any) -> None:
    if not isinstance(key, X25519PublicKey):
        raise BackupBundleError("key_invalid")


def _validate_private_key(key: Any) -> None:
    if not isinstance(key, X25519PrivateKey):
        raise BackupBundleError("key_invalid")


def _validate_chunk_size(value: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 4096 <= value <= 16 * 1024 * 1024
    ):
        raise BackupBundleError("workspace_invalid")


def _read_key_file(path: str | Path, suffix: str) -> bytes:
    candidate = Path(path)
    try:
        if candidate.suffix != suffix or candidate.is_symlink():
            raise BackupBundleError("key_invalid")
        metadata = candidate.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16 * 1024:
            raise BackupBundleError("key_invalid")
        return candidate.read_bytes()
    except BackupBundleError:
        raise
    except OSError as exc:
        raise BackupBundleError("key_invalid") from exc


def _exclusive_key_write(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError as exc:
        raise BackupBundleError("key_invalid") from exc


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        _remove_file(temporary)
        raise


def _publish_file_no_replace(temporary: Path, destination: Path) -> None:
    if temporary.parent != destination.parent:
        raise BackupBundleError("bundle_invalid")
    published = False
    try:
        os.link(temporary, destination)
        published = True
        _fsync_file(destination)
        _fsync_directory(destination.parent)
    except FileExistsError as exc:
        raise BackupBundleError("bundle_invalid") from exc
    except BackupBundleError:
        raise
    except OSError as exc:
        if published:
            try:
                destination.unlink()
            except OSError as cleanup_exc:
                raise BackupBundleError("internal_error") from cleanup_exc
        raise BackupBundleError("bundle_invalid") from exc


@contextmanager
def _exclusive_operation_lock(workspace: BackupWorkspace, operation: str):
    lock_path = workspace.temp_root / f".{operation}-operation.lock"
    try:
        handle = lock_path.open("xb")
    except FileExistsError as exc:
        raise BackupBundleError("restore_target_invalid") from exc
    except OSError as exc:
        raise BackupBundleError("internal_error") from exc
    try:
        yield
    finally:
        handle.close()
        _remove_file(lock_path)


def _publish_directory_no_replace(source: Path, destination: Path) -> None:
    if destination.exists():
        raise BackupBundleError("restore_target_invalid")
    published = False
    try:
        if source.stat().st_dev != destination.parent.stat().st_dev:
            raise BackupBundleError("restore_failed")
        if os.name == "nt":
            os.rename(source, destination)
        elif sys.platform.startswith("linux"):
            _linux_rename_no_replace(source, destination)
        elif sys.platform == "darwin":
            _darwin_rename_no_replace(source, destination)
        else:
            raise BackupBundleError("restore_failed")
        published = True
        _fsync_directory(destination.parent)
    except FileExistsError as exc:
        raise BackupBundleError("restore_target_invalid") from exc
    except BackupBundleError:
        raise
    except OSError as exc:
        if published:
            try:
                shutil.rmtree(destination)
            except OSError as cleanup_exc:
                raise BackupBundleError("internal_error") from cleanup_exc
        raise BackupBundleError("restore_failed") from exc


def _linux_rename_no_replace(source: Path, destination: Path) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = library.renameat2
    except AttributeError as exc:
        raise BackupBundleError("restore_failed") from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    ) != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, "destination exists")
        raise OSError(error, "exclusive directory publication failed")


def _darwin_rename_no_replace(source: Path, destination: Path) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    try:
        renamex_np = library.renamex_np
    except AttributeError as exc:
        raise BackupBundleError("restore_failed") from exc
    renamex_np.argtypes = (
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renamex_np.restype = ctypes.c_int
    if renamex_np(os.fsencode(source), os.fsencode(destination), 4) != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, "destination exists")
        raise OSError(error, "exclusive directory publication failed")


def _fsync_file(path: Path) -> None:
    # Windows requires a writable descriptor for FlushFileBuffers via fsync.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_rmtree(workspace: BackupWorkspace, path: Path) -> None:
    try:
        resolved = path.resolve(strict=False)
        if not _is_strict_within(workspace.temp_root, resolved):
            raise BackupBundleError("workspace_invalid")
        if path.exists():
            shutil.rmtree(path)
    except BackupBundleError:
        raise
    except OSError as exc:
        raise BackupBundleError("internal_error") from exc


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _is_within(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_strict_within(parent: Path, child: Path) -> bool:
    return child != parent and _is_within(parent, child)


def _contains_reparse_point(root: Path, candidate: Path) -> bool:
    current = root
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return True
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError:
            return True
        if current.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
            return True
    return False


def _path_contains_reparse_point(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if not current.exists():
            break
        try:
            metadata = current.lstat()
        except OSError:
            return True
        if current.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
            return True
    return False


def _timestamp(clock: Callable[[], datetime] | None = None) -> str:
    value = (clock or (lambda: datetime.now(timezone.utc)))()
    if not isinstance(value, datetime):
        raise BackupBundleError("internal_error")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _is_utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def _print_result(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline encrypted backup bundle core")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("workspace")
    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("workspace")
    capture_parser.add_argument("--recipient-public-key", required=True)
    capture_parser.add_argument("--ob-commit-sha", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("workspace")
    inspect_parser.add_argument("bundle")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("workspace")
    verify_parser.add_argument("bundle")
    verify_parser.add_argument("--private-key", required=True)
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("workspace")
    restore_parser.add_argument("bundle")
    restore_parser.add_argument("--private-key", required=True)
    restore_parser.add_argument("--restore-name")
    arguments = parser.parse_args(argv)
    try:
        if arguments.operation == "prepare":
            workspace = prepare_backup_workspace(arguments.workspace)
            payload = {"status": "success", "workspace_id": workspace.workspace_id}
        elif arguments.operation == "capture":
            payload = capture_bundle(
                arguments.workspace,
                load_public_key(arguments.recipient_public_key),
                ob_commit_sha=arguments.ob_commit_sha,
            ).to_dict()
        elif arguments.operation == "inspect":
            payload = inspect_bundle(arguments.workspace, arguments.bundle)
        elif arguments.operation == "verify":
            payload = verify_bundle(
                arguments.workspace,
                arguments.bundle,
                load_private_key(arguments.private_key),
            )
        else:
            payload = restore_bundle(
                arguments.workspace,
                arguments.bundle,
                load_private_key(arguments.private_key),
                restore_name=arguments.restore_name,
            )
    except BackupBundleError as exc:
        payload = {"status": exc.status}
    except Exception:
        payload = {"status": "internal_error"}
    _print_result(payload)
    return _EXIT_CODES.get(payload["status"], _EXIT_CODES["internal_error"])


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BackupBundleError",
    "BackupWorkspace",
    "CaptureResult",
    "capture_bundle",
    "generate_test_keypair",
    "inspect_bundle",
    "load_backup_workspace",
    "load_private_key",
    "load_public_key",
    "main",
    "prepare_backup_workspace",
    "restore_bundle",
    "verify_bundle",
    "write_test_private_key",
    "write_test_public_key",
]
