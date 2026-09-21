"""Local-only Host adapter for one trusted legacy asset import."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from typing import Any, Protocol

from maintenance_write_gate import (
    DEFAULT_WRITE_COORDINATOR,
    guarded_mutation,
)

from asset_store import AssetStoreError
from remember_me.core import (
    AssetBlobVerificationResult,
    AssetNotFoundError,
    AssetVerificationCompletion,
    AssetVerificationPage,
    AssetVerificationSnapshot,
    AssetIdConflict,
    BeginAssetVerificationRequest,
    CompleteAssetVerificationRequest,
    ImageMimeMismatch,
    ImageValidationError,
    GetAssetRequest,
    ImportAssetDisposition,
    ImportAssetRequest,
    ImportAssetTag,
    ImportMetadataValidationError,
    InvalidImportRecord,
    ListAssetVerificationPageRequest,
    RememberMeCore,
    RememberMeError,
    StoredShaMismatch,
    StoredShaOwnershipConflict,
    UnsupportedAssetKind,
    UnsupportedImageFormat,
    VerifyAssetBlobRequest,
)


_ASSET_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_SUPPORTED_MEDIA = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
}
_REQUIRED_RECORD_FIELDS = (
    "asset_id",
    "source_sha256",
    "stored_sha256",
    "stored_relpath",
    "original_filename",
    "mime_type",
    "kind",
    "decoded_bytes",
    "stored_bytes",
    "width",
    "height",
    "created_at",
    "updated_at",
    "title",
    "description",
    "tags",
)
_FIXTURE_MARKER_NAME = ".ombre-stage8g-b-fixture"
_FIXTURE_PREFIX = "ombre-stage8g-b-"
_FIXTURE_FACTORY_TOKEN = object()
_OFFLINE_FACTORY_TOKEN = object()
_REHEARSAL_MANIFEST_NAME = "rehearsal-manifest.json"
_REHEARSAL_MARKER_NAME = ".ombre-stage8h-e-rehearsal"
_TARGET_RECONCILIATION_UNSUPPORTED_CHECKS: tuple[str, ...] = ()


class LegacyAssetImportDisposition(str, Enum):
    IMPORTED = "imported"
    SKIPPED_IDEMPOTENT = "skipped_idempotent"
    DRY_RUN_VALID = "dry_run_valid"
    REJECTED = "rejected"


class LegacyAssetImportErrorCode(str, Enum):
    ASSET_ID_CONFLICT = "asset_id_conflict"
    STORED_SHA_OWNERSHIP_CONFLICT = "stored_sha_ownership_conflict"
    UNSUPPORTED_LEGACY_KIND = "unsupported_legacy_kind"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    INVALID_ASSET_ID = "invalid_asset_id"
    LEGACY_ASSET_MISSING = "legacy_asset_missing"
    LEGACY_BLOB_MISSING = "legacy_blob_missing"
    LEGACY_BLOB_UNREADABLE = "legacy_blob_unreadable"
    STORED_SHA_MISMATCH = "stored_sha_mismatch"
    MALFORMED_LEGACY_RECORD = "malformed_legacy_record"
    RM_IMPORT_VALIDATION_FAILURE = "rm_import_validation_failure"


class LegacyAssetImportAdapterError(RuntimeError):
    """Stable unexpected failure with the original exception chained."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class LegacyAssetImportRequest:
    asset_id: str
    dry_run: bool = False


@dataclass(frozen=True)
class LegacyAssetImportResult:
    asset_id: str
    disposition: LegacyAssetImportDisposition
    error_code: LegacyAssetImportErrorCode | None = None
    rm_disposition: str = ""

    def to_dict(self) -> dict[str, str]:
        payload = {
            "asset_id": self.asset_id,
            "disposition": self.disposition.value,
        }
        if self.error_code is not None:
            payload["error_code"] = self.error_code.value
        if self.rm_disposition:
            payload["rm_disposition"] = self.rm_disposition
        return payload


@dataclass(frozen=True)
class LegacyAssetTargetRecord:
    """Safe public-Core projection used by local acceptance checks."""

    asset_id: str
    source_sha256: str
    stored_sha256: str
    original_filename: str
    mime_type: str
    kind: str
    decoded_bytes: int
    stored_bytes: int
    width: int
    height: int
    created_at: str
    updated_at: str
    title: str
    description: str
    tags: tuple[str, ...]


