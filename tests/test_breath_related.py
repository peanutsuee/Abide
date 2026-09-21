import importlib
import sys
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_breath_includes_related_id_and_name_without_content(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: content
    )

    related_id = await server.bucket_mgr.create(
        content="RELATED SECRET CONTENT",
        name="Related memory",
    )
    hit_id = await server.bucket_mgr.create(
        content="search needle",
        name="Matched memory",
    )
    await server.bucket_mgr.update(hit_id, related_buckets=related_id)
    hit = await server.bucket_mgr.get(hit_id)
    server.bucket_mgr.search = AsyncMock(return_value=[hit])
    server.bucket_mgr.list_all = AsyncMock(return_value=[])

    result = await server.breath(query="search needle")

    assert hit_id in result
    assert f"[{related_id}] Related memory" in result
    assert "RELATED SECRET CONTENT" not in result
