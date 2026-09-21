import base64
import hashlib
import importlib
import io
import json
import re
import sqlite3
import sys

import pytest
from PIL import Image

from asset_viewer import (
    ASSET_VIEWER_HTML,
    ASSET_VIEWER_MIME_TYPE,
    ASSET_VIEWER_PATH,
    ASSET_VIEWER_RESOURCE_META,
    ASSET_VIEWER_TOOL_META,
    ASSET_VIEWER_URI,
)


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_PUBLIC_BASE_URL", "https://example.invalid")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def _jpeg_bytes(size=(48, 32)):
    image = Image.new("RGB", size, (35, 90, 160))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=90)
    image.close()
    return output.getvalue()


def _persist(server, data, filename="viewer.jpg", mime_type="image/jpeg"):
    source = server.asset_store.create_temp_path()
    source.write_bytes(data)
    return server.asset_store.persist_upload(
        source,
        hashlib.sha256(data).hexdigest(),
        len(data),
        filename,
        mime_type,
    )


def _result_text(result):
    return "\n".join(
        item.text
        for item in result.content
        if getattr(item, "type", "") == "text"
    )


def test_asset_viewer_resource_is_self_contained_and_safe():
    assert ASSET_VIEWER_URI == "ui://remember-me/asset-viewer.html"
    assert ASSET_VIEWER_MIME_TYPE == "text/html;profile=mcp-app"
    assert ASSET_VIEWER_TOOL_META["ui"]["resourceUri"] == ASSET_VIEWER_URI
    assert ASSET_VIEWER_TOOL_META["ui/resourceUri"] == ASSET_VIEWER_URI
    assert ASSET_VIEWER_RESOURCE_META == {
        "ui": {
            "csp": {"connectDomains": [], "resourceDomains": []},
            "prefersBorder": True,
        }
    }
    assert "*" not in json.dumps(ASSET_VIEWER_RESOURCE_META)

    assert ASSET_VIEWER_PATH.is_file()
    assert ASSET_VIEWER_HTML == ASSET_VIEWER_PATH.read_text(encoding="utf-8")
    html = ASSET_VIEWER_HTML
    assert "Loading Remember-Me image" in html
    assert "ui/initialize" in html
    assert "ui/notifications/initialized" in html
    assert "ui/notifications/tool-result" in html
    assert "ui/notifications/size-changed" in html
    assert "Image metadata received, but image bytes were unavailable." in html
    assert "The viewer connection timed out." in html
    assert "structuredContent" in html
    assert ".textContent" in html
    assert "replaceChildren" in html
    for forbidden in (
        "innerHTML",
        "fetch(",
        "XMLHttpRequest",
        "localStorage",
        "document.cookie",
        "window.parent.document",
        "URL.createObjectURL",
        "unpkg.com",
        "jsdelivr.net",
        "localhost",
        "127.0.0.1",
        "sourceMappingURL",
        "@vite/client",
    ):
        assert forbidden not in html

    dockerfile = (ASSET_VIEWER_PATH.parent / "Dockerfile").read_text(
        encoding="utf-8",
    )
    assert "COPY asset_viewer.html ." in dockerfile

@pytest.mark.asyncio
async def test_rm_asset_view_layers_clean_jpeg_and_reuses_download_token(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch)
    asset = _persist(
        server,
        _jpeg_bytes(),
        filename='<img src=x onerror="alert(1)">.jpg',
    )
    asset = server.asset_store.update_metadata(
        asset["asset_id"],
        title="<script>alert(1)</script>",
        tags=["<b>tag</b>", "safe"],
    )
    stored_path = server.asset_store.resolve_file(asset["asset_id"])[1]
    stored_bytes = stored_path.read_bytes()

    result = await server.rm_asset_view(asset["asset_id"])
    assert isinstance(result, server.CallToolResult)
    assert result.isError is False
    assert len(result.content) == 1
    assert result.content[0].type == "text"

    text = _result_text(result)
    assert "https://example.invalid/rm/asset-download/" in text
    assert base64.b64encode(stored_bytes).decode("ascii") not in text
    assert asset["stored_sha256"] not in text
    assert str(stored_path) not in text

    expected_keys = {
        "asset_id",
        "title",
        "filename",
        "mime_type",
        "width",
        "height",
        "tags",
        "stored_bytes",
    }
    assert set(result.structuredContent) == expected_keys
    assert result.structuredContent["title"] == "<script>alert(1)</script>"
    assert result.structuredContent["tags"] == ["<b>tag</b>", "safe"]
    structured_text = json.dumps(result.structuredContent)
    for forbidden in ("base64", "sha256", "stored_relpath", str(stored_path)):
        assert forbidden not in structured_text

    assert set(result.meta) == {"rememberMe"}
    remember_me = result.meta["rememberMe"]
    assert remember_me["schemaVersion"] == 1
    assert remember_me["mimeType"] == "image/jpeg"
    assert base64.b64decode(remember_me["imageBase64"], validate=True) == stored_bytes
    meta_without_image = {
        **remember_me,
        "imageBase64": "",
    }
    meta_text = json.dumps(meta_without_image)
    for forbidden in ("stored_relpath", str(stored_path), "api_key", "exif", "gps"):
        assert forbidden not in meta_text.casefold()

    token = re.search(r"/rm/asset-download/([A-Za-z0-9_-]+)", text).group(1)
    assert server._rm_read_asset_download(token, "HEAD") is not None
    assert [server._rm_read_asset_download(token, "GET") is not None for _ in range(4)] == [
        True,
        True,
        True,
        False,
    ]