class LegacyAssetImportFixtureContext(AbstractContextManager):
    """Factory-created local fixture capability for Stage 8G-B tests."""

    def __init__(
        self,
        *,
        _token: object,
        fixture_root: Path | None = None,
        legacy_root: Path | None = None,
        rm_root: Path | None = None,
    ) -> None:
        if _token is not _FIXTURE_FACTORY_TOKEN:
            raise LegacyAssetImportAdapterError("invalid_fixture_capability")
        self._temporary_directory = None
        if fixture_root is None:
            self._temporary_directory = tempfile.TemporaryDirectory(
                prefix=_FIXTURE_PREFIX
            )
            raw_root = Path(self._temporary_directory.name)
        else:
            raw_root = Path(fixture_root)
            raw_root.mkdir(parents=True, exist_ok=True)
        self.fixture_root = raw_root.resolve(strict=True)
        self.legacy_root = (
            Path(legacy_root) if legacy_root is not None else self.fixture_root / "legacy"
        ).resolve()
        self.rm_root = (
            Path(rm_root) if rm_root is not None else self.fixture_root / "rm"
        ).resolve()
        self._nonce = secrets.token_hex(16)
        self._legacy_store_id: int | None = None
        self._core_id: int | None = None
        self._core_target_root: Path | None = None
        self._active = True
        self.legacy_root.mkdir(exist_ok=True)
        self.rm_root.mkdir(exist_ok=True)
        (self.fixture_root / _FIXTURE_MARKER_NAME).write_text(
            self._nonce,
            encoding="utf-8",
        )
        _validate_fixture_roots(
            self.fixture_root,
            self.legacy_root,
            self.rm_root,
        )

    def bind_legacy_store(
        self,
        legacy_store: "LegacyAssetImportSource",
    ) -> "LegacyAssetImportSource":
        self._validate_active()
        legacy_root = Path(legacy_store.data_root).resolve()
        if legacy_root != self.legacy_root:
            raise LegacyAssetImportAdapterError("fixture_root_violation")
        self._legacy_store_id = id(legacy_store)
        return legacy_store

    def create_runtime(self, adapter=None):
        self._validate_active()
        if adapter is None:
            from remember_me_adapter import RememberMeAdapter

            adapter = RememberMeAdapter()
        runtime = adapter.create_runtime(self.rm_root)
        self._core_id = id(runtime.service)
        self._core_target_root = self.rm_root
        return runtime

    def bind_core(self, core: RememberMeCore) -> RememberMeCore:
        self._validate_active()
        if not callable(getattr(core, "import_asset", None)):
            raise LegacyAssetImportAdapterError("rm_import_unavailable")
        if self._core_id != id(core):
            self._core_target_root = None
        self._core_id = id(core)
        return core

    def close(self) -> None:
        self._active = False
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _validate_for_adapter(
        self,
        *,
        legacy_store: "LegacyAssetImportSource",
        core: RememberMeCore,
    ) -> None:
        self._validate_active()
        _validate_fixture_roots(
            self.fixture_root,
            self.legacy_root,
            self.rm_root,
        )
        if self._legacy_store_id != id(legacy_store):
            raise LegacyAssetImportAdapterError("fixture_root_violation")
        if self._core_id != id(core):
            raise LegacyAssetImportAdapterError("fixture_root_violation")
        legacy_root = Path(legacy_store.data_root).resolve()
        if legacy_root != self.legacy_root:
            raise LegacyAssetImportAdapterError("fixture_root_violation")

    def _validate_active(self) -> None:
        if not self._active:
            raise LegacyAssetImportAdapterError("fixture_root_violation")
        marker = self.fixture_root / _FIXTURE_MARKER_NAME
        try:
            marker_nonce = marker.read_text(encoding="utf-8")
        except OSError as exc:
            raise LegacyAssetImportAdapterError(
                "fixture_root_violation"
            ) from exc
        if marker_nonce != self._nonce:
            raise LegacyAssetImportAdapterError("fixture_root_violation")


