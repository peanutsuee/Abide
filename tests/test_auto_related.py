import importlib
import sys
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.embedding_engine.enabled = True
    server.bucket_mgr.embedding_engine.enabled = True
    return server


@pytest.mark.asyncio
async def test_auto_related_links_top_matches_and_skips_sealed(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    target_id = await server.bucket_mgr.create(content="target relation")
    similar_id = await server.bucket_mgr.create(content="similar relation")
    sealed_id = await server.bucket_mgr.create(content="sealed relation")
    far_id = await server.bucket_mgr.create(content="far relation")
    await server.trace(sealed_id, sealed=1)

    server.embedding_engine._store_embedding(target_id, [1.0, 0.0])
    server.embedding_engine._store_embedding(similar_id, [0.95, 0.05])
    server.embedding_engine._store_embedding(sealed_id, [0.99, 0.01])
    server.embedding_engine._store_embedding(far_id, [0.0, 1.0])

    linked = await server._auto_link_related(target_id, threshold=0.75)
    target = await server.bucket_mgr.get(target_id)
    similar = await server.bucket_mgr.get(similar_id)
    sealed = await server.bucket_mgr.get(sealed_id)

    assert len(linked) == 1
    assert linked[0][0] == similar_id
    assert linked[0][1] >= 0.75
    assert similar_id in target["metadata"]["related_buckets"]
    assert sealed_id not in target["metadata"]["related_buckets"]
    assert target_id in similar["metadata"]["related_buckets"]
    assert target_id not in sealed["metadata"].get("related_buckets", "")


@pytest.mark.asyncio
async def test_related_backfill_dry_run_does_not_write(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    first_id = await server.bucket_mgr.create(content="first relation")
    second_id = await server.bucket_mgr.create(content="second relation")
    server.embedding_engine._store_embedding(first_id, [1.0, 0.0])
    server.embedding_engine._store_embedding(second_id, [0.95, 0.05])

    result = await server.related_backfill(dry_run=True, threshold=0.75)
    first = await server.bucket_mgr.get(first_id)

    assert "自动 related dry-run" in result
    assert first_id in result
    assert second_id in result
    assert first["metadata"].get("related_buckets", "") == ""