@pytest.mark.asyncio
async def test_rm_asset_view_rejects_missing_and_non_image(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    missing = await server.rm_asset_view("f" * 32)
    assert missing.isError is True
    assert missing.structuredContent == {"ok": False, "error": "asset_unavailable"}

    payload = b"not an image asset"
    asset = _persist(
        server,
        payload,
        filename="document.bin",
        mime_type="application/octet-stream",
    )
    result = await server.rm_asset_view(asset["asset_id"])
    assert result.isError is True
    assert result.structuredContent == {"ok": False, "error": "asset_not_image"}
    assert "base64" not in result.model_dump_json(by_alias=True)


@pytest.mark.asyncio
async def test_rm_asset_view_rejects_bad_mime_corruption_and_oversize(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch)

    invalid_mime = _persist(server, _jpeg_bytes(), filename="mime.jpg")
    with sqlite3.connect(server.asset_store.db_path) as conn:
        conn.execute(
            "UPDATE assets SET mime_type = 'application/octet-stream' WHERE asset_id = ?",
            (invalid_mime["asset_id"],),
        )
    mime_result = await server.rm_asset_view(invalid_mime["asset_id"])
    assert mime_result.structuredContent["error"] == "invalid_image_mime"

    corrupted = _persist(server, _jpeg_bytes((40, 24)), filename="broken.jpg")
    corrupted_path = server.asset_store.resolve_file(corrupted["asset_id"])[1]
    corrupted_path.write_bytes(b"x" * corrupted["stored_bytes"])
    corrupt_result = await server.rm_asset_view(corrupted["asset_id"])
    assert corrupt_result.structuredContent["error"] == "image_unavailable"

    oversized = _persist(server, _jpeg_bytes((36, 20)), filename="large.jpg")
    oversized_path = server.asset_store.resolve_file(oversized["asset_id"])[1]
    oversized_path.write_bytes(
        oversized_path.read_bytes()
        + b"x" * (server.RM_ASSET_MAX_UPLOAD_BYTES + 1)
    )
    actual_size = oversized_path.stat().st_size
    with sqlite3.connect(server.asset_store.db_path) as conn:
        conn.execute(
            "UPDATE assets SET stored_bytes = ? WHERE asset_id = ?",
            (actual_size, oversized["asset_id"]),
        )
    oversized_result = await server.rm_asset_view(oversized["asset_id"])
    assert oversized_result.structuredContent["error"] == "image_too_large"


@pytest.mark.asyncio
async def test_asset_view_protocol_smoke(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    asset = _persist(server, _jpeg_bytes(), filename="protocol.jpg")
    stored_bytes = server.asset_store.resolve_file(asset["asset_id"])[1].read_bytes()

    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server.mcp) as client:
        tools = (await client.list_tools()).tools
        tool = next(item for item in tools if item.name == "rm_asset_view")
        assert tool.meta["ui"]["resourceUri"] == ASSET_VIEWER_URI
        assert tool.meta["ui/resourceUri"] == ASSET_VIEWER_URI
        assert tool.inputSchema["required"] == ["asset_id"]

        resources = (await client.list_resources()).resources
        resource = next(item for item in resources if str(item.uri) == ASSET_VIEWER_URI)
        assert resource.mimeType == ASSET_VIEWER_MIME_TYPE
        assert resource.meta == ASSET_VIEWER_RESOURCE_META

        read_result = await client.read_resource(ASSET_VIEWER_URI)
        assert len(read_result.contents) == 1
        content = read_result.contents[0]
        assert content.mimeType == ASSET_VIEWER_MIME_TYPE
        assert content.text == ASSET_VIEWER_HTML
        assert content.meta == ASSET_VIEWER_RESOURCE_META

        call_result = await client.call_tool(
            "rm_asset_view",
            {"asset_id": asset["asset_id"]},
        )
        assert call_result.isError is False
        assert call_result.structuredContent["asset_id"] == asset["asset_id"]
        assert base64.b64decode(
            call_result.meta["rememberMe"]["imageBase64"],
            validate=True,
        ) == stored_bytes
        assert "imageBase64" not in json.dumps(call_result.structuredContent)
        assert "imageBase64" not in _result_text(call_result)