def create_legacy_asset_import_fixture_context(
    fixture_root: Path | None = None,
    *,
    legacy_root: Path | None = None,
    rm_root: Path | None = None,
) -> LegacyAssetImportFixtureContext:
    return LegacyAssetImportFixtureContext(
        _token=_FIXTURE_FACTORY_TOKEN,
        fixture_root=fixture_root,
        legacy_root=legacy_root,
        rm_root=rm_root,
    )


class LegacyAssetImportOfflineContext(AbstractContextManager):
    """Factory-created capability for one validated offline rehearsal."""

    def __init__(
        self,
        *,
        _token: object,
        workspace_root: Path,
        workspace_id: str,
        nonce: str,
        legacy_root: Path,
        rm_root: Path,
    ) -> None:
        if _token is not _OFFLINE_FACTORY_TOKEN:
            raise LegacyAssetImportAdapterError("invalid_offline_capability")
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        self.workspace_id = workspace_id
        self.legacy_root = Path(legacy_root).resolve(strict=True)
        self.rm_root = Path(rm_root).resolve(strict=True)
        self._nonce = nonce
        self._legacy_store_id: int | None = None
        self._core_id: int | None = None
        self._core_target_root: Path | None = None
        self._active = True
        self._validate_active()

    def bind_legacy_store(
        self,
        legacy_store: "LegacyAssetImportSource",
    ) -> "LegacyAssetImportSource":
        self._validate_active()
        if Path(legacy_store.data_root).resolve() != self.legacy_root:
            raise LegacyAssetImportAdapterError("offline_root_violation")
        self._legacy_store_id = id(legacy_store)
        return legacy_store

    def create_runtime(self, adapter=None):
        self._validate_active()
        if adapter is None:
            from remember_me_adapter import RememberMeAdapter

            adapter = RememberMeAdapter()
        runtime = adapter.create_runtime(self.rm_root)
        self._core_id = id(runtime.service)
        self._core_target_root = self.rm_root
        return runtime

    def close(self) -> None:
        self._active = False
        self._legacy_store_id = None
        self._core_id = None
        self._core_target_root = None

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _validate_for_adapter(
        self,
        *,
        legacy_store: "LegacyAssetImportSource",
        core: RememberMeCore,
    ) -> None:
        self._validate_active()
        if (
            self._legacy_store_id != id(legacy_store)
            or self._core_id != id(core)
            or Path(legacy_store.data_root).resolve() != self.legacy_root
            or self._core_target_root != self.rm_root
        ):
            raise LegacyAssetImportAdapterError("offline_root_violation")

    def _validate_active(self) -> None:
        if not self._active:
            raise LegacyAssetImportAdapterError("offline_capability_expired")
        try:
            manifest = json.loads(
                (self.workspace_root / _REHEARSAL_MANIFEST_NAME).read_text(
                    encoding="utf-8"
                )
            )
            marker = json.loads(
                (self.workspace_root / _REHEARSAL_MARKER_NAME).read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError, TypeError) as exc:
            raise LegacyAssetImportAdapterError(
                "offline_root_violation"
            ) from exc
        if (
            manifest.get("workspace_id") != self.workspace_id
            or manifest.get("nonce") != self._nonce
            or manifest.get("paths")
            != {
                "source": "legacy",
                "target": "remember-me",
                "state": "state",
                "reports": "reports",
            }
            or marker != {
                "workspace_id": self.workspace_id,
                "nonce": self._nonce,
            }
        ):
            raise LegacyAssetImportAdapterError("offline_root_violation")
        try:
            if (
                (self.workspace_root / "legacy").resolve(strict=True)
                != self.legacy_root
                or (
                    self.workspace_root / "remember-me"
                ).resolve(strict=True)
                != self.rm_root
            ):
                raise LegacyAssetImportAdapterError(
                    "offline_root_violation"
                )
        except (OSError, RuntimeError) as exc:
            raise LegacyAssetImportAdapterError(
                "offline_root_violation"
            ) from exc


def _create_legacy_asset_import_offline_context(
    *,
    workspace_root: Path,
    workspace_id: str,
    nonce: str,
    legacy_root: Path,
    rm_root: Path,
) -> LegacyAssetImportOfflineContext:
    return LegacyAssetImportOfflineContext(
        _token=_OFFLINE_FACTORY_TOKEN,
        workspace_root=workspace_root,
        workspace_id=workspace_id,
        nonce=nonce,
        legacy_root=legacy_root,
        rm_root=rm_root,
    )


