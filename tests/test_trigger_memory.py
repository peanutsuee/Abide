import importlib
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.analyze = AsyncMock(
        return_value={
            "domain": ["trigger-test"],
            "valence": 0.5,
            "arousal": 0.3,
            "tags": ["trigger"],
            "suggested_name": "trigger-memory",
        }
    )
    return server


@pytest.mark.asyncio
async def test_boot_surfaces_due_trigger_once_per_day(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    today = datetime.now().date().isoformat()
    result = await server.hold(
        "remember this on trigger day",
        trigger_date=today,
    )
    bucket_id = result.split("→")[-1].split()[0] if "→" in result else ""
    if not bucket_id:
        bucket_id = (await server.bucket_mgr.list_all(include_archive=False))[0]["id"]

    first = await server.boot()
    second = await server.boot()
    bucket = await server.bucket_mgr.get(bucket_id)

    assert "boot: 今日浮现" in first
    assert bucket_id in first
    assert bucket_id not in second
    assert bucket["metadata"]["trigger_last_seen"] == today


@pytest.mark.asyncio
async def test_trace_sets_trigger_and_resolved_hides_it(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    bucket_id = await server.bucket_mgr.create(content="trace trigger body")

    await server.trace(bucket_id, trigger_date=yesterday)
    first = await server.boot()
    await server.trace(bucket_id, resolved=1)
    second = await server.boot()

    assert bucket_id in first
    assert bucket_id not in second


@pytest.mark.asyncio
async def test_invalid_trigger_date_is_rejected(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    hold_result = await server.hold("invalid trigger", trigger_date="2026/07/20")
    trace_result = await server.trace("missing", trigger_date="July 20")

    assert "trigger_date must use YYYY-MM-DD format" in hold_result
    assert "trigger_date must use YYYY-MM-DD format" in trace_result
