from __future__ import annotations

import hashlib
import io
import json
import re
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header

from asset_backend import AssetBackendError
from asset_store import (
    MAX_IMAGE_PIXELS,
    AssetStore,
    AssetStoreError,
    InvalidAssetImage,
)


ALLOWED_IMAGE_MIME_TYPES = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
}
DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 50
THUMBNAIL_SIZE = (360, 240)
MULTIPART_OVERHEAD_BYTES = 256 * 1024
MAX_FORM_FIELD_BYTES = 16 * 1024
UPLOAD_FIELDS = {"file", "title", "description", "tags"}


class AssetDashboardError(Exception):
    def __init__(self, code: str, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class AssetImage:
    asset: dict
    path: Path | None
    mime_type: str
    thumbnail_bytes: bytes | None = None
    content: bytes | None = None


@dataclass(frozen=True)
class AssetUpload:
    path: Path
    filename: str
    mime_type: str
    decoded_bytes: int
    source_sha256: str
    title: str | None
    description: str | None
    tags: list[str] | None


class AssetDashboardService:
    """Reusable authenticated web adapter for Remember-Me image assets."""

    def __init__(
        self,
        store: AssetStore | None = None,
        *,
        backend_provider=None,
        max_asset_bytes: int,
        max_image_pixels: int = MAX_IMAGE_PIXELS,
    ):
        if store is None and backend_provider is None:
            raise TypeError("asset_backend_required")
        self.store = store
        self._backend_provider = backend_provider or (lambda: self.store)
        self.max_asset_bytes = max_asset_bytes
        self.max_image_pixels = max_image_pixels

    def _backend(self):
        try:
            backend = self._backend_provider()
        except AssetBackendError as exc:
            raise AssetDashboardError("asset_authority_unavailable", 503) from exc
        if backend is None:
            raise AssetDashboardError("asset_authority_unavailable", 503)
        return backend

    @staticmethod
    def parse_pagination(limit_value: str, offset_value: str) -> tuple[int, int]:
        try:
            limit = int(limit_value or DEFAULT_PAGE_LIMIT)
            offset = int(offset_value or 0)
        except (TypeError, ValueError) as exc:
            raise AssetDashboardError("invalid_pagination") from exc
        if not 1 <= limit <= MAX_PAGE_LIMIT or offset < 0:
            raise AssetDashboardError("invalid_pagination")
        return limit, offset

    @staticmethod
    def _safe_metadata(asset: dict) -> dict:
        asset_id = asset["asset_id"]
        return {
            "asset_id": asset_id,
            "filename": asset["original_filename"],
            "title": asset.get("title", ""),
            "description": asset.get("description", ""),
            "tags": list(asset.get("tags", [])),
            "kind": asset["kind"],
            "mime_type": asset["mime_type"],
            "width": asset["width"],
            "height": asset["height"],
            "stored_bytes": asset["stored_bytes"],
            "created_at": asset["created_at"],
            "updated_at": asset["updated_at"],
            "thumbnail_url": f"/api/assets/{asset_id}/thumbnail",
            "image_url": f"/api/assets/{asset_id}/image",
            "detail_url": f"/api/assets/{asset_id}",
        }

    @staticmethod
    def _decode_parameter(value: bytes | None, fallback: str = "") -> str:
        if not value:
            return fallback
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("latin-1", errors="replace")

    @staticmethod
    def _parse_tags(value: str | None) -> list[str] | None:
        if value is None:
            return None
        raw = value.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = [item.strip() for item in re.split(r"[,，]", raw)]
        if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
            raise AssetDashboardError("invalid_tags")
        return parsed

    async def parse_upload(self, request) -> AssetUpload:
        backend = self._backend()
        content_type = request.headers.get("content-type", "")
        kind, options = parse_options_header(
            content_type.encode("latin-1", errors="ignore")
        )
        boundary = options.get(b"boundary")
        if kind != b"multipart/form-data" or not boundary:
            raise AssetDashboardError("invalid_multipart")

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.max_asset_bytes + MULTIPART_OVERHEAD_BYTES:
                    raise AssetDashboardError("file_too_large", 413)
            except ValueError as exc:
                raise AssetDashboardError("invalid_content_length") from exc

        try:
            temp_path = backend.create_temp_path()
        except (AssetBackendError, AssetStoreError) as exc:
            code = getattr(exc, "code", str(exc))
            status = 409 if code == "asset_write_frozen" else 503
            raise AssetDashboardError(code, status) from exc
        state = {
            "headers": {},
            "header_name": bytearray(),
            "header_value": bytearray(),
            "part_name": "",
            "in_file": False,
            "file_count": 0,
            "filename": "",
            "mime_type": "",
            "decoded_bytes": 0,
            "wire_bytes": 0,
            "hasher": hashlib.sha256(),
            "field_data": bytearray(),
            "fields": {},
            "ended": False,
        }

        try:
            with temp_path.open("wb") as handle:
                def on_part_begin():
                    state["headers"] = {}
                    state["header_name"].clear()
                    state["header_value"].clear()
                    state["part_name"] = ""
                    state["in_file"] = False
                    state["field_data"].clear()

                def on_header_field(data, start, end):
                    state["header_name"].extend(data[start:end])

                def on_header_value(data, start, end):
                    state["header_value"].extend(data[start:end])

                def on_header_end():
                    name = bytes(state["header_name"]).lower()
                    state["headers"][name] = bytes(state["header_value"])
                    state["header_name"].clear()
                    state["header_value"].clear()

                def on_headers_finished():
                    disposition, params = parse_options_header(
                        state["headers"].get(b"content-disposition", b"")
                    )
                    if disposition != b"form-data":
                        raise AssetDashboardError("invalid_multipart")
                    name = self._decode_parameter(params.get(b"name"))
                    if name not in UPLOAD_FIELDS:
                        raise AssetDashboardError("unexpected_form_field")
                    if name in state["fields"]:
                        raise AssetDashboardError("duplicate_form_field")
                    state["part_name"] = name
                    if name == "file":
                        if b"filename" not in params or state["file_count"]:
                            raise AssetDashboardError("single_file_required")
                        state["file_count"] = 1
                        state["in_file"] = True
                        state["filename"] = backend.sanitize_filename(
                            self._decode_parameter(params.get(b"filename"), "image")
                        )
                        raw_mime = state["headers"].get(b"content-type", b"")
                        state["mime_type"] = self._decode_parameter(raw_mime).lower()

                def on_part_data(data, start, end):
                    block = data[start:end]
                    if state["in_file"]:
                        state["decoded_bytes"] += len(block)
                        if state["decoded_bytes"] > self.max_asset_bytes:
                            raise AssetDashboardError("file_too_large", 413)
                        state["hasher"].update(block)
                        handle.write(block)
                    else:
                        if len(state["field_data"]) + len(block) > MAX_FORM_FIELD_BYTES:
                            raise AssetDashboardError("form_field_too_large")
                        state["field_data"].extend(block)

                def on_part_end():
                    name = state["part_name"]
                    if name == "file":
                        state["fields"][name] = True
                    elif name:
                        try:
                            value = bytes(state["field_data"]).decode("utf-8")
                        except UnicodeDecodeError as exc:
                            raise AssetDashboardError("invalid_form_encoding") from exc
                        state["fields"][name] = value

                def on_end():
                    state["ended"] = True

                parser = MultipartParser(
                    boundary,
                    {
                        "on_part_begin": on_part_begin,
                        "on_part_data": on_part_data,
                        "on_part_end": on_part_end,
                        "on_header_field": on_header_field,
                        "on_header_value": on_header_value,
                        "on_header_end": on_header_end,
                        "on_headers_finished": on_headers_finished,
                        "on_end": on_end,
                    },
                    max_size=self.max_asset_bytes + MULTIPART_OVERHEAD_BYTES,
                )
                async for block in request.stream():
                    state["wire_bytes"] += len(block)
                    if state["wire_bytes"] > self.max_asset_bytes + MULTIPART_OVERHEAD_BYTES:
                        raise AssetDashboardError("file_too_large", 413)
                    parser.write(block)
                parser.finalize()
        except AssetDashboardError:
            temp_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            temp_path.unlink(missing_ok=True)
            raise AssetDashboardError("invalid_multipart") from exc

        if (
            not state["ended"]
            or state["file_count"] != 1
            or not state["fields"].get("file")
            or state["decoded_bytes"] <= 0
        ):
            temp_path.unlink(missing_ok=True)
            raise AssetDashboardError("single_file_required")
        if state["mime_type"] not in ALLOWED_IMAGE_MIME_TYPES:
            temp_path.unlink(missing_ok=True)
            raise AssetDashboardError("unsupported_image", 415)

        try:
            tags = self._parse_tags(state["fields"].get("tags"))
        except AssetDashboardError:
            temp_path.unlink(missing_ok=True)
            raise
        return AssetUpload(
            path=temp_path,
            filename=state["filename"],
            mime_type=state["mime_type"],
            decoded_bytes=state["decoded_bytes"],
            source_sha256=state["hasher"].hexdigest(),
            title=state["fields"].get("title"),
            description=state["fields"].get("description"),
            tags=tags,
        )

    def create_asset(self, upload: AssetUpload) -> dict:
        backend = self._backend()
        try:
            clean_title = (
                backend.clean_metadata_text(upload.title, 200, "title")
                if upload.title is not None
                else None
            )
            clean_description = (
                backend.clean_metadata_text(upload.description, 4000, "description")
                if upload.description is not None
                else None
            )
            clean_tags = (
                backend.normalize_tags(upload.tags)
                if upload.tags is not None
                else None
            )
            asset = backend.persist_upload(
                upload.path,
                upload.source_sha256,
                upload.decoded_bytes,
                upload.filename,
                upload.mime_type,
                require_image=True,
                title=clean_title or "",
                description=clean_description or "",
                tags=clean_tags or [],
            )
            metadata_supplied = bool(
                (clean_title is not None and clean_title)
                or (clean_description is not None and clean_description)
                or (clean_tags is not None and clean_tags)
            )
            if metadata_supplied and getattr(backend, "name", "legacy") == "legacy":
                asset = backend.update_metadata(
                    asset["asset_id"],
                    title=clean_title if clean_title else None,
                    description=(
                        clean_description
                        if clean_description
                        else None
                    ),
                    tags=clean_tags if clean_tags else None,
                )
            safe = self._safe_metadata(asset)
            safe["deduplicated"] = bool(asset.get("deduplicated"))
            return safe
        except InvalidAssetImage as exc:
            upload.path.unlink(missing_ok=True)
            raise AssetDashboardError(str(exc), 422) from exc
        except (AssetStoreError, AssetBackendError) as exc:
            upload.path.unlink(missing_ok=True)
            code = getattr(exc, "code", str(exc))
            status = 409 if code == "asset_write_frozen" else 400
            raise AssetDashboardError(code, status) from exc

    def update_asset(self, asset_id: str, payload: dict) -> dict:
        backend = self._backend()
        if not isinstance(payload, dict):
            raise AssetDashboardError("invalid_json")
        allowed = {"title", "description", "tags"}
        if not payload or set(payload) - allowed:
            raise AssetDashboardError("invalid_fields")
        try:
            backend.assert_public_mutation_allowed()
            existing = backend.get(asset_id)
        except (AssetStoreError, AssetBackendError) as exc:
            if getattr(exc, "code", str(exc)) == "asset_write_frozen":
                raise AssetDashboardError("asset_write_frozen", 409) from exc
            raise AssetDashboardError("asset_unavailable", 404) from exc
        if not existing or existing.get("kind") != "image":
            raise AssetDashboardError("asset_not_found", 404)
        try:
            asset = backend.update_metadata(asset_id, **payload)
        except (AssetStoreError, AssetBackendError) as exc:
            code = str(exc)
            status = 404 if code == "asset_unavailable" else 400
            if code == "asset_write_frozen":
                status = 409
            raise AssetDashboardError(code, status) from exc
        if asset.get("kind") != "image":
            raise AssetDashboardError("asset_not_found", 404)
        return self._safe_metadata(asset)

    def delete_asset(self, asset_id: str) -> dict:
        backend = self._backend()
        try:
            backend.assert_public_mutation_allowed()
            asset = backend.get(asset_id)
        except (AssetStoreError, AssetBackendError) as exc:
            if getattr(exc, "code", str(exc)) == "asset_write_frozen":
                raise AssetDashboardError("asset_write_frozen", 409) from exc
            raise AssetDashboardError("asset_unavailable", 404) from exc
        if not asset or asset.get("kind") != "image":
            raise AssetDashboardError("asset_not_found", 404)
        try:
            return backend.delete(asset_id)
        except (AssetStoreError, AssetBackendError) as exc:
            code = getattr(exc, "code", str(exc))
            status = 404 if code in {"asset_unavailable", "asset_file_unavailable"} else 409
            raise AssetDashboardError(code, status) from exc

    def list_assets(
        self,
        *,
        query: str = "",
        tag: str = "",
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> dict:
        backend = self._backend()
        tags = [tag] if tag.strip() else None
        try:
            result = backend.search(
                query=query,
                tags=tags,
                kind="image",
                limit=limit,
                offset=offset,
            )
        except (AssetStoreError, AssetBackendError) as exc:
            raise AssetDashboardError(getattr(exc, "code", str(exc))) from exc
        return {
            "total": result["total"],
            "offset": result["offset"],
            "limit": result["limit"],
            "results": [
                self._safe_metadata(
                    {
                        **item,
                        "original_filename": item["filename"],
                    }
                )
                for item in result["results"]
            ],
        }

    def get_asset(self, asset_id: str) -> dict:
        try:
            asset = self._backend().get(asset_id)
        except (AssetStoreError, AssetBackendError) as exc:
            raise AssetDashboardError("asset_unavailable", 404) from exc
        if not asset or asset.get("kind") != "image":
            raise AssetDashboardError("asset_not_found", 404)
        if asset.get("mime_type") not in ALLOWED_IMAGE_MIME_TYPES:
            raise AssetDashboardError("unsupported_image", 415)
        return self._safe_metadata(asset)

    def resolve_image(self, asset_id: str, *, thumbnail: bool = False) -> AssetImage:
        backend = self._backend()
        try:
            resolved = backend.resolve(asset_id)
        except (AssetStoreError, AssetBackendError) as exc:
            raise AssetDashboardError("asset_unavailable", 404) from exc
        if not resolved:
            raise AssetDashboardError("asset_not_found", 404)
        asset = resolved.asset
        path = resolved.path
        mime_type = asset.get("mime_type", "")
        if asset.get("kind") != "image":
            raise AssetDashboardError("asset_not_found", 404)
        expected_format = ALLOWED_IMAGE_MIME_TYPES.get(mime_type)
        if not expected_format:
            raise AssetDashboardError("unsupported_image", 415)
        try:
            actual_bytes = (
                path.stat().st_size
                if path is not None
                else len(resolved.content or b"")
            )
        except OSError as exc:
            raise AssetDashboardError("asset_unavailable", 404) from exc
        if actual_bytes <= 0 or actual_bytes > self.max_asset_bytes:
            raise AssetDashboardError("asset_unavailable", 422)

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                source = path if path is not None else io.BytesIO(resolved.content or b"")
                with Image.open(source) as opened:
                    opened.load()
                    actual_format = (opened.format or "").upper()
                    width, height = opened.size
                    if (
                        actual_format != expected_format
                        or width <= 0
                        or height <= 0
                        or width * height > self.max_image_pixels
                        or width != asset["width"]
                        or height != asset["height"]
                    ):
                        raise AssetDashboardError("asset_unavailable", 422)
                    if not thumbnail:
                        return AssetImage(
                            asset,
                            path,
                            mime_type,
                            content=resolved.content,
                        )
                    image = opened.copy()
        except AssetDashboardError:
            raise
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            UnidentifiedImageError,
            OSError,
            ValueError,
        ) as exc:
            raise AssetDashboardError("asset_unavailable", 422) from exc

        image.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
        output = io.BytesIO()
        if mime_type == "image/jpeg":
            if image.mode != "RGB":
                image = image.convert("RGB")
            image.save(output, format="JPEG", quality=82, optimize=True)
        else:
            image.save(output, format="PNG", optimize=True)
        image.close()
        return AssetImage(asset, path, mime_type, output.getvalue())
