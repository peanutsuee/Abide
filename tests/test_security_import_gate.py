import asyncio
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from maintenance_write_gate import DEFAULT_WRITE_COORDINATOR


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("OMBRE_HOOK_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_AUTH_TOKEN", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


class _Request:
    method = "POST"

    def __init__(self, body=None):
        self._body = body or {}

    async def json(self):
        return self._body


@pytest.mark.security
def test_import_review_and_pause_are_rejected_before_any_mutation_during_freeze(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_require_auth", lambda _request: None)
    update = AsyncMock()
    server.bucket_mgr = SimpleNamespace(
        update=update,
        _find_bucket_file=lambda _bucket_id: None,
    )
    pause = Mock()
    server.import_engine = SimpleNamespace(is_running=True, pause=pause)
    review_request = _Request({
        "decisions": [{"bucket_id": "bucket-1", "action": "important"}]
    })
    pause_request = _Request()

    async def exercise():
        async with DEFAULT_WRITE_COORDINATOR.freeze(
            reason="import_gate_test",
            drain_timeout_seconds=1,
            max_freeze_seconds=5,
        ):
            review = await server.api_import_review(review_request)
            paused = await server.api_import_pause(pause_request)
            return review, paused

    review, paused = asyncio.run(exercise())
    assert review.status_code == 503
    assert review.body == b'{"error":"maintenance_in_progress"}'
    assert paused.status_code == 503
    assert paused.body == b'{"error":"maintenance_in_progress"}'
    update.assert_not_awaited()
    pause.assert_not_called()


@pytest.mark.security
def test_import_review_and_pause_work_when_maintenance_is_open(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_require_auth", lambda _request: None)
    monkeypatch.setattr(
        server, "_require_dashboard_write", lambda _request, _route: None
    )
    update = AsyncMock()
    server.bucket_mgr = SimpleNamespace(
        update=update,
        _find_bucket_file=lambda _bucket_id: None,
    )
    pause = Mock()
    server.import_engine = SimpleNamespace(is_running=True, pause=pause)

    async def exercise():
        review = await server.api_import_review(_Request({
            "decisions": [{"bucket_id": "bucket-1", "action": "important"}]
        }))
        paused = await server.api_import_pause(_Request())
        return review, paused

    review, paused = asyncio.run(exercise())
    assert review.status_code == 200
    assert review.body == b'{"applied":1,"errors":0}'
    assert paused.status_code == 200
    assert paused.body == b'{"status":"pause_requested"}'
    update.assert_awaited_once_with("bucket-1", importance=9)
    pause.assert_called_once_with()


@pytest.mark.security
def test_import_review_reports_partial_delete_failures(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_require_auth", lambda _request: None)
    monkeypatch.setattr(
        server, "_require_dashboard_write", lambda _request, _route: None
    )
    delete = AsyncMock(side_effect=[True, False])
    server.bucket_mgr = SimpleNamespace(delete=delete)

    async def exercise():
        return await server.api_import_review(_Request({
            "decisions": [
                {"bucket_id": "bucket-1", "action": "delete"},
                {"bucket_id": "bucket-2", "action": "delete"},
            ]
        }))

    response = asyncio.run(exercise())

    assert response.status_code == 409
    assert response.body == b'{"applied":1,"errors":1}'
    assert delete.await_args_list == [call("bucket-1"), call("bucket-2")]


@pytest.mark.security
def test_import_review_and_pause_keep_custom_route_outermost():
    source = open("server.py", encoding="utf-8").read()
    for route in ("/api/import/review", "/api/import/pause"):
        start = source.index(f'@mcp.custom_route("{route}"')
        end = source.index("async def ", start)
        decorators = source[start:end]
        assert decorators.index("@mcp.custom_route") < decorators.index(
            "@guarded_http_mutation"
        )