class LegacyAssetImportSource(Protocol):
    data_root: Path

    def get_import_record(self, asset_id: str) -> dict | None:
        ...

    def resolve_file(self, asset_id: str) -> tuple[dict, Path] | None:
        ...


class LegacyAssetImportAdapter:
    """Import one legacy image through the public Remember-Me Core contract."""

    def __init__(
        self,
        *,
        legacy_store: LegacyAssetImportSource,
        core: RememberMeCore,
        fixture_context: LegacyAssetImportFixtureContext | None = None,
        offline_context: LegacyAssetImportOfflineContext | None = None,
        fixture_root: Path | None = None,
    ) -> None:
        contexts = tuple(
            context
            for context in (fixture_context, offline_context)
            if context is not None
        )
        if (
            fixture_root is not None
            or len(contexts) != 1
            or not isinstance(
                contexts[0],
                (
                    LegacyAssetImportFixtureContext,
                    LegacyAssetImportOfflineContext,
                ),
            )
        ):
            raise LegacyAssetImportAdapterError("invalid_fixture_capability")
        capability_context = contexts[0]
        capability_context._validate_for_adapter(
            legacy_store=legacy_store,
            core=core,
        )
        root = (
            capability_context.fixture_root
            if isinstance(
                capability_context,
                LegacyAssetImportFixtureContext,
            )
            else capability_context.workspace_root
        )
        legacy_root = Path(legacy_store.data_root).resolve()
        if not callable(getattr(core, "import_asset", None)):
            raise LegacyAssetImportAdapterError("rm_import_unavailable")
        self._legacy_store = legacy_store
        self._core = core
        self._fixture_root = root
        self._legacy_root = legacy_root
        self._fixture_context = capability_context
        self.write_coordinator = getattr(
            legacy_store,
            "write_coordinator",
            DEFAULT_WRITE_COORDINATOR,
        )

    def is_bound_to_legacy_store(
        self,
        legacy_store: LegacyAssetImportSource,
    ) -> bool:
        """Return whether this capability is bound to the exact source object."""
        return self._legacy_store is legacy_store

    def is_bound_to_target_root(self, target_root: Path) -> bool:
        """Return whether this capability's fixture owns the target root."""
        self._fixture_context._validate_for_adapter(
            legacy_store=self._legacy_store,
            core=self._core,
        )
        try:
            return (
                self._fixture_context._core_target_root
                == self._fixture_context.rm_root
                and Path(target_root).resolve()
                == self._fixture_context._core_target_root
            )
        except (OSError, RuntimeError, TypeError):
            return False

    def get_target_record(
        self,
        asset_id: str,
    ) -> LegacyAssetTargetRecord | None:
        """Read one bound fixture target through the public Core contract."""
        self._fixture_context._validate_for_adapter(
            legacy_store=self._legacy_store,
            core=self._core,
        )
        if (
            not isinstance(asset_id, str)
            or _ASSET_ID_PATTERN.fullmatch(asset_id) is None
        ):
            raise LegacyAssetImportAdapterError("invalid_asset_id")
        try:
            record = self._core.get_asset(GetAssetRequest(asset_id=asset_id))
        except AssetNotFoundError:
            return None
        except RememberMeError as exc:
            raise LegacyAssetImportAdapterError(
                "rm_target_read_unavailable"
            ) from exc
        except Exception as exc:
            raise LegacyAssetImportAdapterError(
                "rm_target_read_unavailable"
            ) from exc
        try:
            projected = LegacyAssetTargetRecord(
                asset_id=record.asset_id,
                source_sha256=record.source_sha256,
                stored_sha256=record.stored_sha256,
                original_filename=record.original_filename,
                mime_type=record.mime_type,
                kind=record.kind,
                decoded_bytes=record.decoded_bytes,
                stored_bytes=record.stored_bytes,
                width=record.width,
                height=record.height,
                created_at=record.created_at,
                updated_at=record.updated_at,
                title=record.title,
                description=record.description,
                tags=record.tags,
            )
        except (AttributeError, TypeError) as exc:
            raise LegacyAssetImportAdapterError(
                "rm_target_record_invalid"
            ) from exc
        if not _valid_target_record(projected, asset_id):
            raise LegacyAssetImportAdapterError(
                "rm_target_record_invalid"
            )
        return projected

    def target_reconciliation_unsupported_checks(self) -> tuple[str, ...]:
        """Declare additional gaps; this is not verification evidence."""
        self._fixture_context._validate_for_adapter(
            legacy_store=self._legacy_store,
            core=self._core,
        )
        return _TARGET_RECONCILIATION_UNSUPPORTED_CHECKS

    def begin_target_verification(self) -> AssetVerificationSnapshot:
        return self._call_target_verification(
            "begin_asset_verification",
            BeginAssetVerificationRequest(kind="image"),
        )

    def list_target_verification_page(
        self,
        *,
        snapshot_id: str,
        cursor: str,
        limit: int,
    ) -> AssetVerificationPage:
        return self._call_target_verification(
            "list_asset_verification_page",
            ListAssetVerificationPageRequest(
                snapshot_id=snapshot_id,
                cursor=cursor,
                limit=limit,
            ),
        )

    def verify_target_blob(
        self,
        *,
        snapshot_id: str,
        asset_id: str,
        expected_sha256: str,
        expected_size: int,
        expected_bytes: bytes,
    ) -> AssetBlobVerificationResult:
        return self._call_target_verification(
            "verify_asset_blob",
            VerifyAssetBlobRequest(
                snapshot_id=snapshot_id,
                asset_id=asset_id,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
                expected_bytes=expected_bytes,
            ),
        )

    def complete_target_verification(
        self,
        *,
        snapshot_id: str,
    ) -> AssetVerificationCompletion:
        return self._call_target_verification(
            "complete_asset_verification",
            CompleteAssetVerificationRequest(snapshot_id=snapshot_id),
        )

    def get_legacy_verification_bytes(self, asset_id: str) -> bytes:
        """Read one frozen legacy blob without exposing its locator."""
        self._fixture_context._validate_for_adapter(
            legacy_store=self._legacy_store,
            core=self._core,
        )
        if (
            not isinstance(asset_id, str)
            or _ASSET_ID_PATTERN.fullmatch(asset_id) is None
        ):
            raise LegacyAssetImportAdapterError("invalid_asset_id")
        try:
            resolved = self._legacy_store.resolve_file(asset_id)
        except Exception as exc:
            raise LegacyAssetImportAdapterError(
                "legacy_blob_unavailable"
            ) from exc
        if (
            not isinstance(resolved, tuple)
            or len(resolved) != 2
            or not isinstance(resolved[1], Path)
        ):
            raise LegacyAssetImportAdapterError("legacy_blob_unavailable")
        _, blob_path = resolved
        try:
            content = blob_path.read_bytes()
        except Exception as exc:
            raise LegacyAssetImportAdapterError(
                "legacy_blob_unavailable"
            ) from exc
        if type(content) is not bytes:
            raise LegacyAssetImportAdapterError("legacy_blob_unavailable")
        return content

    def _call_target_verification(self, method_name: str, request):
        self._fixture_context._validate_for_adapter(
            legacy_store=self._legacy_store,
            core=self._core,
        )
        method = getattr(self._core, method_name, None)
        if not callable(method):
            raise LegacyAssetImportAdapterError(
                "rm_target_verification_unavailable"
            )
        try:
            return method(request)
        except RememberMeError as exc:
            code = getattr(exc, "code", "")
            if not isinstance(code, str) or re.fullmatch(
                r"[a-z0-9_]{1,96}", code
            ) is None:
                code = "verification_error"
            raise LegacyAssetImportAdapterError(
                "rm_target_{}".format(code)
            ) from exc
        except Exception as exc:
            raise LegacyAssetImportAdapterError(
                "rm_target_verification_internal_error"
            ) from exc

    @guarded_mutation("remember_me_import")
    def import_asset(
        self,
        request: LegacyAssetImportRequest,
    ) -> LegacyAssetImportResult:
        self._fixture_context._validate_for_adapter(
            legacy_store=self._legacy_store,
            core=self._core,
        )
        if not isinstance(request, LegacyAssetImportRequest):
            return self._reject(
                "",
                LegacyAssetImportErrorCode.MALFORMED_LEGACY_RECORD,
            )
        asset_id = request.asset_id
        if (
            not isinstance(asset_id, str)
            or _ASSET_ID_PATTERN.fullmatch(asset_id) is None
        ):
            return self._reject(
                asset_id if isinstance(asset_id, str) else "",
                LegacyAssetImportErrorCode.INVALID_ASSET_ID,
            )
        if not isinstance(request.dry_run, bool):
            return self._reject(
                asset_id,
                LegacyAssetImportErrorCode.MALFORMED_LEGACY_RECORD,
            )

        try:
            record = self._legacy_store.get_import_record(asset_id)
        except Exception as exc:
            raise LegacyAssetImportAdapterError(
                "legacy_record_unavailable"
            ) from exc
        if record is None:
            return self._reject(
                asset_id,
                LegacyAssetImportErrorCode.LEGACY_ASSET_MISSING,
            )
        if not self._valid_record_shape(record, asset_id):
            return self._reject(
                asset_id,
                LegacyAssetImportErrorCode.MALFORMED_LEGACY_RECORD,
            )
        if record["kind"] != "image":
            return self._reject(
                asset_id,
                LegacyAssetImportErrorCode.UNSUPPORTED_LEGACY_KIND,
            )
        expected_extension = _SUPPORTED_MEDIA.get(record["mime_type"])
        if expected_extension is None:
            return self._reject(
                asset_id,
                LegacyAssetImportErrorCode.UNSUPPORTED_MEDIA_TYPE,
            )

        resolved_result = self._resolve_legacy_blob(asset_id)
        if isinstance(resolved_result, LegacyAssetImportResult):
            return resolved_result
        resolved_record, blob_path = resolved_result
        if not self._matching_resolved_record(record, resolved_record):
            return self._reject(
                asset_id,
                LegacyAssetImportErrorCode.MALFORMED_LEGACY_RECORD,
            )
        if blob_path.suffix.lower() != expected_extension:
            return self._reject(
                asset_id,
                LegacyAssetImportErrorCode.UNSUPPORTED_MEDIA_TYPE,
            )
        try:
            cleaned_bytes = blob_path.read_bytes()
        except FileNotFoundError:
            return self._reject(
                asset_id,
                LegacyAssetImportErrorCode.LEGACY_BLOB_MISSING,
            )
        except OSError:
            return self._reject(
                asset_id,
                LegacyAssetImportErrorCode.LEGACY_BLOB_UNREADABLE,
            )

        try:
            rm_request = ImportAssetRequest(
                asset_id=asset_id,
                source_sha256=record["source_sha256"],
                stored_sha256=record["stored_sha256"],
                cleaned_bytes=cleaned_bytes,
                original_filename=record["original_filename"],
                mime_type=record["mime_type"],
                kind=record["kind"],
                decoded_bytes=record["decoded_bytes"],
                stored_bytes=record["stored_bytes"],
                width=record["width"],
                height=record["height"],
                created_at=record["created_at"],
                updated_at=record["updated_at"],
                title=record["title"],
                description=record["description"],
                tags=tuple(
                    ImportAssetTag(
                        value=tag["value"],
                        created_at=tag["created_at"],
                    )
                    for tag in record["tags"]
                ),
                dry_run=request.dry_run,
            )
        except (KeyError, TypeError):
            return self._reject(
                asset_id,
                LegacyAssetImportErrorCode.MALFORMED_LEGACY_RECORD,
            )

        try:
            imported = self._core.import_asset(rm_request)
        except AssetIdConflict:
            return self._reject(
                asset_id,
                LegacyAssetImportErrorCode.ASSET_ID_CONFLICT,
            )
        except StoredShaOwnershipConflict:
            return self._reject(
                asset_id,
                LegacyAssetImportErrorCode.STORED_SHA_OWNERSHIP_CONFLICT,
            )
        except StoredShaMismatch:
            return self._reject(
                asset_id,
                LegacyAssetImportErrorCode.STORED_SHA_MISMATCH,
            )
        except UnsupportedAssetKind:
            return self._reject(
                asset_id,
                LegacyAssetImportErrorCode.UNSUPPORTED_LEGACY_KIND,
            )
        except (
            UnsupportedImageFormat,
            ImageMimeMismatch,
            ImageValidationError,
            ImportMetadataValidationError,
            InvalidImportRecord,
        ):
            return self._reject(
                asset_id,
                LegacyAssetImportErrorCode.RM_IMPORT_VALIDATION_FAILURE,
            )
        except RememberMeError as exc:
            raise LegacyAssetImportAdapterError("rm_import_failure") from exc
        except Exception as exc:
            raise LegacyAssetImportAdapterError("rm_import_failure") from exc

        disposition = imported.disposition
        if disposition is ImportAssetDisposition.IMPORTED:
            host_disposition = LegacyAssetImportDisposition.IMPORTED
        elif disposition is ImportAssetDisposition.SKIPPED_IDEMPOTENT:
            host_disposition = LegacyAssetImportDisposition.SKIPPED_IDEMPOTENT
        elif disposition in {
            ImportAssetDisposition.WOULD_IMPORT,
            ImportAssetDisposition.WOULD_SKIP_IDEMPOTENT,
        }:
            host_disposition = LegacyAssetImportDisposition.DRY_RUN_VALID
        else:
            raise LegacyAssetImportAdapterError("rm_import_failure")
        return LegacyAssetImportResult(
            asset_id=asset_id,
            disposition=host_disposition,
            rm_disposition=disposition.value,
        )

    def _resolve_legacy_blob(
        self,
        asset_id: str,
    ) -> tuple[dict, Path] | LegacyAssetImportResult:
        try:
            resolved = self._legacy_store.resolve_file(asset_id)
        except AssetStoreError:
            return self._reject(
                asset_id,
                LegacyAssetImportErrorCode.MALFORMED_LEGACY_RECORD,
            )
        except Exception as exc:
            raise LegacyAssetImportAdapterError(
                "legacy_blob_unavailable"
            ) from exc
        if resolved is None:
            return self._reject(
                asset_id,
                LegacyAssetImportErrorCode.LEGACY_BLOB_MISSING,
            )
        record, path = resolved
        try:
            candidate = Path(path).resolve(strict=True)
        except (OSError, RuntimeError):
            return self._reject(
                asset_id,
                LegacyAssetImportErrorCode.LEGACY_BLOB_MISSING,
            )
        if (
            not _is_within(self._fixture_root, candidate)
            or not _is_within(self._legacy_root / "assets", candidate)
        ):
            return self._reject(
                asset_id,
                LegacyAssetImportErrorCode.MALFORMED_LEGACY_RECORD,
            )
        return record, candidate

    @staticmethod
    def _valid_record_shape(record: Any, asset_id: str) -> bool:
        if not isinstance(record, dict):
            return False
        if any(field not in record for field in _REQUIRED_RECORD_FIELDS):
            return False
        if (
            record["asset_id"] != asset_id
            or not isinstance(record["tags"], list)
            or any(
                not isinstance(record[field], str)
                for field in (
                    "asset_id",
                    "source_sha256",
                    "stored_sha256",
                    "stored_relpath",
                    "original_filename",
                    "mime_type",
                    "kind",
                    "created_at",
                    "updated_at",
                    "title",
                    "description",
                )
            )
            or any(
                isinstance(record[field], bool)
                or not isinstance(record[field], int)
                for field in (
                    "decoded_bytes",
                    "stored_bytes",
                    "width",
                    "height",
                )
            )
        ):
            return False
        return all(
            isinstance(tag, dict)
            and set(tag) == {"value", "created_at"}
            and isinstance(tag["value"], str)
            and isinstance(tag["created_at"], str)
            for tag in record["tags"]
        )

    @staticmethod
    def _matching_resolved_record(expected: dict, resolved: Any) -> bool:
        if not isinstance(resolved, dict):
            return False
        for field in _REQUIRED_RECORD_FIELDS:
            if field in {"tags", "stored_relpath"}:
                continue
            if resolved.get(field) != expected[field]:
                return False
        return resolved.get("stored_relpath") == expected["stored_relpath"]

    @staticmethod
    def _reject(
        asset_id: str,
        code: LegacyAssetImportErrorCode,
    ) -> LegacyAssetImportResult:
        return LegacyAssetImportResult(
            asset_id=asset_id,
            disposition=LegacyAssetImportDisposition.REJECTED,
            error_code=code,
        )


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _is_strict_within(root: Path, candidate: Path) -> bool:
    root = root.resolve()
    candidate = candidate.resolve()
    return candidate != root and _is_within(root, candidate)


