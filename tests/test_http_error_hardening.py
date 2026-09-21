import asyncio
import importlib
import json
import sys

import pytest
from starlette.datastructures import Headers, QueryParams
from starlette.requests import Request
from unittest.mock import AsyncMock, Mock


INJECTED = "SYNTHETIC_INTERNAL_DETAIL C:/synthetic/provider-db-secret"


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("OMBRE_HOOK_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def _request(method, path, *, query="", body=b"", headers=None):
    raw_headers = [(b"host", b"testserver")]
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode(), value.encode()))
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "headers": raw_headers,
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 123),
            "root_path": "",
            "http_version": "1.1",
        },
        receive=receive,
    )


def _json_request(method, path, payload):
    return _request(
        method,
        path,
        body=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )


@pytest.mark.security
@pytest.mark.parametrize(
    ("path", "handler_name", "expected", "kwargs"),
    [
        ("/health", "health_check", {"status": "error", "detail": "service_unavailable"}, {}),
        ("/api/search", "api_search", {"error": "search_failed"}, {"params": {"q": "needle"}}),
        ("/api/network", "api_network", {"error": "network_failed"}, {}),
        ("/api/breath-debug", "api_breath_debug", {"error": "breath_debug_failed"}, {}),
        ("/api/import/patterns", "api_import_patterns", {"error": "pattern_detection_failed"}, {}),
        ("/api/import/results", "api_import_results", {"error": "import_results_failed"}, {}),
        ("/api/status", "api_system_status", {"error": "status_unavailable"}, {}),
    ],
)
def test_dashboard_http_errors_are_bounded(
    tmp_path, monkeypatch, path, handler_name, expected, kwargs
):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_require_auth", lambda request: None)

    if handler_name == "health_check":
        server.decay_engine.ensure_started = AsyncMock(
            side_effect=RuntimeError(INJECTED)
        )
    elif handler_name == "api_search":
        server.bucket_mgr.search = AsyncMock(side_effect=RuntimeError(INJECTED))
    elif handler_name in {"api_network", "api_breath_debug", "api_import_results"}:
        server.bucket_mgr.list_all = AsyncMock(side_effect=RuntimeError(INJECTED))
    elif handler_name == "api_import_patterns":
        server.import_engine.detect_patterns = AsyncMock(
            side_effect=RuntimeError(INJECTED)
        )
    elif handler_name == "api_system_status":
        server.bucket_mgr.get_stats = AsyncMock(side_effect=RuntimeError(INJECTED))

    query = "q=needle" if kwargs.get("params") else ""
    response = asyncio.run(
        getattr(server, handler_name)(_request("GET", path, query=query))
    )

    assert response.status_code == 500
    response_text = response.body.decode("utf-8")
    assert json.loads(response_text) == expected
    assert INJECTED not in response_text
    assert "synthetic/provider-db-secret" not in response_text


