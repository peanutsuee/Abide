from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backfill_embeddings import backfill_batch


@pytest.mark.asyncio
async def test_backfill_batch_only_indexes_missing_nonempty_buckets():
    buckets = [
        {"id": "existing", "content": "already indexed"},
        {"id": "missing-1", "content": "first missing"},
        {"id": "missing-2", "content": "second missing"},
        {"id": "empty", "content": "  "},
    ]
    bucket_mgr = SimpleNamespace(
        list_all=AsyncMock(return_value=buckets),
    )
    indexed = {"existing"}

    async def get_embedding(bucket_id):
        return [1.0] if bucket_id in indexed else None

    async def generate_and_store(bucket_id, content):
        indexed.add(bucket_id)
        return True

    engine = SimpleNamespace(
        enabled=True,
        model="Qwen/Qwen3-Embedding-0.6B",
        get_embedding=AsyncMock(side_effect=get_embedding),
        generate_and_store=AsyncMock(side_effect=generate_and_store),
        last_error="",
        last_error_details={},
    )

    result = await backfill_batch(bucket_mgr, engine, limit=1)

    assert result == {
        "model": "Qwen/Qwen3-Embedding-0.6B",
        "total_buckets": 4,
        "eligible_buckets": 3,
        "empty_skipped": 1,
        "indexed_total": 2,
        "attempted": 1,
        "success": 1,
        "failed": 0,
        "remaining": 1,
        "last_error": "",
        "error_details": {},
    }
    engine.generate_and_store.assert_awaited_once_with(
        "missing-1",
        "first missing",
    )
