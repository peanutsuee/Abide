import base64
import hashlib
import importlib
import io
import json
import sqlite3
import sys

import pytest
from PIL import Image


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_PUBLIC_BASE_URL", "https://example.invalid")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def _image_bytes(image_format, size=(48, 32)):
    image = Image.new("RGB", size, (45, 105, 170))
    output = io.BytesIO()
    image.save(output, format=image_format, quality=90)
    image.close()
    return output.getvalue()


def _persist(server, data, filename, mime_type):
    source = server.asset_store.create_temp_path()
    source.write_bytes(data)
    return server.asset_store.persist_upload(
        source,
        hashlib.sha256(data).hexdigest(),
        len(data),
        filename,
        mime_type,
    )


def _content_by_type(result, content_type):
    return [
        item
        for item in result.content
        if getattr(item, "type", "") == content_type
    ]


@pytest.mark.parametrize(
    ("image_format", "mime_type", "filename"),
    [
        ("PNG", "image/png", "inspect.png"),
        ("JPEG", "image/jpeg", "inspect.jpg"),
    ],
)
@pytest.mark.asyncio
async def test_rm_asset_inspect_protocol_returns_clean_image_content(
    tmp_path,
    monkeypatch,
    caplog,
    image_format,
    mime_type,
    filename,
):
    server = _load_server(tmp_path, monkeypatch)
    asset = _persist(
        server,
        _image_bytes(image_format),
        filename,
        mime_type,
    )
    asset = server.asset_store.update_metadata(
        asset["asset_id"],
        title="Inspection target",
        tags=["privacy-clean", "sample"],
    )
    stored_path = server.asset_store.resolve_file(asset["asset_id"])[1]
    stored_bytes = stored_path.read_bytes()
    encoded = base64.b64encode(stored_bytes).decode("ascii")

    async def unexpected_embedding_update(*_args, **_kwargs):
        raise AssertionError("rm_asset_inspect must not update embeddings")

    monkeypatch.setattr(
        server.asset_embedding_index,
        "index_asset",
        unexpected_embedding_update,
    )

    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server.mcp) as client:
        tools = (await client.list_tools()).tools
        tool = next(item for item in tools if item.name == "rm_asset_inspect")
        assert tool.inputSchema["required"] == ["asset_id"]
        assert "actual visual understanding" in tool.description
        assert "rm_asset_view" in tool.description
        assert "Never guess image content from metadata" in tool.description

        result = await client.call_tool(
            "rm_asset_inspect",
            {"asset_id": asset["asset_id"]},
        )

    assert result.isError is False
    assert result.meta is None
    text_items = _content_by_type(result, "text")
    image_items = _content_by_type(result, "image")
    assert len(text_items) == 1
    assert len(image_items) == 1
    assert asset["asset_id"] in text_items[0].text
    assert filename in text_items[0].text
    assert mime_type in text_items[0].text
    assert f"{asset['width']} x {asset['height']}" in text_items[0].text
    assert image_items[0].mimeType == mime_type
    assert base64.b64decode(image_items[0].data, validate=True) == stored_bytes

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
    assert result.structuredContent["asset_id"] == asset["asset_id"]
    assert result.structuredContent["title"] == "Inspection target"
    assert result.structuredContent["tags"] == ["privacy-clean", "sample"]
    assert encoded not in text_items[0].text
    assert encoded not in json.dumps(result.structuredContent)
    assert encoded not in caplog.text
    assert str(stored_path) not in result.model_dump_json(by_alias=True)
    assert asset["stored_sha256"] not in result.model_dump_json(by_alias=True)
    unchanged = server.asset_store.get(asset["asset_id"])
    assert unchanged["title"] == "Inspection target"
    assert unchanged["tags"] == ["privacy-clean", "sample"]
    assert unchanged["stored_sha256"] == asset["stored_sha256"]
    assert unchanged["stored_bytes"] == asset["stored_bytes"]


