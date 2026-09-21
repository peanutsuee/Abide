"""Isolated compatibility presentation for current OB image MCP contracts."""

from __future__ import annotations

import base64
import io
import json
import math
from typing import Any, Mapping

from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image, UnidentifiedImageError
from remember_me.compat.ombre_brain import MAX_IMAGE_PIXELS, MAX_UPLOAD_BYTES

from remember_me_core_adapter import (
    RememberMeCoreAdapterError,
    RememberMeReindexResult,
)
from remember_me_download_links import (
    RememberMeDownloadLinkCollaborator,
    RememberMeDownloadLinkError,
)


class RememberMeMcpCompatibilityPresenterError(RuntimeError):
    """Configuration error that contains no host or asset details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RememberMeMcpCompatibilityPresenter:
    """Present RM Core results using the six current OB MCP envelopes."""

    def __init__(
        self,
        core_adapter: Any,
        download_links: RememberMeDownloadLinkCollaborator,
    ) -> None:
        required = (
            "get",
            "get_ob_public_metadata",
            "update_ob_public_metadata",
            "resolve_blob",
            "search",
        )
        if core_adapter is None or not all(
            callable(getattr(core_adapter, name, None))
            for name in required
        ):
            raise RememberMeMcpCompatibilityPresenterError(
                "core_adapter_unavailable"
            )
        if not callable(
            getattr(download_links, "create_download_link", None)
        ):
            raise RememberMeMcpCompatibilityPresenterError(
                "download_link_collaborator_unavailable"
            )
        self._core = core_adapter
        self._download_links = download_links

    def rm_asset_get(self, asset_id: str) -> str:
        """Present current OB metadata JSON without bytes or disk paths."""
        try:
            asset = self._core.get_ob_public_metadata(asset_id)
        except RememberMeCoreAdapterError:
            return _json_error("asset_unavailable")
        except Exception:
            return _json_error("asset_unavailable")
        normalized = _normalize_public_metadata(asset)
        if normalized is None:
            return _json_error("asset_unavailable")
        try:
            return _json_success(normalized)
        except Exception:
            return _json_error("asset_unavailable")

    def rm_asset_update_metadata(
        self,
        asset_id: str,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Preserve OB omission, null, empty-string, and empty-tag semantics."""
        try:
            asset = self._core.update_ob_public_metadata(
                asset_id,
                title=title,
                description=description,
                tags=tags,
            )
        except RememberMeCoreAdapterError as exc:
            return _json_error(
                _metadata_error_code(exc.ob_code or exc.code)
            )
        except Exception:
            return _json_error("asset_unavailable")
        normalized = _normalize_updated_public_metadata(
            asset,
            requested_asset_id=asset_id,
        )
        if normalized is None:
            return _json_error("asset_unavailable")
        try:
            return _json_success(normalized)
        except Exception:
            return _json_error("asset_unavailable")

    async def rm_asset_search(
        self,
        query: str = "",
        tags: list[str] | None = None,
        kind: str = "",
        mime_type: str = "",
        created_from: str = "",
        created_to: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """Present current OB search JSON without private fields."""
        try:
            result = await self._core.search(
                query=query,
                tags=tags,
                kind=kind,
                mime_type=mime_type,
                created_from=created_from,
                created_to=created_to,
                limit=limit,
                offset=offset,
            )
        except RememberMeCoreAdapterError as exc:
            return _json_error(_search_error_code(exc.ob_code or exc.code))
        except Exception:
            return _json_error("search_unavailable")
        normalized = _normalize_search_result(
            result,
            requested_limit=limit,
            requested_offset=offset,
        )
        if normalized is None:
            return _json_error("search_unavailable")
        try:
            return json.dumps(
                {"ok": True, **normalized},
                ensure_ascii=False,
                sort_keys=True,
            )
        except Exception:
            return _json_error("search_unavailable")

    async def rm_asset_reindex_embeddings(
        self,
        asset_id: str = "",
        limit: int = 100,
    ) -> str:
        """Present RM reindex counters using the legacy OB JSON envelope."""
        try:
            result = await self._core.reindex_embeddings(
                asset_id=asset_id,
                limit=limit,
            )
        except RememberMeCoreAdapterError as exc:
            code = (
                "invalid_limit"
                if exc.code == "invalid_limit"
                else "asset_unavailable"
            )
            return _json_error(code)
        except Exception:
            return _json_error("asset_unavailable")
        try:
            counters = RememberMeReindexResult(
                scanned=result.scanned,
                indexed=result.indexed,
                skipped=result.skipped,
                failed=result.failed,
                last_error=getattr(result, "last_error", ""),
                last_error_details=getattr(
                    result,
                    "last_error_details",
                    None,
                ),
            )
            payload = {
                "ok": True,
                "scanned": counters.scanned,
                "indexed": counters.indexed,
                "skipped": counters.skipped,
                "failed": counters.failed,
            }
            if counters.failed:
                payload["last_error_details"] = (
                    counters.last_error_details or {}
                )
                if counters.last_error:
                    payload["last_error"] = counters.last_error
            return json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
            )
        except Exception:
            return _json_error("asset_unavailable")

    def rm_asset_download_link(self, asset_id: str) -> str:
        """Confirm the asset through RM, then delegate OB Ticket and URL work."""
        try:
            asset = self._core.get_ob_public_metadata(asset_id)
        except RememberMeCoreAdapterError:
            return _json_error("asset_unavailable")
        except Exception:
            return _json_error("asset_unavailable")
        normalized = _normalize_public_metadata(asset)
        if normalized is None:
            return _json_error("asset_unavailable")
        try:
            payload = self._create_download_payload(normalized)
        except Exception:
            return _json_error("download_unavailable")
        if isinstance(payload, str):
            return _json_error(payload)
        try:
            return json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
            )
        except Exception:
            return _json_error("download_unavailable")

    def rm_asset_view(self, asset_id: str) -> CallToolResult:
        """Return the existing OB MCP Apps viewer result and fallback link."""
        verified = self._verified_image(asset_id)
        if isinstance(verified, str):
            return _image_error(verified, inspect=False)
        asset, data = verified
        try:
            encoded = base64.b64encode(data).decode("ascii")
            structured = _flat_image_metadata(asset)
            title = asset.get("title") or asset["original_filename"]
        except Exception:
            return _image_error("image_unavailable", inspect=False)
        download = self._download_payload(
            asset_id,
            expected_asset=asset,
        )
        if download is None:
            return _image_error("download_unavailable", inspect=False)
        fallback_url = (
            download.get("download_url")
            or download.get("download_path")
        )
        if not isinstance(fallback_url, str) or not fallback_url:
            return _image_error("download_unavailable", inspect=False)
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=(
                        f"Remember-Me image: {title}\n"
                        "If this client does not display the inline viewer, "
                        "use this short-lived download link: "
                        f"{fallback_url}"
                    ),
                )
            ],
            structuredContent=structured,
            _meta={
                "rememberMe": {
                    "schemaVersion": 1,
                    "imageBase64": encoded,
                    "mimeType": asset["mime_type"],
                }
            },
        )

    def rm_asset_inspect(self, asset_id: str) -> CallToolResult:
        """Return the current OB text, ImageContent, and flat metadata."""
        verified = self._verified_image(asset_id)
        if isinstance(verified, str):
            return _image_error(verified, inspect=True)
        asset, data = verified
        width = asset["width"]
        height = asset["height"]
        if (
            width <= 0
            or height <= 0
            or width * height > MAX_IMAGE_PIXELS
        ):
            return _image_error("image_too_large", inspect=True)
        try:
            encoded = base64.b64encode(data).decode("ascii")
            structured = _flat_image_metadata(asset)
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=(
                            f"Remember-Me image asset {asset['asset_id']}; "
                            f"filename: {asset['original_filename']}; "
                            f"MIME type: {asset['mime_type']}; "
                            f"dimensions: {width} x {height}."
                        ),
                    ),
                    ImageContent(
                        type="image",
                        data=encoded,
                        mimeType=asset["mime_type"],
                    ),
                ],
                structuredContent=structured,
            )
        except Exception:
            return _image_error("image_unavailable", inspect=True)

    def _verified_image(
        self,
        asset_id: str,
    ) -> tuple[dict, bytes] | str:
        try:
            resolved = self._core.resolve_blob(asset_id)
        except RememberMeCoreAdapterError as exc:
            if exc.code in {
                "asset_not_found",
                "blob_missing",
                "invalid_asset_id",
            }:
                return "asset_unavailable"
            if exc.code == "pixel_limit":
                return "image_too_large"
            return "image_unavailable"
        except Exception:
            return "image_unavailable"
        try:
            if not isinstance(resolved, tuple) or len(resolved) != 2:
                return "image_unavailable"
            raw_asset, data = resolved
            if not isinstance(raw_asset, Mapping):
                return "image_unavailable"
            if not isinstance(data, bytes) or not data:
                return "image_unavailable"
            normalized = {
                "asset_id": raw_asset["asset_id"],
                "original_filename": raw_asset["original_filename"],
                "mime_type": raw_asset["mime_type"],
                "kind": raw_asset["kind"],
                "stored_bytes": raw_asset["stored_bytes"],
                "width": raw_asset["width"],
                "height": raw_asset["height"],
                "title": raw_asset["title"],
                "tags": raw_asset["tags"],
            }
            if (
                not isinstance(normalized["asset_id"], str)
                or len(normalized["asset_id"]) != 32
                or normalized["asset_id"].lower() != normalized["asset_id"]
                or any(
                    char not in "0123456789abcdef"
                    for char in normalized["asset_id"]
                )
            ):
                return "image_unavailable"
            for key in (
                "original_filename",
                "mime_type",
                "kind",
                "title",
            ):
                if not isinstance(normalized[key], str):
                    return "image_unavailable"
            tags = normalized["tags"]
            if (
                not isinstance(tags, (list, tuple))
                or any(not isinstance(tag, str) for tag in tags)
            ):
                return "image_unavailable"
            normalized["tags"] = list(tags)
            for key in ("stored_bytes", "width", "height"):
                value = normalized[key]
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value <= 0
                ):
                    return "image_unavailable"
            if normalized["kind"] != "image":
                return "asset_not_image"
            mime_type = normalized["mime_type"]
            if mime_type not in {"image/jpeg", "image/png"}:
                return "invalid_image_mime"
            if len(data) != normalized["stored_bytes"]:
                return "image_unavailable"
            if len(data) > MAX_UPLOAD_BYTES:
                return "image_too_large"
            with Image.open(io.BytesIO(data)) as image:
                image_format = image.format
                image_size = image.size
                image.verify()
        except (OSError, ValueError, UnidentifiedImageError):
            return "image_unavailable"
        except Exception:
            return "image_unavailable"
        expected_format = "JPEG" if mime_type == "image/jpeg" else "PNG"
        if (
            image_format != expected_format
            or image_size != (normalized["width"], normalized["height"])
        ):
            return "image_unavailable"
        return normalized, bytes(data)

    def _download_payload(
        self,
        asset_id: str,
        *,
        expected_asset: Mapping[str, Any] | None = None,
    ) -> dict | None:
        try:
            asset = self._core.get_ob_public_metadata(asset_id)
        except Exception:
            return None
        normalized = _normalize_public_metadata(asset)
        if normalized is None:
            return None
        if expected_asset is not None:
            try:
                if (
                    normalized["asset_id"] != expected_asset["asset_id"]
                    or normalized["mime_type"] != expected_asset["mime_type"]
                    or normalized["stored_bytes"] != expected_asset["stored_bytes"]
                ):
                    return None
            except Exception:
                return None
        payload = self._create_download_payload(normalized)
        if isinstance(payload, str):
            return None
        if expected_asset is not None:
            try:
                if (
                    payload["asset_id"] != expected_asset["asset_id"]
                    or payload["mime_type"] != expected_asset["mime_type"]
                    or payload["stored_bytes"] != expected_asset["stored_bytes"]
                ):
                    return None
            except Exception:
                return None
        return payload

    def _create_download_payload(
        self,
        asset: Mapping[str, Any],
    ) -> dict[str, Any] | str:
        try:
            payload = self._download_links.create_download_link(asset)
            return _normalize_download_payload(payload)
        except RememberMeDownloadLinkError as exc:
            return _download_error_code(exc.code)
        except RememberMeMcpCompatibilityPresenterError as exc:
            return _download_error_code(exc.code)
        except Exception:
            return "download_unavailable"


