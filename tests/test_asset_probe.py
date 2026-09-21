import asyncio
import base64
import concurrent.futures
import hashlib
import importlib
import json
import re
import struct
import sys
import zlib
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_METADATA_CHUNKS = {b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"iCCP"}


def _parse_png(data):
    assert data.startswith(PNG_SIGNATURE)
    offset = len(PNG_SIGNATURE)
    chunks = []
    while offset < len(data):
        assert offset + 12 <= len(data)
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        chunk_type = data[offset + 4:offset + 8]
        chunk_data_start = offset + 8
        chunk_data_end = chunk_data_start + length
        crc_start = chunk_data_end
        crc_end = crc_start + 4
        assert crc_end <= len(data)
        chunk_data = data[chunk_data_start:chunk_data_end]
        expected_crc = struct.unpack(">I", data[crc_start:crc_end])[0]
        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        assert actual_crc == expected_crc
        chunks.append((chunk_type, chunk_data))
        offset = crc_end
        if chunk_type == b"IEND":
            break
    assert offset == len(data)
    return chunks


def _assert_valid_probe_png(data, expected_size=(128, 128), expected_color_type=None):
    chunks = _parse_png(data)
    chunk_types = [chunk_type for chunk_type, _ in chunks]
    assert b"IHDR" in chunk_types
    assert b"IDAT" in chunk_types
    assert b"IEND" in chunk_types
    assert not PNG_METADATA_CHUNKS.intersection(chunk_types)

    ihdr = next(chunk_data for chunk_type, chunk_data in chunks if chunk_type == b"IHDR")
    width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
        ">IIBBBBB", ihdr
    )
    assert (width, height) == expected_size
    assert bit_depth == 8
    if expected_color_type is None:
        assert color_type in (2, 6)
    else:
        assert color_type == expected_color_type
    assert compression == 0
    assert filter_method == 0
    assert interlace == 0

    idat = b"".join(chunk_data for chunk_type, chunk_data in chunks if chunk_type == b"IDAT")
    raw = zlib.decompress(idat)
    channels = 3 if color_type == 2 else 4
    assert len(raw) == height * (1 + width * channels)
    assert chunks[-1][0] == b"IEND"
    return {"width": width, "height": height, "color_type": color_type, "chunks": chunk_types, "raw": raw}


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")

def _download_client(server):
    app = Starlette(routes=[
        Route(
            "/rm/vision-download/{token}",
            server.asset_vision_download_route,
            methods=["GET", "HEAD"],
        )
    ])
    return TestClient(app)


def _browser_upload_client(server):
    app = Starlette(routes=[
        Route(
            "/rm/upload/{token}",
            server.asset_browser_upload_route,
            methods=["GET", "POST"],
        )
    ])
    return TestClient(app)

def _challenge_prompt_and_png(challenge):
    prompt = json.loads(next(block.text for block in challenge.content if getattr(block, "type", None) == "text"))
    image = next(block for block in challenge.content if getattr(block, "type", None) == "image")
    return prompt, base64.b64decode(image.data, validate=True)