def _valid_target_record(
    record: LegacyAssetTargetRecord,
    asset_id: str,
) -> bool:
    text_fields = (
        record.original_filename,
        record.mime_type,
        record.kind,
        record.title,
        record.description,
    )
    integer_fields = (
        record.decoded_bytes,
        record.stored_bytes,
        record.width,
        record.height,
    )
    return (
        isinstance(record.asset_id, str)
        and record.asset_id == asset_id
        and _ASSET_ID_PATTERN.fullmatch(record.asset_id) is not None
        and isinstance(record.source_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", record.source_sha256) is not None
        and isinstance(record.stored_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", record.stored_sha256) is not None
        and all(isinstance(value, str) for value in text_fields)
        and all(type(value) is int and value >= 0 for value in integer_fields)
        and _valid_public_timestamp(record.created_at)
        and _valid_public_timestamp(record.updated_at)
        and isinstance(record.tags, tuple)
        and all(isinstance(value, str) for value in record.tags)
    )


def _valid_public_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


def _is_filesystem_root(path: Path) -> bool:
    resolved = path.resolve()
    return resolved == resolved.parent


def _forbidden_host_roots() -> set[Path]:
    roots = {Path(__file__).resolve().parent}
    env_root = os.environ.get("OMBRE_BUCKETS_DIR", "").strip()
    if env_root:
        roots.add(Path(env_root).expanduser())
    raw_config = os.environ.get("OMBRE_CONFIG_PATH", "").strip()
    config_path = (
        Path(raw_config).expanduser()
        if raw_config
        else Path(__file__).resolve().parent / "config.yaml"
    )
    try:
        import yaml

        if config_path.exists():
            parsed = yaml.safe_load(
                config_path.read_text(encoding="utf-8")
            ) or {}
            if isinstance(parsed, dict) and parsed.get("buckets_dir"):
                roots.add(Path(str(parsed["buckets_dir"])).expanduser())
    except Exception:
        pass
    roots.add(Path(__file__).resolve().parent / "buckets")
    resolved_roots = set()
    for root in roots:
        try:
            resolved_roots.add(root.resolve())
        except (OSError, RuntimeError):
            continue
    return resolved_roots


def _validate_fixture_roots(
    fixture_root: Path,
    legacy_root: Path,
    rm_root: Path,
) -> None:
    try:
        root = Path(fixture_root).resolve(strict=True)
        legacy = Path(legacy_root).resolve(strict=False)
        rm = Path(rm_root).resolve(strict=False)
        system_temp = Path(tempfile.gettempdir()).resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise LegacyAssetImportAdapterError("fixture_root_violation") from exc

    if _is_filesystem_root(root):
        raise LegacyAssetImportAdapterError("fixture_root_violation")
    if not _is_strict_within(system_temp, root):
        raise LegacyAssetImportAdapterError("fixture_root_violation")
    for forbidden in _forbidden_host_roots():
        if root == forbidden or _is_within(root, forbidden):
            raise LegacyAssetImportAdapterError("fixture_root_violation")
    marker = root / _FIXTURE_MARKER_NAME
    try:
        marker_text = marker.read_text(encoding="utf-8")
    except OSError as exc:
        raise LegacyAssetImportAdapterError("fixture_root_violation") from exc
    if not re.fullmatch(r"[0-9a-f]{32}", marker_text):
        raise LegacyAssetImportAdapterError("fixture_root_violation")
    if not _is_strict_within(root, legacy) or not _is_strict_within(root, rm):
        raise LegacyAssetImportAdapterError("fixture_root_violation")
    if legacy == rm:
        raise LegacyAssetImportAdapterError("fixture_root_violation")


__all__ = [
    "LegacyAssetImportFixtureContext",
    "LegacyAssetImportOfflineContext",
    "LegacyAssetImportAdapter",
    "LegacyAssetImportAdapterError",
    "LegacyAssetImportDisposition",
    "LegacyAssetImportErrorCode",
    "LegacyAssetImportRequest",
    "LegacyAssetImportResult",
    "LegacyAssetTargetRecord",
    "create_legacy_asset_import_fixture_context",
]