def _json_error(error: str) -> str:
    return json.dumps(
        {"ok": False, "error": error},
        ensure_ascii=False,
        sort_keys=True,
    )


def _json_success(asset: Mapping[str, Any]) -> str:
    return json.dumps(
        {"ok": True, **dict(asset)},
        ensure_ascii=False,
        sort_keys=True,
    )


def _metadata_error_code(code: str) -> str:
    if code in {
        "description_too_long",
        "invalid_description",
        "invalid_tag",
        "invalid_tags",
        "invalid_title",
        "tag_too_long",
        "title_too_long",
        "too_many_tags",
    }:
        return code
    return "asset_unavailable"


def _download_error_code(code: Any) -> str:
    if code == "download_store_full":
        return "download_store_full"
    return "download_unavailable"


def _search_error_code(code: Any) -> str:
    if code in _SEARCH_ERROR_CODES:
        return code
    return "search_unavailable"


_OB_PUBLIC_METADATA_KEYS = (
    "asset_id",
    "source_sha256",
    "stored_sha256",
    "decoded_bytes",
    "stored_bytes",
    "mime_type",
    "filename",
    "kind",
    "width",
    "height",
    "created_at",
    "title",
    "description",
    "tags",
    "updated_at",
)

_DOWNLOAD_PAYLOAD_KEYS = (
    "asset_id",
    "filename",
    "mime_type",
    "stored_bytes",
    "stored_sha256",
    "download_path",
    "download_url",
    "expires_in_seconds",
)

