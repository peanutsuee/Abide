import hashlib
import importlib
import io
import sqlite3
import sys
from pathlib import Path

import pytest

from PIL import Image
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from asset_backend import AssetBackendError
from asset_dashboard import AssetDashboardError, AssetDashboardService


def _png(color=(20, 40, 60), size=(48, 32)):
    output = io.BytesIO()
    with Image.new("RGB", size, color) as image:
        image.save(output, format="PNG")
    return output.getvalue()


def _persist(store, data, filename="image.png", mime_type="image/png"):
    source = store.create_temp_path()
    source.write_bytes(data)
    return store.persist_upload(
        source,
        hashlib.sha256(data).hexdigest(),
        len(data),
        filename,
        mime_type,
    )


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "test-password")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def _client(server, authenticated=True, base_url="http://testserver"):
    app = Starlette(routes=[
        Route("/api/assets", server.api_assets, methods=["GET", "POST"]),
        Route("/api/assets/{asset_id}", server.api_asset_detail, methods=["GET", "PATCH", "DELETE"]),
        Route(
            "/api/assets/{asset_id}/thumbnail",
            server.api_asset_thumbnail,
            methods=["GET"],
        ),
        Route(
            "/api/assets/{asset_id}/image",
            server.api_asset_image,
            methods=["GET", "HEAD"],
        ),
        Route("/api/buckets", server.api_buckets, methods=["GET"]),
        Route("/api/archives", server.api_archives, methods=["GET"]),
        Route("/dashboard", server.dashboard, methods=["GET"]),
        Route(
            "/dashboard-assets.js",
            server.dashboard_assets_script,
            methods=["GET"],
        ),
        Route(
            "/dashboard-assets.css",
            server.dashboard_assets_styles,
            methods=["GET"],
        ),
    ])
    client = TestClient(app, base_url=base_url)
    if authenticated:
        token = server._create_session()
        client.cookies.set("ombre_session", token)
        client.headers.update({
            "Origin": base_url.rstrip("/"),
            "X-Ombre-CSRF": server._sessions[token]["csrf_token"],
        })
    return client


