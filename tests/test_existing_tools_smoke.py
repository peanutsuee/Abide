import importlib
import sys
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_all_existing_tools_still_run_in_isolated_storage(tmp_path, monkeypatch):
    buckets_dir = tmp_path / "buckets"
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(buckets_dir))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    sys.modules.pop("backup_entry", None)
    server = importlib.import_module("server")
    backup_entry = importlib.import_module("backup_entry")

    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: content[:120]
    )
    server.dehydrator.analyze = AsyncMock(
        return_value={
            "domain": ["regression"],
            "valence": 0.5,
            "arousal": 0.3,
            "tags": ["smoke"],
            "suggested_name": "smoke-memory",
        }
    )

    hold_result = await server.hold(
        "isolated hold regression memory",
        tags="smoke",
        importance=5,
    )
    grow_result = await server.grow("isolated grow")
    buckets = await server.bucket_mgr.list_all(include_archive=False)
    trace_result = await server.trace(buckets[0]["id"], importance=6)
    breath_result = await server.breath(query="isolated", max_results=2)
    pulse_result = await server.pulse()
    dream_result = await server.dream()
    archive_result = await server.archive_session("isolated archive regression")

    for index in range(3):
        await server.bucket_mgr.create(
            content=f"pinned control {index}",
            tags=["pinned"],
            domain=["regression"],
            pinned=True,
        )
    regular_ids = [
        await server.bucket_mgr.create(
            content=f"needle result {index}",
            tags=["needle"],
            domain=["regression"],
        )
        for index in range(4)
    ]
    limited_search = await server.breath(query="needle", max_results=2)

    for result in [
        breath_result,
        hold_result,
        grow_result,
        trace_result,
        pulse_result,
        dream_result,
        archive_result,
    ]:
        assert isinstance(result, str)
        assert result
    assert callable(backup_entry.backup_export_endpoint)
    assert callable(backup_entry.embeddings_backfill_endpoint)
    assert sum(bucket_id in limited_search for bucket_id in regular_ids) == 2
    assert "pinned control" not in limited_search