@pytest.mark.asyncio
async def test_asset_ingest_probe_accepts_base64_and_hashes_without_files(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    payload = b"phase-0 binary payload"
    encoded = base64.b64encode(payload).decode("ascii")
    expected = hashlib.sha256(payload).hexdigest()
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    result = json.loads(await server.asset_ingest_probe(encoded, expected, "application/octet-stream"))
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    assert result["ok"] is True
    assert result["base64_chars"] == len(encoded)
    assert result["decoded_bytes"] == len(payload)
    assert result["sha256"] == expected
    assert result["expected_sha256"] == expected
    assert result["hash_match"] is True
    assert result["mime_type"] == "application/octet-stream"
    assert after == before


@pytest.mark.asyncio
async def test_asset_ingest_probe_rejects_invalid_base64(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    result = json.loads(await server.asset_ingest_probe("not valid ***"))

    assert result["ok"] is False
    assert result["error"] == "invalid_base64"


@pytest.mark.asyncio
async def test_asset_ingest_probe_rejects_oversized_input(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    result = json.loads(await server.asset_ingest_probe("A" * (server.ASSET_PROBE_MAX_BASE64_CHARS + 1)))

    assert result["ok"] is False
    assert result["error"] == "base64_too_large"
    assert result["max_base64_chars"] == server.ASSET_PROBE_MAX_BASE64_CHARS


@pytest.mark.asyncio
async def test_asset_chunked_ingest_restores_64kb_payload(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    payload = bytes(range(256)) * 256
    expected = hashlib.sha256(payload).hexdigest()
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    begin = json.loads(await server.asset_ingest_begin(
        len(payload), expected, "application/octet-stream", "../unsafe\\probe.bin"
    ))
    assert begin["ok"] is True
    assert begin["recommended_chunk_base64_chars"] == 8192
    assert begin["max_chunk_base64_chars"] == 16384
    assert begin["expires_in_seconds"] == 600
    upload_id = begin["upload_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", upload_id)
    stored_filename = server._asset_ingest_uploads[upload_id]["filename"]
    assert "/" not in stored_filename and "\\" not in stored_filename and ":" not in stored_filename

    decoded_chunk_bytes = server.ASSET_INGEST_RECOMMENDED_CHUNK_BASE64_CHARS // 4 * 3
    chunk_count = 0
    for chunk_index, offset in enumerate(range(0, len(payload), decoded_chunk_bytes)):
        encoded = base64.b64encode(payload[offset:offset + decoded_chunk_bytes]).decode("ascii")
        assert len(encoded) <= server.ASSET_INGEST_RECOMMENDED_CHUNK_BASE64_CHARS
        chunk = json.loads(await server.asset_ingest_chunk(upload_id, chunk_index, encoded))
        assert chunk["ok"] is True
        assert chunk["received_chunks"] == chunk_index + 1
        chunk_count += 1

    finish = json.loads(await server.asset_ingest_finish(upload_id))
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert finish == {
        "ok": True,
        "upload_id": upload_id,
        "decoded_bytes": len(payload),
        "sha256": expected,
        "expected_sha256": expected,
        "size_match": True,
        "hash_match": True,
        "received_chunks": chunk_count,
    }
    assert upload_id not in server._asset_ingest_uploads
    unavailable = json.loads(await server.asset_ingest_chunk(upload_id, chunk_count, "eA=="))
    assert unavailable["error"] == "upload_unavailable"
    assert after == before


@pytest.mark.asyncio
async def test_asset_chunked_ingest_empty_and_chunk_validation(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    empty_hash = hashlib.sha256(b"").hexdigest()
    empty = json.loads(await server.asset_ingest_begin(0, empty_hash))
    finished = json.loads(await server.asset_ingest_finish(empty["upload_id"]))
    assert finished["decoded_bytes"] == 0
    assert finished["received_chunks"] == 0
    assert finished["size_match"] is True
    assert finished["hash_match"] is True

    begin = json.loads(await server.asset_ingest_begin(10, hashlib.sha256(b"0123456789").hexdigest()))
    upload_id = begin["upload_id"]
    invalid = json.loads(await server.asset_ingest_chunk(upload_id, 0, "not base64 ***"))
    oversized = json.loads(await server.asset_ingest_chunk(
        upload_id, 0, "A" * (server.ASSET_INGEST_MAX_CHUNK_BASE64_CHARS + 1)
    ))
    skipped = json.loads(await server.asset_ingest_chunk(upload_id, 1, base64.b64encode(b"abc").decode("ascii")))
    first_data = base64.b64encode(b"abc").decode("ascii")
    first = json.loads(await server.asset_ingest_chunk(upload_id, 0, first_data))
    duplicate = json.loads(await server.asset_ingest_chunk(upload_id, 0, first_data))
    conflict = json.loads(await server.asset_ingest_chunk(
        upload_id, 0, base64.b64encode(b"xyz").decode("ascii")
    ))

    assert invalid["error"] == "invalid_base64"
    assert oversized["error"] == "chunk_too_large"
    assert skipped["error"] == "chunk_out_of_order"
    assert skipped["expected_chunk_index"] == 0
    assert first["idempotent"] is False
    assert duplicate["ok"] is True and duplicate["idempotent"] is True
    assert duplicate["decoded_bytes"] == 3 and duplicate["received_chunks"] == 1
    assert conflict["error"] == "chunk_conflict"


@pytest.mark.asyncio
async def test_asset_chunked_ingest_enforces_two_mib_limit(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    begin = json.loads(await server.asset_ingest_begin(server.ASSET_INGEST_MAX_BYTES, "0" * 64))
    upload_id = begin["upload_id"]
    decoded_chunk_bytes = server.ASSET_INGEST_MAX_CHUNK_BASE64_CHARS // 4 * 3
    remaining = server.ASSET_INGEST_MAX_BYTES
    chunk_index = 0
    while remaining:
        raw = b"x" * min(remaining, decoded_chunk_bytes)
        result = json.loads(await server.asset_ingest_chunk(
            upload_id, chunk_index, base64.b64encode(raw).decode("ascii")
        ))
        assert result["ok"] is True
        remaining -= len(raw)
        chunk_index += 1

    rejected = json.loads(await server.asset_ingest_chunk(upload_id, chunk_index, "eA=="))
    too_large_begin = json.loads(await server.asset_ingest_begin(server.ASSET_INGEST_MAX_BYTES + 1, "0" * 64))
    assert rejected["error"] == "file_too_large"
    assert rejected["max_bytes"] == 2 * 1024 * 1024
    assert too_large_begin["error"] == "file_too_large"
    await server.asset_ingest_abort(upload_id)


@pytest.mark.asyncio
async def test_asset_chunked_ingest_expiry_forgery_abort_and_capacity(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    expected = hashlib.sha256(b"x").hexdigest()
    expired = json.loads(server._asset_begin_ingest_upload(1, expected, now=100.0))
    expired_chunk = json.loads(server._asset_ingest_chunk_data(
        expired["upload_id"], 0, "eA==", now=100.0 + server.ASSET_INGEST_TTL_SECONDS + 1
    ))
    forged = "f" * 32
    forged_finish = json.loads(await server.asset_ingest_finish(forged))
    malformed = json.loads(await server.asset_ingest_finish("../bad"))

    active = json.loads(await server.asset_ingest_begin(1, expected))
    first_abort = json.loads(await server.asset_ingest_abort(active["upload_id"]))
    second_abort = json.loads(await server.asset_ingest_abort(active["upload_id"]))
    after_abort = json.loads(await server.asset_ingest_chunk(active["upload_id"], 0, "eA=="))

    assert expired_chunk["error"] == "upload_unavailable"
    assert forged_finish["error"] == "upload_unavailable"
    assert malformed["error"] == "invalid_upload_id"
    assert first_abort["ok"] is True and first_abort["aborted"] is True
    assert second_abort["ok"] is True and second_abort["aborted"] is False
    assert after_abort["error"] == "upload_unavailable"

    monkeypatch.setattr(server, "ASSET_INGEST_MAX_UPLOADS", 2)
    server._asset_ingest_uploads.clear()
    assert json.loads(server._asset_begin_ingest_upload(1, expected, now=200.0))["ok"] is True
    assert json.loads(server._asset_begin_ingest_upload(1, expected, now=200.0))["ok"] is True
    assert json.loads(server._asset_begin_ingest_upload(1, expected, now=200.0))["error"] == "upload_store_full"
    cleaned = json.loads(server._asset_begin_ingest_upload(
        1, expected, now=200.0 + server.ASSET_INGEST_TTL_SECONDS + 1
    ))
    assert cleaned["ok"] is True
    assert len(server._asset_ingest_uploads) == 1


def test_asset_chunked_ingest_concurrent_duplicate_is_idempotent(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    raw = b"concurrent chunk"
    expected = hashlib.sha256(raw).hexdigest()
    begin = json.loads(server._asset_begin_ingest_upload(len(raw), expected))
    upload_id = begin["upload_id"]
    encoded = base64.b64encode(raw).decode("ascii")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(
            lambda _: json.loads(server._asset_ingest_chunk_data(upload_id, 0, encoded)),
            range(16),
        ))

    assert all(result["ok"] is True for result in results)
    assert sum(1 for result in results if result["idempotent"] is False) == 1
    assert all(result["decoded_bytes"] == len(raw) for result in results)
    assert all(result["received_chunks"] == 1 for result in results)
    finish = json.loads(server._asset_finish_ingest_upload(upload_id))
    assert finish["hash_match"] is True
    assert finish["received_chunks"] == 1

@pytest.mark.parametrize("size", [64 * 1024, 256 * 1024, 512 * 1024, 1024 * 1024, 2 * 1024 * 1024])
@pytest.mark.asyncio
async def test_asset_browser_upload_capacity_and_status(size, tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setenv("OMBRE_PUBLIC_BASE_URL", "https://example.test/base/")
    pattern = bytes(range(251))
    payload = (pattern * (size // len(pattern) + 1))[:size]
    expected = hashlib.sha256(payload).hexdigest()
    link = json.loads(await server.asset_browser_upload_link(
        size, expected, "upload.bin", "application/octet-stream"
    ))

    assert link["ok"] is True
    assert link["max_bytes"] == 2 * 1024 * 1024
    assert link["expires_in_seconds"] == 600
    assert link["upload_url"] == f"https://example.test/base{link['upload_path']}"
    assert "data_base64" not in link
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    with _browser_upload_client(server) as client:
        response = client.post(
            link["upload_path"],
            files={"file": ("upload.bin", payload, "application/octet-stream")},
        )
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    assert response.status_code == 200
    assert str(size) in response.text
    assert expected in response.text
    status = json.loads(await server.asset_browser_upload_status(link["upload_id"]))
    assert status["state"] == "completed"
    assert status["decoded_bytes"] == size
    assert status["sha256"] == expected
    assert status["expected_bytes"] == size
    assert status["expected_sha256"] == expected
    assert status["size_match"] is True
    assert status["hash_match"] is True
    assert status["filename"] == "upload.bin"
    assert status["mime_type"] == "application/octet-stream"
    assert after == before


@pytest.mark.asyncio
async def test_asset_browser_upload_empty_file_and_missing_hash(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    link = json.loads(await server.asset_browser_upload_link(0, "", "empty.bin"))
    with _browser_upload_client(server) as client:
        response = client.post(link["upload_path"], files={"file": ("empty.bin", b"")})

    empty_hash = hashlib.sha256(b"").hexdigest()
    status_text = await server.asset_browser_upload_status(link["upload_id"])
    status = json.loads(status_text)
    assert response.status_code == 200
    assert status["state"] == "completed"
    assert status["decoded_bytes"] == 0
    assert status["sha256"] == empty_hash
    assert status["expected_sha256"] == ""
    assert status["size_match"] is True
    assert status["hash_match"] is False
    assert "data_base64" not in status_text


@pytest.mark.asyncio
async def test_asset_browser_upload_stops_over_limit_and_keeps_no_file(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    link = json.loads(await server.asset_browser_upload_link(server.ASSET_BROWSER_UPLOAD_MAX_BYTES, ""))
    payload = b"x" * (server.ASSET_BROWSER_UPLOAD_MAX_BYTES + 1)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    with _browser_upload_client(server) as client:
        response = client.post(link["upload_path"], files={"file": ("too-large.bin", payload)})
        retry_page = client.get(link["upload_path"])
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    assert response.status_code == 413
    assert retry_page.status_code == 200
    status = json.loads(await server.asset_browser_upload_status(link["upload_id"]))
    assert status["state"] == "pending"
    assert status["decoded_bytes"] == 0
    assert after == before


@pytest.mark.asyncio
async def test_asset_browser_upload_security_filename_and_wrong_hash(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    payload = b"browser upload metadata only"
    marker = payload.decode("ascii")
    wrong_hash = "0" * 64
    link_text = await server.asset_browser_upload_link(
        len(payload), wrong_hash, "../../private\\probe.bin", "text/plain\r\nunsafe"
    )
    link = json.loads(link_text)
    assert "data_base64" not in link_text

    with _browser_upload_client(server) as client:
        page = client.get(link["upload_path"])
        response = client.post(link["upload_path"], files={"file": ("probe.bin", payload, "text/plain")})

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["x-frame-options"] == "DENY"
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "<script" not in page.text.lower()
    assert "http://" not in page.text and "https://" not in page.text
    assert "../" not in page.text and "..\\" not in page.text
    assert response.status_code == 200

    status_text = await server.asset_browser_upload_status(link["upload_id"])
    status = json.loads(status_text)
    assert status["state"] == "completed"
    assert status["size_match"] is True
    assert status["hash_match"] is False
    assert "/" not in status["filename"] and "\\" not in status["filename"] and ":" not in status["filename"]
    assert "\r" not in status["mime_type"] and "\n" not in status["mime_type"]
    assert marker not in status_text
    assert "data_base64" not in status_text


@pytest.mark.asyncio
async def test_asset_browser_upload_rejects_forged_expired_and_reused_tokens(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    forged_path = "/rm/upload/" + "A" * 43
    with _browser_upload_client(server) as client:
        assert client.get(forged_path).status_code == 404
        assert client.post(forged_path, files={"file": ("x.bin", b"x")}).status_code == 404

    expired = json.loads(await server.asset_browser_upload_link(1, hashlib.sha256(b"x").hexdigest()))
    server._asset_browser_uploads[expired["upload_id"]]["expires_at"] = 0
    server._asset_browser_uploads[expired["upload_id"]]["retire_at"] = 10 ** 20
    with _browser_upload_client(server) as client:
        assert client.get(expired["upload_path"]).status_code == 404
    expired_status = json.loads(await server.asset_browser_upload_status(expired["upload_id"]))
    assert expired_status["state"] == "expired"

    valid = json.loads(await server.asset_browser_upload_link(1, hashlib.sha256(b"x").hexdigest()))
    with _browser_upload_client(server) as client:
        first = client.post(valid["upload_path"], files={"file": ("x.bin", b"x")})
        second = client.post(valid["upload_path"], files={"file": ("x.bin", b"x")})
        after = client.get(valid["upload_path"])
    assert first.status_code == 200
    assert second.status_code == 404
    assert after.status_code == 404


@pytest.mark.asyncio
async def test_asset_browser_upload_concurrent_token_and_active_limit(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    payload = b"z" * (256 * 1024)
    expected = hashlib.sha256(payload).hexdigest()
    link = json.loads(await server.asset_browser_upload_link(len(payload), expected, "parallel.bin"))

    def upload_once(_):
        with _browser_upload_client(server) as client:
            return client.post(link["upload_path"], files={"file": ("parallel.bin", payload)}).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(upload_once, range(8)))
    assert statuses.count(200) == 1
    assert statuses.count(404) == 7
    completed = json.loads(await server.asset_browser_upload_status(link["upload_id"]))
    assert completed["state"] == "completed"
    assert completed["hash_match"] is True

    monkeypatch.setattr(server, "ASSET_BROWSER_UPLOAD_MAX_UPLOADS", 2)
    server._asset_browser_uploads.clear()
    server._asset_browser_upload_tokens.clear()
    first = json.loads(server._asset_create_browser_upload_link(1, expected, now=100.0))
    second = json.loads(server._asset_create_browser_upload_link(1, expected, now=100.0))
    full = json.loads(server._asset_create_browser_upload_link(1, expected, now=100.0))
    assert first["ok"] is True and second["ok"] is True
    assert full["error"] == "upload_store_full"
    cleaned = json.loads(server._asset_create_browser_upload_link(
        1, expected, now=100.0 + server.ASSET_BROWSER_UPLOAD_TTL_SECONDS + 1
    ))
    assert cleaned["ok"] is True

@pytest.mark.asyncio
async def test_asset_render_probe_returns_valid_png_image_block(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    result = await server.asset_render_probe()
    assert isinstance(result, server.CallToolResult)
    image_blocks = [block for block in result.content if getattr(block, "type", None) == "image"]
    text_blocks = [block for block in result.content if getattr(block, "type", None) == "text"]

    assert len(image_blocks) == 1
    image = image_blocks[0]
    assert image.mimeType == "image/png"
    decoded = base64.b64decode(image.data, validate=True)
    disk_bytes = Path(server.ASSET_PROBE_PATH).read_bytes()
    assert decoded == disk_bytes
    info = _assert_valid_probe_png(decoded)
    assert info["width"] == 128
    assert info["height"] == 128
    assert Path(server.ASSET_PROBE_PATH).name == "probe.png"
    assert all(image.data not in getattr(block, "text", "") for block in text_blocks)

@pytest.mark.asyncio
async def test_asset_export_probe_returns_user_visible_base64_payload(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    result_text = await server.asset_export_probe()
    payload = json.loads(result_text)

    assert payload["ok"] is True
    assert payload["filename"] == "remember-me-probe.png"
    assert payload["mime_type"] == "image/png"
    decoded = base64.b64decode(payload["data_base64"], validate=True)
    disk_bytes = Path(server.ASSET_PROBE_PATH).read_bytes()
    assert decoded == disk_bytes
    assert payload["decoded_bytes"] == len(disk_bytes)
    assert payload["sha256"] == hashlib.sha256(disk_bytes).hexdigest()
    assert _assert_valid_probe_png(decoded)["chunks"] == [b"IHDR", b"IDAT", b"IEND"]

    render_result = await server.asset_render_probe()
    render_image = next(block for block in render_result.content if getattr(block, "type", None) == "image")
    assert base64.b64decode(render_image.data, validate=True) == decoded
    assert str(Path.cwd()) not in result_text
    assert str(Path.home()) not in result_text
    assert not re.search(r"[A-Za-z]:[\\/]", result_text)
    assert "/".join(["", "app", "assets"]) not in result_text
    assert "data:image/" not in payload["data_base64"]

@pytest.mark.asyncio
async def test_asset_vision_challenge_returns_blind_text_and_png(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    result = await server.asset_vision_challenge()
    assert isinstance(result, server.CallToolResult)
    image_blocks = [block for block in result.content if getattr(block, "type", None) == "image"]
    text_blocks = [block for block in result.content if getattr(block, "type", None) == "text"]
    assert len(image_blocks) == 1
    assert len(text_blocks) == 1

    prompt = json.loads(text_blocks[0].text)
    assert set(prompt) == {
        "allowed_colors",
        "allowed_symbol_positions",
        "allowed_symbols",
        "answer_format",
        "decoded_bytes",
        "sha256",
        "submit_to",
        "trial_id",
    }
    assert len(prompt["trial_id"]) == 32
    assert prompt["answer_format"] == {
        "top_left": "<color>",
        "top_right": "<color>",
        "bottom_left": "<color>",
        "bottom_right": "<color>",
        "symbol": "<symbol>",
        "symbol_position": "<position>",
    }
    assert prompt["submit_to"] == "asset_vision_verify"
    assert set(prompt["allowed_colors"]) == set(server.ASSET_VISION_COLORS)
    assert set(prompt["allowed_symbols"]) == set(server.ASSET_VISION_SYMBOLS)
    assert set(prompt["allowed_symbol_positions"]) == set(server.ASSET_VISION_POSITIONS)
    assert prompt["trial_id"] in server._asset_vision_trials

    trial_answer = server._asset_vision_trials[prompt["trial_id"]]["answer"]
    prompt_text = text_blocks[0].text
    for position in server.ASSET_VISION_POSITIONS:
        assert f'"{position}": "{trial_answer[position]}"' not in prompt_text
    assert f'"symbol": "{trial_answer["symbol"]}"' not in prompt_text
    assert f'"symbol_position": "{trial_answer["symbol_position"]}"' not in prompt_text

    image = image_blocks[0]
    assert image.mimeType == "image/png"
    decoded = base64.b64decode(image.data, validate=True)
    info = _assert_valid_probe_png(decoded, expected_size=(256, 256), expected_color_type=2)
    assert info["chunks"] == [b"IHDR", b"IDAT", b"IEND"]
    assert prompt["decoded_bytes"] == len(decoded)
    assert prompt["sha256"] == hashlib.sha256(decoded).hexdigest()
    assert server._asset_vision_trials[prompt["trial_id"]]["sha256"] == prompt["sha256"]
    assert server._asset_vision_trials[prompt["trial_id"]]["exported"] is False


@pytest.mark.asyncio
async def test_asset_vision_export_matches_challenge_png_and_keeps_trial_verifiable(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    challenge = await server.asset_vision_challenge()
    prompt = json.loads(next(block.text for block in challenge.content if getattr(block, "type", None) == "text"))
    image = next(block for block in challenge.content if getattr(block, "type", None) == "image")
    challenge_png = base64.b64decode(image.data, validate=True)

    export_text = await server.asset_vision_export(prompt["trial_id"])
    exported = json.loads(export_text)
    exported_png = base64.b64decode(exported["data_base64"], validate=True)
    answer = dict(server._asset_vision_trials[prompt["trial_id"]]["answer"])
    verify = json.loads(await server.asset_vision_verify(prompt["trial_id"], json.dumps(answer)))

    assert exported["ok"] is True
    assert exported["trial_id"] == prompt["trial_id"]
    assert exported["filename"] == f"remember-me-vision-{prompt['trial_id']}.png"
    assert re.fullmatch(r"remember-me-vision-[0-9a-f]{32}\.png", exported["filename"])
    assert exported["mime_type"] == "image/png"
    assert exported["decoded_bytes"] == len(challenge_png) == prompt["decoded_bytes"]
    assert exported["sha256"] == hashlib.sha256(challenge_png).hexdigest() == prompt["sha256"]
    assert exported_png == challenge_png
    assert _assert_valid_probe_png(exported_png, expected_size=(256, 256), expected_color_type=2)["chunks"] == [b"IHDR", b"IDAT", b"IEND"]
    assert "answer" not in exported
    assert "field_results" not in exported
    assert "top_left" not in export_text
    assert verify["ok"] is True
    assert verify["score"] == 6


@pytest.mark.asyncio
async def test_asset_vision_upload_challenge_exports_and_verifies_without_image_or_answer(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    result_text = await server.asset_vision_upload_challenge()
    payload = json.loads(result_text)

    assert payload["ok"] is True
    assert set(payload) == {
        "allowed_colors",
        "allowed_symbol_positions",
        "allowed_symbols",
        "answer_format",
        "decoded_bytes",
        "ok",
        "sha256",
        "trial_id",
    }
    assert len(payload["trial_id"]) == 32
    assert re.fullmatch(r"[0-9a-f]{32}", payload["trial_id"])
    assert not hasattr(result_text, "content")
    assert "ImageContent" not in result_text
    assert "data_base64" not in result_text
    assert set(payload["allowed_colors"]) == set(server.ASSET_VISION_COLORS)
    assert set(payload["allowed_symbols"]) == set(server.ASSET_VISION_SYMBOLS)
    assert set(payload["allowed_symbol_positions"]) == set(server.ASSET_VISION_POSITIONS)
    assert payload["answer_format"] == {
        "top_left": "<color>",
        "top_right": "<color>",
        "bottom_left": "<color>",
        "bottom_right": "<color>",
        "symbol": "<symbol>",
        "symbol_position": "<position>",
    }

    trial = server._asset_vision_trials[payload["trial_id"]]
    answer = dict(trial["answer"])
    for position in server.ASSET_VISION_POSITIONS:
        assert f'"{position}": "{answer[position]}"' not in result_text
    assert f'"symbol": "{answer["symbol"]}"' not in result_text
    assert f'"symbol_position": "{answer["symbol_position"]}"' not in result_text

    export = json.loads(await server.asset_vision_export(payload["trial_id"]))
    exported_png = base64.b64decode(export["data_base64"], validate=True)
    verify = json.loads(await server.asset_vision_verify(payload["trial_id"], json.dumps(answer)))

    assert export["ok"] is True
    assert export["decoded_bytes"] == payload["decoded_bytes"] == len(exported_png)
    assert export["sha256"] == payload["sha256"] == hashlib.sha256(exported_png).hexdigest()
    assert exported_png == trial["png"]
    assert verify["ok"] is True
    assert verify["score"] == 6
    assert payload["trial_id"] not in server._asset_vision_trials

@pytest.mark.asyncio
async def test_asset_vision_download_link_returns_signed_path_and_http_png(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.delenv("OMBRE_PUBLIC_BASE_URL", raising=False)
    challenge = await server.asset_vision_challenge()
    prompt, challenge_png = _challenge_prompt_and_png(challenge)
    answer = server._asset_vision_trials[prompt["trial_id"]]["answer"]

    result_text = await server.asset_vision_download_link(prompt["trial_id"])
    payload = json.loads(result_text)
    token = payload["download_path"].rsplit("/", 1)[-1]
    repeat = json.loads(await server.asset_vision_download_link(prompt["trial_id"]))

    assert payload["ok"] is True
    assert set(payload) == {
        "decoded_bytes",
        "download_path",
        "download_url",
        "expires_in_seconds",
        "filename",
        "mime_type",
        "ok",
        "sha256",
        "trial_id",
    }
    assert payload["trial_id"] == prompt["trial_id"]
    assert payload["filename"] == f"remember-me-vision-{prompt['trial_id']}.png"
    assert re.fullmatch(r"remember-me-vision-[0-9a-f]{32}\.png", payload["filename"])
    assert payload["mime_type"] == "image/png"
    assert payload["decoded_bytes"] == len(challenge_png) == prompt["decoded_bytes"]
    assert payload["sha256"] == hashlib.sha256(challenge_png).hexdigest() == prompt["sha256"]
    assert payload["download_url"] == ""
    assert re.fullmatch(r"/rm/vision-download/[A-Za-z0-9_-]{43,128}", payload["download_path"])
    assert prompt["trial_id"] not in token
    assert repeat["download_path"] == payload["download_path"]
    assert 0 <= repeat["expires_in_seconds"] <= payload["expires_in_seconds"] <= server.ASSET_VISION_DOWNLOAD_TTL_SECONDS
    assert "data_base64" not in result_text
    assert "ImageContent" not in result_text
    for position in server.ASSET_VISION_POSITIONS:
        assert f'"{position}": "{answer[position]}"' not in result_text
    assert f'"symbol": "{answer["symbol"]}"' not in result_text
    assert f'"symbol_position": "{answer["symbol_position"]}"' not in result_text

    response = _download_client(server).get(payload["download_path"])

    assert response.status_code == 200
    assert response.content == challenge_png
    assert response.headers["content-length"] == str(payload["decoded_bytes"])
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"] == f'attachment; filename="{payload["filename"]}"'
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_asset_vision_download_head_and_get_limit(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    challenge = await server.asset_vision_challenge()
    prompt, challenge_png = _challenge_prompt_and_png(challenge)
    payload = json.loads(await server.asset_vision_download_link(prompt["trial_id"]))
    token = payload["download_path"].rsplit("/", 1)[-1]
    client = _download_client(server)

    head = client.head(payload["download_path"])
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(len(challenge_png))
    assert server._asset_vision_download_tokens[token]["get_count"] == 0

    responses = [client.get(payload["download_path"]) for _ in range(server.ASSET_VISION_DOWNLOAD_MAX_GETS)]
    fourth = client.get(payload["download_path"])

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert all(response.content == challenge_png for response in responses)
    assert fourth.status_code == 404


@pytest.mark.asyncio
async def test_asset_vision_download_rejects_invalid_expired_and_verified_tokens(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    client = _download_client(server)
    assert client.get("/rm/vision-download/not-a-valid-token").status_code == 404
    assert client.get("/rm/vision-download/../escape").status_code == 404
    assert client.get("/rm/vision-download/" + "a" * 43).status_code == 404

    expired = server._asset_new_vision_trial(now=100.0)
    assert server._asset_store_vision_trial(expired, now=100.0) == (True, "")
    expired_link = json.loads(server._asset_create_vision_download_link(expired["trial_id"], now=100.0))
    monkeypatch.setattr(server.time, "time", lambda: 100.0 + server.ASSET_VISION_DOWNLOAD_TTL_SECONDS + 1)
    assert client.get(expired_link["download_path"]).status_code == 404

    monkeypatch.setattr(server.time, "time", lambda: 200.0)
    trial = server._asset_new_vision_trial(now=200.0)
    assert server._asset_store_vision_trial(trial, now=200.0) == (True, "")
    link = json.loads(server._asset_create_vision_download_link(trial["trial_id"], now=200.0))
    verified = json.loads(await server.asset_vision_verify(trial["trial_id"], json.dumps(trial["answer"])))
    assert verified["ok"] is True
    assert client.get(link["download_path"]).status_code == 404


@pytest.mark.asyncio
async def test_asset_vision_download_public_base_url_and_store_limit_cleanup(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setenv("OMBRE_PUBLIC_BASE_URL", "https://example.test/base/")
    first = server._asset_new_vision_trial(now=100.0)
    assert server._asset_store_vision_trial(first, now=100.0) == (True, "")
    first_link = json.loads(server._asset_create_vision_download_link(first["trial_id"], now=100.0))
    assert first_link["download_url"] == f"https://example.test/base{first_link['download_path']}"

    monkeypatch.setattr(server, "ASSET_VISION_MAX_DOWNLOAD_TOKENS", 1)
    second = server._asset_new_vision_trial(now=110.0)
    assert server._asset_store_vision_trial(second, now=110.0) == (True, "")
    full = json.loads(server._asset_create_vision_download_link(second["trial_id"], now=110.0))
    assert full == {"ok": False, "trial_id": second["trial_id"], "error": "download_store_full"}

    third = server._asset_new_vision_trial(now=100.0 + server.ASSET_VISION_DOWNLOAD_TTL_SECONDS + 1)
    assert server._asset_store_vision_trial(third, now=100.0 + server.ASSET_VISION_DOWNLOAD_TTL_SECONDS + 1) == (True, "")
    cleaned = json.loads(server._asset_create_vision_download_link(third["trial_id"], now=100.0 + server.ASSET_VISION_DOWNLOAD_TTL_SECONDS + 1))
    assert cleaned["ok"] is True


@pytest.mark.asyncio
async def test_asset_vision_download_concurrent_link_and_get_are_limited(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    trial = server._asset_new_vision_trial()
    assert server._asset_store_vision_trial(trial) == (True, "")

    def create_link():
        return json.loads(server._asset_create_vision_download_link(trial["trial_id"]))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        links = list(executor.map(lambda _: create_link(), range(8)))

    paths = {link["download_path"] for link in links if link.get("ok") is True}
    assert len(paths) == 1
    path = next(iter(paths))
    token = path.rsplit("/", 1)[-1]
    assert len(server._asset_vision_download_tokens) == 1
    assert token in server._asset_vision_download_tokens

    client = _download_client(server)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(lambda _: client.get(path).status_code, range(8)))

    assert statuses.count(200) == server.ASSET_VISION_DOWNLOAD_MAX_GETS
    assert statuses.count(404) == 8 - server.ASSET_VISION_DOWNLOAD_MAX_GETS

@pytest.mark.asyncio
async def test_asset_vision_export_rejects_second_verified_expired_missing_and_invalid_trials(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    challenge = await server.asset_vision_challenge()
    trial_id = json.loads(next(block.text for block in challenge.content if getattr(block, "type", None) == "text"))["trial_id"]
    answer = dict(server._asset_vision_trials[trial_id]["answer"])

    first_export = json.loads(await server.asset_vision_export(trial_id))
    second_export = json.loads(await server.asset_vision_export(trial_id))
    verify = json.loads(await server.asset_vision_verify(trial_id, json.dumps(answer)))
    verified_export = json.loads(await server.asset_vision_export(trial_id))

    expired = server._asset_new_vision_trial(now=100.0)
    server._asset_store_vision_trial(expired, now=100.0)
    expired_export = json.loads(server._asset_export_vision_trial(expired["trial_id"], now=100.0 + server.ASSET_VISION_TTL_SECONDS + 1))
    missing_export = json.loads(await server.asset_vision_export("0" * 32))
    invalid_export = json.loads(await server.asset_vision_export("../" + trial_id))

    assert first_export["ok"] is True
    assert second_export == {"ok": False, "trial_id": trial_id, "error": "already_exported"}
    assert verify["ok"] is True
    assert verified_export == {"ok": False, "trial_id": trial_id, "error": "trial_unavailable"}
    assert expired_export == {"ok": False, "trial_id": expired["trial_id"], "error": "trial_unavailable"}
    assert missing_export == {"ok": False, "trial_id": "0" * 32, "error": "trial_unavailable"}
    assert invalid_export["ok"] is False
    assert invalid_export["error"] == "invalid_trial_id"
    assert "filename" not in invalid_export


@pytest.mark.asyncio
async def test_asset_vision_export_concurrent_single_success_and_safe_filename(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    challenge = await server.asset_vision_challenge()
    trial_id = json.loads(next(block.text for block in challenge.content if getattr(block, "type", None) == "text"))["trial_id"]

    results = await asyncio.gather(*(server.asset_vision_export(trial_id) for _ in range(8)))
    parsed = [json.loads(result) for result in results]
    successes = [result for result in parsed if result.get("ok") is True]
    failures = [result for result in parsed if result.get("ok") is False]

    assert len(successes) == 1
    assert len(failures) == 7
    assert all(result["error"] == "already_exported" for result in failures)
    assert successes[0]["filename"] == f"remember-me-vision-{trial_id}.png"
    assert "/" not in successes[0]["filename"]
    assert "\\" not in successes[0]["filename"]
    assert ".." not in successes[0]["filename"]


@pytest.mark.asyncio
async def test_asset_vision_verify_scores_correct_and_consumes_trial(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    trial = server._asset_new_vision_trial()
    ok, error = server._asset_store_vision_trial(trial)
    assert ok, error

    result = json.loads(await server.asset_vision_verify(trial["trial_id"], json.dumps(trial["answer"])))

    assert result == {
        "ok": True,
        "trial_id": trial["trial_id"],
        "score": 6,
        "max_score": 6,
        "all_correct": True,
        "field_results": {
            "top_left": True,
            "top_right": True,
            "bottom_left": True,
            "bottom_right": True,
            "symbol": True,
            "symbol_position": True,
        },
    }
    assert trial["trial_id"] not in server._asset_vision_trials
    assert not set(result).intersection(set(server.ASSET_VISION_COLORS))


@pytest.mark.asyncio
async def test_asset_vision_verify_scores_partial_answer(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    trial = server._asset_new_vision_trial()
    server._asset_store_vision_trial(trial)
    answer = dict(trial["answer"])
    answer["top_left"] = next(color for color in server.ASSET_VISION_COLORS if color != answer["top_left"])

    result = json.loads(await server.asset_vision_verify(trial["trial_id"], json.dumps(answer)))

    assert result["ok"] is True
    assert result["score"] == 5
    assert result["max_score"] == 6
    assert result["all_correct"] is False
    assert result["field_results"]["top_left"] is False
    assert all(result["field_results"][key] for key in result["field_results"] if key != "top_left")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_answer,expected_error",
    [
        ("not json", "invalid_json"),
        (json.dumps({}), "invalid_fields"),
        (json.dumps({"top_left": "red", "top_right": "green", "bottom_left": "blue", "bottom_right": "orange", "symbol": "circle"}), "invalid_fields"),
        (json.dumps({"top_left": "red", "top_right": "green", "bottom_left": "blue", "bottom_right": "orange", "symbol": "circle", "symbol_position": "top_left", "extra": "no"}), "invalid_fields"),
        (json.dumps({"top_left": 1, "top_right": "green", "bottom_left": "blue", "bottom_right": "orange", "symbol": "circle", "symbol_position": "top_left"}), "invalid_field_type"),
        (json.dumps({"top_left": "cyan", "top_right": "green", "bottom_left": "blue", "bottom_right": "orange", "symbol": "circle", "symbol_position": "top_left"}), "invalid_enum"),
    ],
)
async def test_asset_vision_verify_rejects_invalid_answers_and_consumes_trial(tmp_path, monkeypatch, bad_answer, expected_error):
    server = _load_server(tmp_path, monkeypatch)
    trial = server._asset_new_vision_trial()
    server._asset_store_vision_trial(trial)

    result = json.loads(await server.asset_vision_verify(trial["trial_id"], bad_answer))
    second = json.loads(await server.asset_vision_verify(trial["trial_id"], json.dumps(trial["answer"])))

    assert result["ok"] is False
    assert result["error"] == expected_error
    assert "field_results" not in result
    assert second["ok"] is False
    assert second["error"] == "trial_unavailable"


@pytest.mark.asyncio
async def test_asset_vision_verify_second_submit_expired_and_missing_fail(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    trial = server._asset_new_vision_trial()
    server._asset_store_vision_trial(trial)

    first = json.loads(await server.asset_vision_verify(trial["trial_id"], json.dumps(trial["answer"])))
    second = json.loads(await server.asset_vision_verify(trial["trial_id"], json.dumps(trial["answer"])))
    expired = server._asset_new_vision_trial(now=100.0)
    server._asset_store_vision_trial(expired, now=100.0)
    expired_result = json.loads(server._asset_score_vision_answer(expired["trial_id"], json.dumps(expired["answer"]), now=100.0 + server.ASSET_VISION_TTL_SECONDS + 1))
    missing = json.loads(await server.asset_vision_verify("missing-trial", json.dumps(trial["answer"])))

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["error"] == "trial_unavailable"
    assert expired_result["ok"] is False
    assert expired_result["error"] == "trial_unavailable"
    assert missing["ok"] is False
    assert missing["error"] == "trial_unavailable"


@pytest.mark.asyncio
async def test_asset_vision_concurrent_create_and_verify_are_safe(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    challenges = await asyncio.gather(*(server.asset_vision_challenge() for _ in range(20)))
    trial_ids = [json.loads(next(block.text for block in result.content if getattr(block, "type", None) == "text"))["trial_id"] for result in challenges]
    assert len(set(trial_ids)) == 20

    trial = server._asset_new_vision_trial()
    server._asset_store_vision_trial(trial)
    results = await asyncio.gather(*(server.asset_vision_verify(trial["trial_id"], json.dumps(trial["answer"])) for _ in range(8)))
    parsed = [json.loads(result) for result in results]
    assert sum(1 for result in parsed if result.get("ok") is True) == 1
    assert sum(1 for result in parsed if result.get("error") == "trial_unavailable") == 7


def test_asset_vision_trial_limit_and_expired_cleanup(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "ASSET_VISION_MAX_TRIALS", 2)
    server._asset_vision_trials.clear()

    first = server._asset_new_vision_trial(now=100.0)
    second = server._asset_new_vision_trial(now=100.0)
    third = server._asset_new_vision_trial(now=100.0)
    assert server._asset_store_vision_trial(first, now=100.0) == (True, "")
    assert server._asset_store_vision_trial(second, now=100.0) == (True, "")
    assert server._asset_store_vision_trial(third, now=100.0) == (False, "trial_store_full")

    server._asset_vision_trials.clear()
    expired = server._asset_new_vision_trial(now=100.0)
    fresh = server._asset_new_vision_trial(now=100.0 + server.ASSET_VISION_TTL_SECONDS + 1)
    assert server._asset_store_vision_trial(expired, now=100.0) == (True, "")
    assert server._asset_store_vision_trial(fresh, now=100.0 + server.ASSET_VISION_TTL_SECONDS + 1) == (True, "")
    assert list(server._asset_vision_trials) == [fresh["trial_id"]]