@pytest.mark.asyncio
async def test_rm_asset_inspect_rejects_missing_non_image_and_missing_file(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch)

    missing = await server.rm_asset_inspect("f" * 32)
    assert missing.isError is True
    assert missing.structuredContent == {
        "ok": False,
        "error": "asset_unavailable",
    }

    data = b"not an image"
    non_image = _persist(
        server,
        data,
        "document.bin",
        "application/octet-stream",
    )
    non_image_result = await server.rm_asset_inspect(non_image["asset_id"])
    assert non_image_result.isError is True
    assert non_image_result.structuredContent["error"] == "asset_not_image"

    unsupported = _persist(
        server,
        _image_bytes("PNG"),
        "unsupported.png",
        "image/png",
    )
    with sqlite3.connect(server.asset_store.db_path) as conn:
        conn.execute(
            "UPDATE assets SET mime_type = 'image/gif' WHERE asset_id = ?",
            (unsupported["asset_id"],),
        )
    unsupported_result = await server.rm_asset_inspect(unsupported["asset_id"])
    assert unsupported_result.isError is True
    assert unsupported_result.structuredContent["error"] == "invalid_image_mime"

    removed = _persist(
        server,
        _image_bytes("PNG"),
        "removed.png",
        "image/png",
    )
    removed_path = server.asset_store.resolve_file(removed["asset_id"])[1]
    removed_path.unlink()
    removed_result = await server.rm_asset_inspect(removed["asset_id"])
    assert removed_result.isError is True
    assert removed_result.structuredContent["error"] == "asset_unavailable"

    for result in (missing, non_image_result, unsupported_result, removed_result):
        serialized = result.model_dump_json(by_alias=True)
        assert "base64" not in serialized
        assert str(server.asset_store.data_root) not in serialized


@pytest.mark.asyncio
async def test_rm_asset_inspect_rejects_corruption_and_limits(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch)

    corrupted = _persist(
        server,
        _image_bytes("JPEG"),
        "corrupted.jpg",
        "image/jpeg",
    )
    corrupted_path = server.asset_store.resolve_file(corrupted["asset_id"])[1]
    corrupted_path.write_bytes(b"x" * corrupted["stored_bytes"])
    corrupted_result = await server.rm_asset_inspect(corrupted["asset_id"])
    assert corrupted_result.isError is True
    assert corrupted_result.structuredContent["error"] == "image_unavailable"

    oversized = _persist(
        server,
        _image_bytes("PNG"),
        "oversized.png",
        "image/png",
    )
    oversized_path = server.asset_store.resolve_file(oversized["asset_id"])[1]
    oversized_path.write_bytes(
        oversized_path.read_bytes()
        + b"x" * (server.RM_ASSET_MAX_UPLOAD_BYTES + 1)
    )
    with sqlite3.connect(server.asset_store.db_path) as conn:
        conn.execute(
            "UPDATE assets SET stored_bytes = ? WHERE asset_id = ?",
            (oversized_path.stat().st_size, oversized["asset_id"]),
        )
    oversized_result = await server.rm_asset_inspect(oversized["asset_id"])
    assert oversized_result.isError is True
    assert oversized_result.structuredContent["error"] == "image_too_large"

    pixel_limited = _persist(
        server,
        _image_bytes("PNG", size=(20, 20)),
        "pixel-limit.png",
        "image/png",
    )
    monkeypatch.setattr(server, "RM_ASSET_MAX_IMAGE_PIXELS", 100)
    pixel_result = await server.rm_asset_inspect(pixel_limited["asset_id"])
    assert pixel_result.isError is True
    assert pixel_result.structuredContent["error"] == "image_too_large"

    for result in (corrupted_result, oversized_result, pixel_result):
        serialized = result.model_dump_json(by_alias=True)
        assert "base64" not in serialized
        assert str(server.asset_store.data_root) not in serialized