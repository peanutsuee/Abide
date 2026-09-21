import importlib
import sys
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_breath_filters_search_results_by_updated_date(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: content
    )

    inside_id = await server.bucket_mgr.create(content="date needle inside")
    outside_id = await server.bucket_mgr.create(content="date needle outside")
    inside = await server.bucket_mgr.get(inside_id)
    outside = await server.bucket_mgr.get(outside_id)
    inside["metadata"]["updated_at"] = "2026-06-10"
    outside["metadata"]["updated_at"] = "2026-05-01"
    server.bucket_mgr.search = AsyncMock(return_value=[inside, outside])
    server.bucket_mgr.list_all = AsyncMock(return_value=[])

    result = await server.breath(
        query="date needle",
        date_from="2026-06-01",
        date_to="2026-06-12",
    )

    assert inside_id in result
    assert outside_id not in result


@pytest.mark.asyncio
async def test_breath_rejects_invalid_date_range(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)

    assert "YYYY-MM-DD" in await server.breath(date_from="2026/06/01")
    assert "cannot be later" in await server.breath(
        date_from="2026-06-12",
        date_to="2026-06-01",
    )