_SEARCH_RESULT_KEYS = (
    "asset_id",
    "filename",
    "title",
    "description",
    "tags",
    "kind",
    "mime_type",
    "width",
    "height",
    "stored_bytes",
    "created_at",
    "updated_at",
    "match_reasons",
)

_SEARCH_ERROR_CODES = {
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

_SEARCH_MATCH_REASONS = {
    "asset_id_exact",
    "asset_id",
    "tag_exact",
    "tag",
    "title_exact",
    "title_prefix",
    "title",
    "filename",
    "description",
    "semantic",
}


def _normalize_public_metadata(asset: Any) -> dict[str, Any] | None:
    if not isinstance(asset, Mapping):
        return None
    try:
        return {key: asset[key] for key in _OB_PUBLIC_METADATA_KEYS}
    except Exception:
        return None


def _is_lower_hex(value: str, *, length: int) -> bool:
    return (
        len(value) == length
        and value.lower() == value
        and all(char in "0123456789abcdef" for char in value)
    )


def _normalize_updated_public_metadata(
    asset: Any,
    *,
    requested_asset_id: str,
) -> dict[str, Any] | None:
    try:
        normalized = _normalize_public_metadata(asset)
        requested = (requested_asset_id or "").strip()
    except Exception:
        return None
    if normalized is None:
        return None
    if (
        not isinstance(normalized["asset_id"], str)
        or not _is_lower_hex(normalized["asset_id"], length=32)
        or normalized["asset_id"] != requested
        or not isinstance(normalized["source_sha256"], str)
        or not _is_lower_hex(normalized["source_sha256"], length=64)
        or not isinstance(normalized["stored_sha256"], str)
        or not _is_lower_hex(normalized["stored_sha256"], length=64)
    ):
        return None
    for key in (
        "decoded_bytes",
        "stored_bytes",
    ):
        value = normalized[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
    for key in (
        "filename",
        "mime_type",
        "kind",
        "created_at",
        "title",
        "description",
        "updated_at",
    ):
        if not isinstance(normalized[key], str):
            return None
    for key in ("width", "height"):
        value = normalized[key]
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            return None
    tags = normalized["tags"]
    if (
        not isinstance(tags, (list, tuple))
        or any(not isinstance(tag, str) for tag in tags)
    ):
        return None
    normalized["tags"] = list(tags)
    return normalized


def _normalize_search_result(
    result: Any,
    *,
    requested_limit: int,
    requested_offset: int,
) -> dict[str, Any] | None:
    if not isinstance(result, Mapping):
        return None
    try:
        total = result["total"]
        offset = result["offset"]
        limit = result["limit"]
        items = result["results"]
    except Exception:
        return None
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or total < 0
        or not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset < 0
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 1
        or limit > 50
        or offset != requested_offset
        or limit != requested_limit
        or not isinstance(items, (list, tuple))
        or len(items) > limit
    ):
        return None
    normalized_items = []
    for item in items:
        normalized = _normalize_search_item(item)
        if normalized is None:
            return None
        normalized_items.append(normalized)
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "results": normalized_items,
    }