def test_asset_routes_require_dashboard_auth(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    assert _client(server, authenticated=False).get("/api/assets").status_code == 401


def test_asset_list_search_pagination_and_safe_shape(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    first = _persist(server.asset_store, _png(), "first-secret-name.png")
    second = _persist(
        server.asset_store,
        _png((80, 30, 20)),
        "second.png",
    )
    server.asset_store.update_metadata(
        first["asset_id"],
        title="山间记录",
        description="一段中文描述",
        tags=["旅行", "收藏"],
    )
    server.asset_store.update_metadata(
        second["asset_id"],
        title="Other",
        description="Unrelated",
        tags=["archive"],
    )
    client = _client(server)

    page = client.get("/api/assets?limit=1&offset=0")
    assert page.status_code == 200
    payload = page.json()
    assert payload["total"] == 2
    assert len(payload["results"]) == 1

    for query in ("山间", "中文描述", "first-secret-name"):
        result = client.get("/api/assets", params={"q": query}).json()
        assert [item["asset_id"] for item in result["results"]] == [first["asset_id"]]
    tagged = client.get("/api/assets", params={"tag": "旅行"}).json()
    assert [item["asset_id"] for item in tagged["results"]] == [first["asset_id"]]

    safe = tagged["results"][0]
    assert safe["thumbnail_url"].endswith("/thumbnail")
    assert safe["image_url"].endswith("/image")
    forbidden = {
        "stored_relpath",
        "stored_sha256",
        "source_sha256",
        "decoded_bytes",
        "base64",
        "token",
    }
    assert not forbidden.intersection(safe)
    assert str(server.asset_store.data_root) not in page.text


def test_asset_routes_validate_pagination_and_missing_asset(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    client = _client(server)

    for query in ("limit=0", "limit=51", "offset=-1", "limit=nope"):
        response = client.get("/api/assets?" + query)
        assert response.status_code == 400
        assert response.json() == {"error": "invalid_pagination"}
    missing = "0" * 32
    assert client.get(f"/api/assets/{missing}").status_code == 404
    assert client.get(f"/api/assets/{missing}/image").status_code == 404


def test_asset_image_and_thumbnail_use_cleaned_registered_file(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    asset = _persist(server.asset_store, _png(size=(600, 400)), "large.png")
    resolved = server.asset_store.resolve_file(asset["asset_id"])
    stored_bytes = resolved[1].read_bytes()
    client = _client(server)

    image = client.get(f"/api/assets/{asset['asset_id']}/image")
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/png")
    assert image.headers["x-content-type-options"] == "nosniff"
    assert image.content == stored_bytes

    thumbnail = client.get(f"/api/assets/{asset['asset_id']}/thumbnail")
    assert thumbnail.status_code == 200
    assert thumbnail.headers["content-type"].startswith("image/png")
    with Image.open(io.BytesIO(thumbnail.content)) as opened:
        assert opened.width <= 360
        assert opened.height <= 240


def test_asset_image_rejects_tampered_path_without_leaking_it(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    asset = _persist(server.asset_store, _png(), "safe.png")
    with sqlite3.connect(server.asset_store.db_path) as conn:
        conn.execute(
            "UPDATE assets SET stored_relpath = ? WHERE asset_id = ?",
            ("../private.png", asset["asset_id"]),
        )
    response = _client(server).get(f"/api/assets/{asset['asset_id']}/image")

    assert response.status_code == 404
    assert response.json() == {"error": "asset_unavailable"}
    assert ".." not in response.text
    assert str(tmp_path) not in response.text


def test_empty_asset_library(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    assert _client(server).get("/api/assets").json() == {
        "total": 0,
        "offset": 0,
        "limit": 20,
        "results": [],
    }


def test_archives_are_separate_from_active_buckets(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    active_id = "active-bucket"
    archive_id = "session-archive"
    active = {
        "id": active_id,
        "content": "active content",
        "metadata": {
            "name": "active",
            "type": "dynamic",
            "domain": ["daily"],
            "created": "2026-07-20T10:00:00+00:00",
        },
    }
    archive = {
        "id": archive_id,
        "content": "archived conversation",
        "metadata": {
            "name": "session archive",
            "type": "archived",
            "domain": ["session"],
            "tags": ["session", "archive"],
            "created": "2026-07-21T10:00:00+00:00",
        },
    }

    async def fake_list(include_archive=False):
        return [active, archive] if include_archive else [active]

    monkeypatch.setattr(server.bucket_mgr, "list_all", fake_list)
    client = _client(server)
    buckets = client.get("/api/buckets").json()
    archives = client.get("/api/archives").json()

    assert [item["id"] for item in buckets] == [active_id]
    assert [item["id"] for item in archives["results"]] == [archive_id]


def test_dashboard_static_contract_has_three_sections_and_safe_asset_ui(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch)
    client = _client(server)
    html = client.get("/dashboard").text
    script = client.get("/dashboard-assets.js").text
    css = client.get("/dashboard-assets.css")

    assert 'data-tab="list">记忆桶<' in html
    assert 'data-tab="archives">归档对话<' in html
    assert 'data-tab="assets">图片资产<' in html
    assert 'id="archives-view"' in html
    assert 'id="assets-view"' in html
    assert "没有符合搜索条件的归档对话" in html
    assert "图片库还是空的" in script
    assert "没有符合当前搜索条件的图片" in script
    assert "缩略图无法加载" in script
    assert "rm-asset-detail" in script
    assert "innerHTML" not in script
    assert "stored_relpath" not in script
    assert "base64" not in script.lower()
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")


def _jpeg(color=(90, 70, 50), size=(48, 32)):
    output = io.BytesIO()
    with Image.new("RGB", size, color) as image:
        image.save(output, format="JPEG")
    return output.getvalue()


def _upload(client, data, filename="upload.png", mime_type="image/png", **fields):
    form = {
        "title": fields.get("title", ""),
        "description": fields.get("description", ""),
        "tags": fields.get("tags", "[]"),
    }
    return client.post(
        "/api/assets",
        data=form,
        files={"file": (filename, data, mime_type)},
    )


def test_asset_writes_require_auth_csrf_and_same_origin(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    payload = _png()
    assert _upload(_client(server, authenticated=False), payload).status_code == 401

    client = _client(server)
    csrf = client.headers.pop("X-Ombre-CSRF")
    response = _upload(client, payload)
    assert response.status_code == 403
    assert response.json() == {"error": "csrf_required"}

    client.headers["X-Ombre-CSRF"] = "wrong-token"
    response = _upload(client, payload)
    assert response.status_code == 403
    assert response.json() == {"error": "csrf_required"}

    client.headers["X-Ombre-CSRF"] = csrf
    client.headers.pop("Origin")
    response = _upload(client, payload)
    assert response.status_code == 403
    assert response.json() == {"error": "same_origin_required"}


def test_dashboard_write_origin_accepts_direct_and_proxy_https(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    direct = _client(server, base_url="https://direct.example")
    assert _upload(direct, _png()).status_code == 201

    proxy = _client(server)
    proxy.headers.update({
        "Origin": "https://public.example",
        "Host": "public.example",
        "X-Forwarded-Proto": "https",
    })
    assert _upload(proxy, _png((30, 60, 90))).status_code == 201

    forwarded_host = _client(server)
    forwarded_host.headers.update({
        "Origin": "https://public.example",
        "Host": "internal.service:8000",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "public.example",
    })
    assert _upload(forwarded_host, _png((90, 60, 30))).status_code == 201


def test_dashboard_write_origin_rejects_mismatch_and_proxy_chains(
    tmp_path, monkeypatch, caplog
):
    server = _load_server(tmp_path, monkeypatch)
    client = _client(server)
    csrf = client.headers["X-Ombre-CSRF"]

    cases = [
        {"Origin": "https://other.example", "Host": "public.example", "X-Forwarded-Proto": "https"},
        {"Origin": "https://public.example", "Host": "public.example", "X-Forwarded-Proto": "https, http"},
        {"Origin": "https://public.example", "Host": "internal.service", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "public.example, other.example"},
    ]
    for headers in cases:
        guarded = _client(server)
        guarded.headers.clear()
        guarded.headers.update(headers)
        guarded.headers["X-Ombre-CSRF"] = csrf
        guarded.cookies.update(client.cookies)
        response = _upload(guarded, _png())
        assert response.status_code == 403
        assert response.json() == {"error": "same_origin_required"}

    log_text = caplog.text
    assert "route=/api/assets status=403 code=same_origin_required" in log_text
    assert csrf not in log_text
    assert "public.example" not in log_text


def test_png_jpeg_upload_metadata_dedup_and_temp_cleanup(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    client = _client(server)

    png = _upload(
        client,
        _png(),
        "../unsafe:name.png",
        "image/png",
        title="保存的图片",
        description="安全描述",
        tags='["旅行", "收藏"]',
    )
    assert png.status_code == 201
    created = png.json()
    assert created["filename"] == "_unsafe_name.png"
    assert created["title"] == "保存的图片"
    assert created["description"] == "安全描述"
    assert created["tags"] == ["收藏", "旅行"]
    assert created["mime_type"] == "image/png"
    assert created["deduplicated"] is False

    duplicate = _upload(client, _png(), "again.png", "image/png")
    assert duplicate.status_code == 200
    assert duplicate.json()["asset_id"] == created["asset_id"]
    assert duplicate.json()["deduplicated"] is True
    assert duplicate.json()["title"] == "保存的图片"

    jpeg = _upload(client, _jpeg(), "photo.jpg", "image/jpeg")
    assert jpeg.status_code == 201
    assert jpeg.json()["mime_type"] == "image/jpeg"
    assert not list(server.asset_store.temp_dir.glob("rm-*"))


def test_upload_limits_corruption_and_mime_mismatch(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    client = _client(server)

    exact = _png() + b"\0" * (server.RM_ASSET_MAX_UPLOAD_BYTES - len(_png()))
    response = _upload(client, exact)
    assert response.status_code == 201

    too_large = exact + b"x"
    response = _upload(client, too_large)
    assert response.status_code == 413
    assert "file_too_large" in response.text

    mismatch = _upload(client, _png(), "wrong.jpg", "image/jpeg")
    assert mismatch.status_code == 422
    assert mismatch.json() == {"error": "image_mime_mismatch"}

    corrupt = _upload(client, b"not-an-image", "broken.png", "image/png")
    assert corrupt.status_code == 422
    assert "invalid_image" in corrupt.text

    unsupported = _upload(client, b"plain", "plain.txt", "text/plain")
    assert unsupported.status_code == 415
    assert "unsupported_image" in unsupported.text


def test_upload_rejects_pixel_limit(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    client = _client(server)
    monkeypatch.setattr("asset_store.MAX_IMAGE_PIXELS", 10)
    response = _upload(client, _png(size=(4, 3)))
    assert response.status_code == 422
    assert response.json() == {"error": "image_pixel_limit"}


def test_metadata_edit_validation_and_embedding_refresh(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    client = _client(server)
    created = _upload(client, _png()).json()
    calls = []

    async def fake_index(asset):
        calls.append(asset["asset_id"])
        return "indexed"

    monkeypatch.setattr(server.asset_embedding_index, "index_asset", fake_index)
    response = client.patch(
        f"/api/assets/{created['asset_id']}",
        json={"title": "新标题", "description": "新描述", "tags": ["一", "二"]},
    )
    assert response.status_code == 200
    assert response.json()["title"] == "新标题"
    assert calls == [created["asset_id"]]

    assert client.patch(
        f"/api/assets/{created['asset_id']}", json={"filename": "nope"}
    ).status_code == 400
    assert client.patch(
        f"/api/assets/{created['asset_id']}", json={"title": 123}
    ).status_code == 400
    assert client.patch(
        f"/api/assets/{created['asset_id']}", json={"title": "x" * 201}
    ).status_code == 400
    assert client.patch(
        "/api/assets/" + "0" * 32, json={"title": "missing"}
    ).status_code == 404


def test_delete_removes_file_tags_embedding_and_routes(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    client = _client(server)
    created = _upload(client, _png(), tags='["delete-me"]', title="delete").json()
    asset_id = created["asset_id"]
    resolved = server.asset_store.resolve_file(asset_id)
    stored_path = resolved[1]
    with sqlite3.connect(server.asset_store.db_path) as conn:
        conn.execute(
            "INSERT INTO asset_embeddings (asset_id, embedding, model, content_hash, updated_at) VALUES (?, ?, ?, ?, ?)",
            (asset_id, "[0.1]", "test", "hash", "2026-07-24T00:00:00+00:00"),
        )

    response = client.delete(f"/api/assets/{asset_id}")
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert not stored_path.exists()
    assert client.get(f"/api/assets/{asset_id}").status_code == 404
    assert client.get(f"/api/assets/{asset_id}/image").status_code == 404
    with sqlite3.connect(server.asset_store.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM asset_tags WHERE asset_id = ?", (asset_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM asset_embeddings WHERE asset_id = ?", (asset_id,)).fetchone()[0] == 0


def test_delete_failure_and_traversal_do_not_claim_success(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    client = _client(server)
    created = _upload(client, _png()).json()
    asset_id = created["asset_id"]

    with sqlite3.connect(server.asset_store.db_path) as conn:
        conn.execute("UPDATE assets SET stored_relpath = ? WHERE asset_id = ?", ("../outside.png", asset_id))
    response = client.delete(f"/api/assets/{asset_id}")
    assert response.status_code == 409
    assert str(tmp_path) not in response.text
    assert server.asset_store.get(asset_id) is not None

    monkeypatch.setattr(
        server.asset_dashboard,
        "delete_asset",
        lambda _asset_id: (_ for _ in ()).throw(server.AssetDashboardError("asset_delete_failed", 409)),
    )
    response = client.delete(f"/api/assets/{asset_id}")
    assert response.status_code == 409
    assert response.json() == {"error": "asset_delete_failed"}


def test_stage5b_static_contract_and_import_scope(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    client = _client(server)
    html = client.get("/dashboard").text
    script = client.get("/dashboard-assets.js").text

    assert 'id="rm-asset-upload-open"' in html
    assert "上传图片" in html
    assert 'accept = "image/png,image/jpeg,.png,.jpg,.jpeg"' in script
    assert "new FormData()" in script
    assert 'method: "PATCH"' in script
    assert 'method: "DELETE"' in script
    assert "确认删除" in script
    assert "删除后将从 Remember-Me 图片库中永久移除" in script
    assert "data.append(\"file\"" in script
    assert "base64" not in script.lower()
    assert "uploadErrorMessage(error.code, error.status)" in script
    assert "登录验证已过期，请刷新页面后重试。" in script
    assert "上传请求未通过同源安全校验，请刷新页面后重试。" in script
    assert "服务器处理上传时出错，请稍后重试。" in script
    assert "上传失败，请检查图片格式、大小和网络后重试。" not in script
    assert 'accept=".json,.txt,.md,.jsonl"' in html
    assert 'id="import-file-input"' in html

def test_invalid_upload_metadata_cleans_temp_without_persisting(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    client = _client(server)
    response = _upload(client, _png(), tags='{"not": "a list"}')
    assert response.status_code == 400
    assert server.asset_store.search(kind="image")["total"] == 0
    assert not list(server.asset_store.temp_dir.glob("rm-*"))

    response = _upload(client, _png(), title="x" * 201)
    assert response.status_code == 400
    assert server.asset_store.search(kind="image")["total"] == 0
    assert not list(server.asset_store.temp_dir.glob("rm-*"))


def test_embedding_failure_does_not_rollback_dashboard_write(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    client = _client(server)

    async def fail_index(_asset):
        raise RuntimeError("synthetic embedding failure")

    monkeypatch.setattr(server.asset_embedding_index, "index_asset", fail_index)
    uploaded = _upload(client, _png(), title="kept")
    assert uploaded.status_code == 201
    asset_id = uploaded.json()["asset_id"]
    assert server.asset_store.get(asset_id)["title"] == "kept"

    updated = client.patch(
        f"/api/assets/{asset_id}",
        json={"description": "still updated"},
    )
    assert updated.status_code == 200
    assert server.asset_store.get(asset_id)["description"] == "still updated"


class _GateProbeBackend:
    name = "rm"

    def __init__(self, frozen):
        self.frozen = frozen
        self.calls = []

    def assert_public_mutation_allowed(self):
        self.calls.append("gate")
        if self.frozen:
            raise AssetBackendError("asset_write_frozen")

    def get(self, asset_id):
        self.calls.append(("get", asset_id))
        return None


@pytest.mark.parametrize("method", ["update_asset", "delete_asset"])
def test_dashboard_missing_id_is_rejected_by_frozen_gate_before_lookup(method):
    backend = _GateProbeBackend(frozen=True)
    service = AssetDashboardService(
        backend_provider=lambda: backend,
        max_asset_bytes=1024,
    )

    with pytest.raises(AssetDashboardError) as raised:
        if method == "update_asset":
            service.update_asset("0" * 32, {"title": "probe"})
        else:
            service.delete_asset("0" * 32)

    assert raised.value.code == "asset_write_frozen"
    assert raised.value.status_code == 409
    assert backend.calls == ["gate"]


@pytest.mark.parametrize("method", ["update_asset", "delete_asset"])
def test_dashboard_missing_id_keeps_open_state_not_found_semantics(method):
    backend = _GateProbeBackend(frozen=False)
    service = AssetDashboardService(
        backend_provider=lambda: backend,
        max_asset_bytes=1024,
    )

    with pytest.raises(AssetDashboardError) as raised:
        if method == "update_asset":
            service.update_asset("0" * 32, {"title": "probe"})
        else:
            service.delete_asset("0" * 32)

    assert raised.value.code == "asset_not_found"
    assert raised.value.status_code == 404
    assert backend.calls == ["gate", ("get", "0" * 32)]
