from unittest.mock import AsyncMock, MagicMock

import pytest

from bucket_manager import BucketManager
from embedding_engine import EmbeddingEngine


@pytest.mark.asyncio
async def test_embedding_database_is_scoped_to_current_model(test_config):
    first_config = {
        **test_config,
        "embedding": {
            "api_key": "test-key",
            "model": "old-model",
            "enabled": True,
            "independent": True,
        },
    }
    first = EmbeddingEngine(first_config)
    first._store_embedding("bucket-1", [1.0, 0.0])

    second_config = {
        **test_config,
        "embedding": {
            "api_key": "test-key",
            "model": "BAAI/bge-m3",
            "enabled": True,
            "independent": True,
        },
    }
    second = EmbeddingEngine(second_config)

    assert second.model == "BAAI/bge-m3"
    assert await second.get_embedding("bucket-1") is None


@pytest.mark.asyncio
async def test_semantic_recall_finds_fuzzy_description(test_config):
    embedding = MagicMock()
    embedding.enabled = True
    embedding.generate_and_store = AsyncMock(return_value=True)
    embedding.search_similar = AsyncMock()
    manager = BucketManager(test_config, embedding_engine=embedding)

    target_id = await manager.create(
        content="在雨夜争执后，两个人沉默了很久。",
        tags=["关系"],
        domain=["关系"],
    )
    other_id = await manager.create(
        content="今天整理了书架和桌面。",
        tags=["日常"],
        domain=["日常"],
    )
    embedding.search_similar.return_value = [
        (target_id, 0.91),
        (other_id, 0.12),
    ]

    results = await manager.search("那次不愉快", limit=5)

    assert results
    assert results[0]["id"] == target_id
    assert results[0]["vector_match"] is True
    embedding.search_similar.assert_awaited_once()


@pytest.mark.asyncio
async def test_exact_keyword_stays_above_stronger_semantic_match(test_config):
    embedding = MagicMock()
    embedding.enabled = True
    embedding.generate_and_store = AsyncMock(return_value=True)
    embedding.search_similar = AsyncMock()
    manager = BucketManager(test_config, embedding_engine=embedding)

    exact_id = await manager.create(
        content="人物资料",
        tags=["婷"],
        domain=["人物"],
    )
    semantic_id = await manager.create(
        content="另一个语义接近的事件",
        tags=["其他"],
        domain=["关系"],
    )
    embedding.search_similar.return_value = [
        (semantic_id, 0.99),
        (exact_id, 0.45),
    ]

    results = await manager.search("婷", limit=5)

    assert results[0]["id"] == exact_id