def _normalize_search_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    try:
        normalized = {key: item[key] for key in _SEARCH_RESULT_KEYS}
        semantic_score = item.get("semantic_score", None)
    except Exception:
        return None
    if (
        not isinstance(normalized["asset_id"], str)
        or not _is_lower_hex(normalized["asset_id"], length=32)
    ):
        return None
    for key in (
        "filename",
        "title",
        "description",
        "kind",
        "mime_type",
        "created_at",
        "updated_at",
    ):
        if not isinstance(normalized[key], str):
            return None
    if normalized["kind"] not in {"image", "file"}:
        return None
    if normalized["mime_type"] not in {
        "application/octet-stream",
        "image/jpeg",
        "image/png",
    }:
        return None
    for key in ("width", "height", "stored_bytes"):
        value = normalized[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
    for key in ("tags", "match_reasons"):
        value = normalized[key]
        if (
            not isinstance(value, (list, tuple))
            or any(not isinstance(entry, str) for entry in value)
        ):
            return None
        normalized[key] = list(value)
    if any(
        reason not in _SEARCH_MATCH_REASONS
        for reason in normalized["match_reasons"]
    ):
        return None
    has_semantic = "semantic" in normalized["match_reasons"]
    if semantic_score is None:
        if has_semantic:
            return None
    else:
        if (
            not has_semantic
            or not isinstance(semantic_score, (int, float))
            or isinstance(semantic_score, bool)
            or not math.isfinite(float(semantic_score))
            or float(semantic_score) < 0.0
            or float(semantic_score) > 1.0
        ):
            return None
        normalized["semantic_score"] = float(semantic_score)
    return normalized


def _normalize_download_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise RememberMeDownloadLinkError("download_unavailable")
    try:
        if payload["ok"] is not True:
            raise RememberMeDownloadLinkError(payload.get("error", ""))
        normalized = {
            key: payload[key] for key in _DOWNLOAD_PAYLOAD_KEYS
        }
        if (
            not isinstance(normalized["asset_id"], str)
            or not isinstance(normalized["filename"], str)
            or not isinstance(normalized["mime_type"], str)
            or not isinstance(normalized["stored_bytes"], int)
            or isinstance(normalized["stored_bytes"], bool)
            or not isinstance(normalized["stored_sha256"], str)
            or not isinstance(normalized["download_path"], str)
            or not normalized["download_path"]
            or not isinstance(normalized["download_url"], str)
            or not isinstance(normalized["expires_in_seconds"], int)
            or isinstance(normalized["expires_in_seconds"], bool)
        ):
            raise RememberMeDownloadLinkError("download_unavailable")
    except RememberMeDownloadLinkError:
        raise
    except Exception as exc:
        raise RememberMeDownloadLinkError(
            "download_unavailable"
        ) from exc
    return {"ok": True, **normalized}


def _flat_image_metadata(asset: Mapping[str, Any]) -> dict:
    return {
        "asset_id": asset["asset_id"],
        "title": asset.get("title", ""),
        "filename": asset["original_filename"],
        "mime_type": asset["mime_type"],
        "width": asset["width"],
        "height": asset["height"],
        "tags": list(asset.get("tags", [])),
        "stored_bytes": asset["stored_bytes"],
    }


_VIEW_MESSAGES = {
    "asset_unavailable": "The requested Remember-Me asset is unavailable.",
    "asset_not_image": "The requested Remember-Me asset is not an image.",
    "invalid_image_mime": (
        "The requested Remember-Me image type is not supported."
    ),
    "image_too_large": (
        "The requested Remember-Me image exceeds the viewer limit."
    ),
    "image_unavailable": (
        "The requested Remember-Me image could not be verified."
    ),
    "download_unavailable": (
        "A temporary fallback download link could not be created."
    ),
}

_INSPECT_MESSAGES = {
    "asset_unavailable": "The requested Remember-Me asset is unavailable.",
    "asset_not_image": "The requested Remember-Me asset is not an image.",
    "invalid_image_mime": (
        "The requested Remember-Me image type is not supported for inspection."
    ),
    "image_too_large": (
        "The requested Remember-Me image exceeds the inspection limit."
    ),
    "image_unavailable": (
        "The requested Remember-Me image could not be verified."
    ),
}


def _image_error(error: str, *, inspect: bool) -> CallToolResult:
    messages = _INSPECT_MESSAGES if inspect else _VIEW_MESSAGES
    fallback = (
        "The Remember-Me image could not be inspected."
        if inspect
        else "The Remember-Me image could not be displayed."
    )
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=messages.get(error, fallback),
            )
        ],
        structuredContent={"ok": False, "error": error},
        isError=True,
    )


__all__ = [
    "RememberMeDownloadLinkCollaborator",
    "RememberMeMcpCompatibilityPresenter",
    "RememberMeMcpCompatibilityPresenterError",
]
