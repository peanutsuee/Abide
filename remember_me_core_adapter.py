"""Ombre-Brain compatibility boundary for the public Remember-Me Core."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from typing import Any

from remember_me.core import (
    AssetFileUnavailable,
    AssetNotFoundError,
    AssetUnavailable,
    DeleteAssetRequest,
    GetAssetRequest,
    ImagePixelLimitExceeded,
    ImageValidationError,
    IngestImageRequest,
    InvalidMetadata,
    ReindexEmbeddingsRequest,
    RememberMeError,
    ResolveAssetRequest,
    SearchAssetsRequest,
    StorageConsistencyError,
    UpdateMetadataRequest,
    UploadSizeMismatch,
    UploadTooLarge,
)
from maintenance_write_gate import (
    DEFAULT_WRITE_COORDINATOR,
    guarded_async_mutation,
    guarded_mutation,
)

_ASSET_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_OB_METADATA_ERROR_CODES = {
    "description_too_long",
    "invalid_description",
    "invalid_tag",
    "invalid_tags",
    "invalid_title",
    "tag_too_long",
    "title_too_long",
    "too_many_tags",
}
_OB_SEARCH_ERROR_CODES = {
    "invalid_query",
    "invalid_limit",
    "invalid_offset",
    "invalid_kind",
    "invalid_mime_type",
    "invalid_created_from",
    "invalid_created_to",
    "invalid_date_range",
    "invalid_tags",
    "invalid_tag",
    "tag_too_long",
    "too_many_tags",
}


class RememberMeCoreAdapterError(RuntimeError):
    """Stable, path-free error returned by the OB Core compatibility layer."""

    def __init__(self, code: str, *, ob_code: str | None = None):
        self.code = code
        self.ob_code = ob_code
        super().__init__(code)


@dataclass(frozen=True)
class RememberMeReindexResult:
    scanned: int
    indexed: int
    skipped: int
    failed: int
    last_error: str = ""
    last_error_details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        counters = (
            self.scanned,
            self.indexed,
            self.skipped,
            self.failed,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in counters
        ) or self.scanned != self.indexed + self.skipped + self.failed:
            raise ValueError("invalid_reindex_counters")
        if not isinstance(self.last_error, str):
            raise ValueError("invalid_reindex_error")
        if self.last_error_details is not None and not isinstance(
            self.last_error_details, dict
        ):
            raise ValueError("invalid_reindex_error_details")


def _bounded_reindex_failure(provider: Any) -> tuple[str, dict[str, Any]]:
    raw_code = str(getattr(provider, "last_error", "") or "")
    code = raw_code if re.fullmatch(r"[a-z0-9_]{1,80}", raw_code) else ""
    raw_details = getattr(provider, "last_error_details", {})
    if not isinstance(raw_details, dict):
        return code, {}

    details: dict[str, Any] = {}
    request_url = raw_details.get("request_url")
    if (
        isinstance(request_url, str)
        and len(request_url) <= 500
        and re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]+", request_url)
    ):
        details["request_url"] = request_url
    status_code = raw_details.get("status_code")
    if isinstance(status_code, int) and not isinstance(status_code, bool) and 100 <= status_code <= 599:
        details["status_code"] = status_code
    response_body = raw_details.get("response_body")
    if response_body in ("", "[redacted]"):
        details["response_body"] = response_body
    error_type = raw_details.get("error_type")
    if isinstance(error_type, str) and re.fullmatch(r"[A-Za-z0-9_]{1,80}", error_type):
        details["error_type"] = error_type
    return code, details


class RememberMeCoreAdapter:
    """Translate public Remember-Me Core operations into OB-safe structures."""

    def __init__(
        self,
        runtime: Any,
        *,
        host_adapter: Any = None,
        write_coordinator=None,
    ) -> None:
        if runtime is None or not all(
            hasattr(runtime, name)
            for name in ("service", "repository", "blob_store")
        ):
            raise RememberMeCoreAdapterError("runtime_unavailable")
        self._runtime = runtime
        self._host_adapter = host_adapter
        self.write_coordinator = write_coordinator or DEFAULT_WRITE_COORDINATOR

    @classmethod
    def from_host_adapter(
        cls,
        host_adapter: Any,
        data_root: Path,
        *,
        vector_provider: Any = None,
    ):
        """Explicitly create one RM runtime through the Stage 8B host owner."""
        if not isinstance(data_root, Path):
            raise RememberMeCoreAdapterError("invalid_data_root")
        try:
            runtime = host_adapter.create_runtime(
                data_root,
                vector_provider=vector_provider,
            )
        except Exception as exc:
            code = str(exc)
            if code == "remember_me_data_root_already_owned":
                raise RememberMeCoreAdapterError(
                    "runtime_already_owned"
                ) from exc
            raise RememberMeCoreAdapterError("runtime_unavailable") from exc
        return cls(runtime, host_adapter=host_adapter)

    @guarded_mutation("remember_me_ingest")
    def ingest_image(
        self,
        content: bytes,
        expected_bytes: int,
        filename: str,
        mime_type: str = "application/octet-stream",
        *,
        title: str = "",
        description: str = "",
        tags: list[str] | tuple[str, ...] = (),
    ) -> dict:
        try:
            clean_tags = self._tag_tuple(tags)
            result = self._runtime.service.ingest_image(
                IngestImageRequest(
                    content=content,
                    expected_bytes=expected_bytes,
                    filename=filename,
                    mime_type=mime_type,
                    title=title,
                    description=description,
                    tags=clean_tags,
                )
            )
            asset = self._asset_dict(result.asset)
            asset["deduplicated"] = bool(result.deduplicated)
            return asset
        except Exception as exc:
            self._raise_mapped(exc)

    @guarded_mutation("remember_me_ingest_ob")
    def ingest_ob_public_metadata(
        self,
        content: bytes,
        expected_bytes: int,
        filename: str,
        mime_type: str = "application/octet-stream",
        *,
        title: str = "",
        description: str = "",
        tags: list[str] | tuple[str, ...] = (),
    ) -> dict:
        try:
            clean_tags = self._tag_tuple(tags)
            result = self._runtime.service.ingest_image(
                IngestImageRequest(
                    content=content,
                    expected_bytes=expected_bytes,
                    filename=filename,
                    mime_type=mime_type,
                    title=title,
                    description=description,
                    tags=clean_tags,
                )
            )
            asset = self._ob_public_metadata(result.asset)
            asset["deduplicated"] = bool(result.deduplicated)
            return asset
        except Exception as exc:
            self._raise_mapped(exc)

    def get(self, asset_id: str) -> dict | None:
        if not self._valid_asset_id(asset_id):
            return None
        try:
            asset = self._runtime.service.get_asset(
                GetAssetRequest(asset_id.strip())
            )
            return self._asset_dict(asset)
        except AssetNotFoundError:
            return None
        except Exception as exc:
            self._raise_mapped(exc)

    def get_ob_public_metadata(self, asset_id: str) -> dict | None:
        """Return only fields already public in the current OB MCP contract."""
        if not self._valid_asset_id(asset_id):
            return None
        try:
            asset = self._runtime.service.get_asset(
                GetAssetRequest(asset_id.strip())
            )
            return self._ob_public_metadata(asset)
        except AssetNotFoundError:
            return None
        except RememberMeError as exc:
            self._raise_mapped(exc)

    def update_metadata(
        self,
        asset_id: str,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        asset = self._update_metadata_asset(
            asset_id,
            title=title,
            description=description,
            tags=tags,
        )
        return self._asset_dict(asset)

    def update_ob_public_metadata(
        self,
        asset_id: str,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        """Mutate once and return the current OB public metadata shape."""
        asset = self._update_metadata_asset(
            asset_id,
            title=title,
            description=description,
            tags=tags,
        )
        return self._ob_public_metadata(asset)

    @guarded_mutation("remember_me_metadata_update")
    def _update_metadata_asset(
        self,
        asset_id: str,
        *,
        title: str | None,
        description: str | None,
        tags: list[str] | tuple[str, ...] | None,
    ) -> Any:
        self._require_asset_id(asset_id)
        try:
            clean_tags = self._validate_metadata_update(
                title=title,
                description=description,
                tags=tags,
            )
            return self._runtime.service.update_metadata(
                UpdateMetadataRequest(
                    asset_id=asset_id.strip(),
                    title=title,
                    description=description,
                    tags=clean_tags,
                )
            )
        except Exception as exc:
            self._raise_mapped(exc)

    async def search(
        self,
        query: str = "",
        tags: list[str] | tuple[str, ...] | None = None,
        kind: str = "",
        mime_type: str = "",
        created_from: str = "",
        created_to: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        try:
            clean_tags = self._tag_tuple(tags or ())
            result = await self._runtime.service.search_assets(
                SearchAssetsRequest(
                    query=query,
                    tags=clean_tags,
                    kind=kind,
                    mime_type=mime_type,
                    created_from=created_from,
                    created_to=created_to,
                    limit=limit,
                    offset=offset,
                )
            )
            items = []
            for item in result.results:
                asset = self._asset_dict(item.asset, search_result=True)
                asset["match_reasons"] = list(item.match_reasons)
                if item.semantic_score is not None:
                    asset["semantic_score"] = item.semantic_score
                items.append(asset)
            return {
                "total": result.total,
                "offset": result.offset,
                "limit": result.limit,
                "results": items,
            }
        except Exception as exc:
            candidate = str(exc)
            if candidate in _OB_SEARCH_ERROR_CODES:
                raise RememberMeCoreAdapterError(candidate) from exc
            self._raise_mapped(exc)

    @guarded_async_mutation("remember_me_reindex")
    async def reindex_embeddings(
        self,
        asset_id: str = "",
        limit: int = 100,
    ) -> RememberMeReindexResult:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise RememberMeCoreAdapterError("invalid_limit")
        provider = getattr(self._runtime.service, "vector_provider", None)
        clear_diagnostics = getattr(provider, "clear_error_diagnostics", None)
        if callable(clear_diagnostics):
            clear_diagnostics()
        try:
            result = await self._runtime.service.reindex_embeddings(
                ReindexEmbeddingsRequest(
                    asset_id=(asset_id or "").strip(),
                    limit=limit,
                )
            )
            last_error, last_error_details = (
                _bounded_reindex_failure(provider)
                if result.failed
                else ("", None)
            )
            if last_error_details == {}:
                last_error_details = None
            return RememberMeReindexResult(
                scanned=result.scanned,
                indexed=result.indexed,
                skipped=result.skipped,
                failed=result.failed,
                last_error=last_error,
                last_error_details=last_error_details,
            )
        except InvalidMetadata as exc:
            raise RememberMeCoreAdapterError("asset_unavailable") from exc
        except AssetUnavailable as exc:
            raise RememberMeCoreAdapterError("asset_unavailable") from exc
        except Exception as exc:
            raise RememberMeCoreAdapterError("asset_unavailable") from exc

    def resolve_blob(self, asset_id: str) -> tuple[dict, bytes]:
        self._require_asset_id(asset_id)
        try:
            resolved = self._runtime.service.resolve_asset(
                ResolveAssetRequest(asset_id.strip())
            )
            content = self._runtime.blob_store.read(resolved.blob_key)
            return self._asset_dict(resolved.asset), content
        except Exception as exc:
            self._raise_mapped(exc)

    def resolve_ob_download(self, asset_id: str) -> tuple[dict, bytes]:
        self._require_asset_id(asset_id)
        try:
            resolved = self._runtime.service.resolve_asset(
                ResolveAssetRequest(asset_id.strip())
            )
            content = self._runtime.blob_store.read(resolved.blob_key)
            metadata = self._ob_public_metadata(resolved.asset)
            if not isinstance(content, bytes):
                raise RememberMeCoreAdapterError("repository_failure")
            if len(content) != metadata["stored_bytes"]:
                raise RememberMeCoreAdapterError("repository_failure")
            if hashlib.sha256(content).hexdigest() != metadata["stored_sha256"]:
                raise RememberMeCoreAdapterError("repository_failure")
            return metadata, bytes(content)
        except Exception as exc:
            self._raise_mapped(exc)

    @guarded_mutation("remember_me_delete")
    def delete(self, asset_id: str) -> dict:
        self._require_asset_id(asset_id)
        try:
            result = self._runtime.service.delete_asset(
                DeleteAssetRequest(asset_id.strip())
            )
            return {
                "asset_id": result.asset_id,
                "deleted": bool(result.deleted),
                "cleanup_pending": bool(result.cleanup_pending),
            }
        except Exception as exc:
            self._raise_mapped(exc)

    @staticmethod
    def _asset_dict(asset: Any, *, search_result: bool = False) -> dict:
        timestamp_fields = {
            "created_at": _normalize_timestamp(asset.created_at),
            "updated_at": _normalize_timestamp(asset.updated_at),
        }
        result = {
            "asset_id": asset.asset_id,
            "original_filename": asset.original_filename,
            "mime_type": asset.mime_type,
            "kind": asset.kind,
            "decoded_bytes": asset.decoded_bytes,
            "stored_bytes": asset.stored_bytes,
            "width": asset.width,
            "height": asset.height,
            "title": asset.title,
            "description": asset.description,
            "tags": list(asset.tags),
            **timestamp_fields,
        }
        if search_result:
            result["filename"] = result.pop("original_filename")
        return result

    @staticmethod
    def _ob_public_metadata(asset: Any) -> dict:
        """Match the hashes and metadata already exposed by OB tools."""
        return {
            "asset_id": asset.asset_id,
            "source_sha256": asset.source_sha256,
            "stored_sha256": asset.stored_sha256,
            "decoded_bytes": asset.decoded_bytes,
            "stored_bytes": asset.stored_bytes,
            "mime_type": asset.mime_type,
            "filename": asset.original_filename,
            "kind": asset.kind,
            "width": asset.width,
            "height": asset.height,
            "created_at": _normalize_timestamp(asset.created_at),
            "title": asset.title,
            "description": asset.description,
            "tags": list(asset.tags),
            "updated_at": _normalize_timestamp(asset.updated_at),
        }

    @staticmethod
    def _valid_asset_id(asset_id: Any) -> bool:
        return (
            isinstance(asset_id, str)
            and _ASSET_ID_PATTERN.fullmatch(asset_id.strip()) is not None
        )

    @classmethod
    def _require_asset_id(cls, asset_id: Any) -> None:
        if not cls._valid_asset_id(asset_id):
            raise RememberMeCoreAdapterError("invalid_asset_id")

    @staticmethod
    def _tag_tuple(tags: Any) -> tuple[str, ...]:
        if not isinstance(tags, (list, tuple)):
            raise RememberMeCoreAdapterError("invalid_metadata")
        return tuple(tags)

    @classmethod
    def _validate_metadata_update(
        cls,
        *,
        title: Any,
        description: Any,
        tags: Any,
    ) -> tuple[str, ...] | None:
        import unicodedata

        whitespace = re.compile(r"\s+")
        control_categories = {"Cc", "Cf"}
        for value, field, maximum in (
            (title, "title", 200),
            (description, "description", 4000),
        ):
            if value is None:
                continue
            if type(value) is not str:
                raise RememberMeCoreAdapterError(
                    "invalid_metadata", ob_code=f"invalid_{field}"
                )
            normalized = unicodedata.normalize("NFKC", value)
            normalized = "".join(
                " "
                if unicodedata.category(character)
                in control_categories
                else character
                for character in normalized
            )
            normalized = normalized.strip()
            if len(normalized) > maximum:
                raise RememberMeCoreAdapterError(
                    "invalid_metadata", ob_code=f"{field}_too_long"
                )

        clean_tags = None if tags is None else cls._tag_tuple(tags)
        if clean_tags is None:
            return None
        identities = set()
        for value in clean_tags:
            if type(value) is not str:
                raise RememberMeCoreAdapterError(
                    "invalid_metadata", ob_code="invalid_tag"
                )
            normalized = unicodedata.normalize("NFKC", value)
            normalized = "".join(
                " "
                if unicodedata.category(character)
                in control_categories
                else character
                for character in normalized
            )
            normalized = whitespace.sub(" ", normalized).strip()
            if len(normalized) > 64:
                raise RememberMeCoreAdapterError(
                    "invalid_metadata", ob_code="tag_too_long"
                )
            if normalized:
                identities.add(normalized.casefold())
        if len(identities) > 30:
            raise RememberMeCoreAdapterError(
                "invalid_metadata", ob_code="too_many_tags"
            )
        return clean_tags

    @staticmethod
    def _raise_mapped(exc: Exception) -> None:
        ob_code = None
        if isinstance(exc, RememberMeCoreAdapterError):
            raise exc
        if isinstance(exc, AssetFileUnavailable):
            code = "blob_missing"
        elif isinstance(exc, AssetNotFoundError):
            code = "asset_not_found"
        elif isinstance(exc, UploadTooLarge):
            code = "upload_too_large"
        elif isinstance(exc, UploadSizeMismatch):
            code = "upload_size_mismatch"
        elif isinstance(exc, ImagePixelLimitExceeded):
            code = "pixel_limit"
        elif isinstance(exc, ImageValidationError):
            code = "invalid_image"
        elif isinstance(exc, InvalidMetadata):
            candidate = str(exc)
            ob_code = (
                candidate
                if candidate in _OB_METADATA_ERROR_CODES
                else None
            )
            code = "invalid_metadata"
        elif isinstance(exc, StorageConsistencyError):
            code = "repository_failure"
        elif isinstance(exc, RememberMeError):
            code = "core_failure"
        else:
            code = "repository_failure"
        raise RememberMeCoreAdapterError(
            code,
            ob_code=ob_code,
        ) from exc


def _normalize_timestamp(value: str) -> str:
    """Return the seconds-precision UTC format currently emitted by OB."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise RememberMeCoreAdapterError("repository_failure") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "RememberMeCoreAdapter",
    "RememberMeCoreAdapterError",
    "RememberMeReindexResult",
]
