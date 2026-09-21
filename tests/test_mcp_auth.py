import importlib
import logging
import sys
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


def _load_server(monkeypatch):
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def _app_with_auth(server):
    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/mcp", ok, methods=["GET", "POST"]),
            Route("/mcp/sub", ok, methods=["GET", "POST"]),
            Route("/sse", ok, methods=["GET"]),
            Route("/messages", ok, methods=["POST"]),
            Route("/health", ok),
            Route("/dashboard", ok),
            Route("/auth/login", ok, methods=["POST"]),
        ]
    )
    server.add_mcp_auth_middleware(app)
    return app


def _sse_app_with_auth(server):
    app = server.mcp.sse_app()
    server.add_mcp_auth_middleware(app)
    return app


def test_mcp_fails_closed_when_token_unset_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", raising=False)
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/mcp").status_code == 401
    assert client.post("/mcp/sub").status_code == 401
    assert client.get("/health").status_code == 200


def test_mcp_anonymous_requires_explicit_opt_in_and_warns(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", "true")
    server = _load_server(monkeypatch)

    with caplog.at_level("WARNING"):
        client = TestClient(_app_with_auth(server))

    assert client.get("/mcp").status_code == 200
    assert any(
        "SECURITY WARNING" in record.getMessage()
        and "OMBRE_MCP_ALLOW_ANONYMOUS_HTTP" in record.getMessage()
        for record in caplog.records
    )


def test_mcp_requires_token_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_AUTH_TOKEN", "test-token")
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/mcp").status_code == 401
    assert client.get("/mcp", headers={"Authorization": "Bearer bad"}).status_code == 401
    assert client.get("/mcp", headers={"Authorization": "Bearer test-token"}).status_code == 200
    assert client.post("/mcp/sub?token=test-token").status_code == 401


def test_query_token_is_rejected_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_QUERY_TOKEN", raising=False)
    monkeypatch.setenv("OMBRE_MCP_QUERY_TOKEN", "query-secret")
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/mcp?token=query-secret").status_code == 401


def test_query_token_requires_dedicated_token_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", raising=False)
    monkeypatch.setenv("OMBRE_MCP_ALLOW_QUERY_TOKEN", "true")
    monkeypatch.delenv("OMBRE_MCP_QUERY_TOKEN", raising=False)
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/mcp?token=query-secret").status_code == 401


def test_query_token_rejects_wrong_dedicated_token(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", raising=False)
    monkeypatch.setenv("OMBRE_MCP_ALLOW_QUERY_TOKEN", "true")
    monkeypatch.setenv("OMBRE_MCP_QUERY_TOKEN", "query-secret")
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/mcp?token=wrong").status_code == 401


def test_query_token_accepts_dedicated_token_when_explicitly_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", raising=False)
    monkeypatch.setenv("OMBRE_MCP_ALLOW_QUERY_TOKEN", "true")
    monkeypatch.setenv("OMBRE_MCP_QUERY_TOKEN", "query-secret")
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/mcp?token=query-secret").status_code == 200


def test_bearer_token_is_not_reused_as_query_token(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_AUTH_TOKEN", "bearer-secret")
    monkeypatch.setenv("OMBRE_MCP_ALLOW_QUERY_TOKEN", "true")
    monkeypatch.delenv("OMBRE_MCP_QUERY_TOKEN", raising=False)
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/mcp?token=bearer-secret").status_code == 401

    monkeypatch.setenv("OMBRE_MCP_QUERY_TOKEN", "bearer-secret")
    server = _load_server(monkeypatch)
    assert TestClient(_app_with_auth(server)).get("/mcp?token=bearer-secret").status_code == 200


def test_non_mcp_paths_remain_exempt_with_token(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_AUTH_TOKEN", "test-token")
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/health").status_code == 200
    assert client.get("/dashboard").status_code == 200
    assert client.post("/auth/login").status_code == 200


def test_sse_mcp_routes_fail_closed_without_token(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", raising=False)
    server = _load_server(monkeypatch)

    client = TestClient(_sse_app_with_auth(server))

    assert client.get("/sse").status_code == 401
    assert client.post("/messages").status_code == 401
    assert client.get("/health").status_code == 200


def test_sse_mcp_message_route_accepts_bearer(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_AUTH_TOKEN", "test-token")
    server = _load_server(monkeypatch)

    client = TestClient(_sse_app_with_auth(server))

    assert client.post(
        "/messages",
        headers={"Authorization": "Bearer test-token"},
    ).status_code != 401


def test_sse_and_message_mcp_paths_accept_dedicated_query_token(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", raising=False)
    monkeypatch.setenv("OMBRE_MCP_ALLOW_QUERY_TOKEN", "true")
    monkeypatch.setenv("OMBRE_MCP_QUERY_TOKEN", "query-secret")
    server = _load_server(monkeypatch)

    client = TestClient(_app_with_auth(server))

    assert client.get("/sse?token=query-secret").status_code == 200
    assert client.post("/messages?token=query-secret").status_code == 200


def test_sse_mcp_message_route_allows_explicit_anonymous_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", "true")
    server = _load_server(monkeypatch)

    client = TestClient(_sse_app_with_auth(server))

    assert client.post("/messages").status_code != 401


def test_query_token_warning_does_not_include_secret(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", raising=False)
    monkeypatch.setenv("OMBRE_MCP_ALLOW_QUERY_TOKEN", "true")
    monkeypatch.setenv("OMBRE_MCP_QUERY_TOKEN", "query-secret")

    with caplog.at_level("WARNING"):
        server = _load_server(monkeypatch)
        _app_with_auth(server)

    assert "query-token compatibility enabled" in caplog.text
    assert "query-secret" not in caplog.text


def test_uvicorn_access_log_filter_redacts_only_query_token(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    server = _load_server(monkeypatch)
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1:1234",
            "GET",
            "/mcp?mode=stream&token=plain-secret&after=kept",
            "1.1",
            200,
        ),
        None,
    )

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.filters.clear()
    server.install_uvicorn_access_log_redaction()
    server.install_uvicorn_access_log_redaction()
    assert len(access_logger.filters) == 1
    assert access_logger.filters[0].filter(record) is True

    rendered = record.getMessage()
    assert "plain-secret" not in rendered
    assert "/mcp?mode=stream&token=[redacted]&after=kept" in rendered


def test_http_cors_origins_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_MCP_ALLOW_ANONYMOUS_HTTP", raising=False)
    monkeypatch.delenv("OMBRE_HTTP_ALLOWED_ORIGINS", raising=False)
    server = _load_server(monkeypatch)

    app = _app_with_auth(server)
    server.add_http_cors_middleware(app)
    client = TestClient(app)
    response = client.get("/health", headers={"Origin": "https://evil.example"})
    assert response.headers.get("access-control-allow-origin") is None

    monkeypatch.setenv(
        "OMBRE_HTTP_ALLOWED_ORIGINS",
        "https://one.example, https://two.example",
    )
    app = _app_with_auth(server)
    server.add_http_cors_middleware(app)
    client = TestClient(app)

    allowed = client.get("/health", headers={"Origin": "https://two.example"})
    denied = client.get("/health", headers={"Origin": "https://other.example"})
    assert allowed.headers.get("access-control-allow-origin") == "https://two.example"
    assert denied.headers.get("access-control-allow-origin") is None


def test_both_http_entrypoints_use_shared_cors_policy():
    root = Path(__file__).parents[1]
    server_source = (root / "server.py").read_text(encoding="utf-8")
    backup_source = (root / "backup_entry.py").read_text(encoding="utf-8")

    assert "add_http_cors_middleware(_app)" in server_source
    assert "server.add_http_cors_middleware(app)" in backup_source
    assert "install_uvicorn_access_log_redaction()" in server_source
    assert "server.install_uvicorn_access_log_redaction()" in backup_source
    assert 'allow_origins=["*"]' not in server_source
    assert 'allow_origins=["*"]' not in backup_source