@pytest.mark.security
def test_dashboard_status_reports_current_release_version(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_require_auth", lambda request: None)
    server.bucket_mgr.get_stats = AsyncMock(
        return_value={
            "permanent_count": 0,
            "dynamic_count": 0,
            "archive_count": 0,
        }
    )

    response = asyncio.run(
        server.api_system_status(_request("GET", "/api/status"))
    )

    assert response.status_code == 200
    assert json.loads(response.body)["version"] == "1.4.0"


@pytest.mark.security
def test_config_persistence_error_is_bounded(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_require_dashboard_write", lambda request, route: None)

    def fail_open(*args, **kwargs):
        raise OSError(INJECTED)

    monkeypatch.setattr(server, "open", fail_open, raising=False)
    response = asyncio.run(
        server.api_config_update(
            _json_request("POST", "/api/config", {"persist": True})
        )
    )

    assert response.status_code == 500
    response_text = response.body.decode("utf-8")
    assert json.loads(response_text) == {"error": "persist_failed", "updated": []}
    assert INJECTED not in response_text


@pytest.mark.security
def test_host_vault_write_error_is_bounded(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_require_dashboard_write", lambda request, route: None)
    monkeypatch.setattr(server, "_write_env_var", Mock(side_effect=OSError(INJECTED)))

    response = asyncio.run(
        server.api_host_vault_set(
            _json_request("POST", "/api/host-vault", {"value": "/safe/vault"})
        )
    )

    assert response.status_code == 500
    response_text = response.body.decode("utf-8")
    assert json.loads(response_text) == {"error": "env_write_failed"}
    assert INJECTED not in response_text


@pytest.mark.security
def test_import_upload_read_error_is_bounded(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_require_dashboard_write", lambda request, route: None)
    server.import_engine._running = False

    class BrokenRequest:
        method = "POST"
        headers = Headers({"content-type": "multipart/form-data; boundary=synthetic"})
        query_params = QueryParams()

        async def form(self):
            raise RuntimeError(INJECTED)

    response = asyncio.run(server.api_import_upload(BrokenRequest()))

    assert response.status_code == 400
    assert response.body == b'{"error":"upload_read_failed"}'
    assert INJECTED not in response.body.decode()


@pytest.mark.security
def test_import_status_redacts_persisted_exception_details(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_require_auth", lambda request: None)
    server.import_engine.state.data["errors"] = [INJECTED]

    response = asyncio.run(
        server.api_import_status(_request("GET", "/api/import/status"))
    )

    response_text = response.body.decode("utf-8")
    payload = json.loads(response_text)
    assert response.status_code == 200
    assert payload["errors"] == ["import_failed"]
    assert INJECTED not in response_text


@pytest.mark.security
@pytest.mark.parametrize(
    ("path", "handler_name", "operation", "expected"),
    [
        ("/api/backup/export", "backup_export_endpoint", "backup", "backup_export_failed"),
        ("/api/embeddings/backfill", "embeddings_backfill_endpoint", "backfill", "embedding_backfill_failed"),
    ],
)
def test_backup_http_errors_are_bounded(
    tmp_path, monkeypatch, path, handler_name, operation, expected
):
    server = _load_server(tmp_path, monkeypatch)
    sys.modules.pop("backup_entry", None)
    backup_entry = importlib.import_module("backup_entry")
    monkeypatch.setattr(
        backup_entry,
        "_authenticated_claims",
        AsyncMock(return_value={"repository": "synthetic", "run_id": "synthetic"}),
    )
    if operation == "backup":
        monkeypatch.setattr(
            backup_entry, "backup_payload_json", Mock(side_effect=OSError(INJECTED))
        )
        method = "GET"
    elif operation == "backfill":
        monkeypatch.setattr(
            backup_entry, "backfill_batch", AsyncMock(side_effect=RuntimeError(INJECTED))
        )
        method = "POST"
    request = (
        _json_request(method, path, {})
        if method == "POST"
        else _request(method, path)
    )
    response = asyncio.run(getattr(backup_entry, handler_name)(request))

    assert response.status_code == 500
    response_text = response.body.decode("utf-8")
    assert json.loads(response_text) == {"error": expected}
    assert INJECTED not in response_text


@pytest.mark.security
def test_asset_ingest_invalid_base64_has_no_library_detail(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    result = asyncio.run(server.asset_ingest_probe("not valid ***"))

    assert result.startswith('{"base64_chars":')
    assert "invalid_base64" in result
    assert "detail" not in result
    assert "Only base64 data is allowed" not in result


@pytest.mark.security
@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("update", "asset_unavailable"),
        ("search", "search_unavailable"),
        ("reindex", "asset_unavailable"),
    ],
)
def test_asset_mcp_errors_are_bounded(tmp_path, monkeypatch, operation, expected):
    server = _load_server(tmp_path, monkeypatch)
    if operation == "update":
        server.asset_store.update_metadata = Mock(
            side_effect=server.AssetStoreError(INJECTED)
        )
        raw = asyncio.run(server.rm_asset_update_metadata("a" * 32, title="safe"))
    elif operation == "search":
        server.asset_store.search = Mock(side_effect=server.AssetStoreError(INJECTED))
        raw = asyncio.run(server.rm_asset_search(query="safe"))
    else:
        server.asset_embedding_index.reindex = AsyncMock(
            side_effect=RuntimeError(INJECTED)
        )
        raw = asyncio.run(server.rm_asset_reindex_embeddings())

    assert raw == '{"error": "%s", "ok": false}' % expected
    assert INJECTED not in raw


@pytest.mark.security
def test_mcp_maintenance_and_memory_errors_are_bounded(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server._with_response_seal = lambda value: value

    server._run_digest = AsyncMock(side_effect=RuntimeError(INJECTED))
    digest_result = asyncio.run(server.digest())

    server._run_related_backfill = AsyncMock(side_effect=RuntimeError(INJECTED))
    related_result = asyncio.run(server.related_backfill())

    server._detect_conflict_warning = AsyncMock(return_value="")
    server.dehydrator.digest = AsyncMock(side_effect=RuntimeError(INJECTED))
    grow_result = asyncio.run(
        server.grow("A synthetic diary entry long enough for digest")
    )

    server.bucket_mgr.get_stats = AsyncMock(side_effect=RuntimeError(INJECTED))
    pulse_result = asyncio.run(server.pulse())

    server.bucket_mgr.list_all = AsyncMock(side_effect=RuntimeError(INJECTED))
    breath_result = asyncio.run(server.breath(importance_min=5))

    for result in (digest_result, related_result, grow_result, pulse_result, breath_result):
        assert INJECTED not in result
    assert digest_result == "自动消化失败。"
    assert related_result == "自动 related 回填失败。"
    assert grow_result == "日记整理失败。"
    assert pulse_result == "获取系统状态失败。"
    assert breath_result == "记忆系统暂时无法访问。"
