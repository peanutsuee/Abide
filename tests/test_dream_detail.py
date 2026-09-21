import importlib
import sys
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_dream_detail_fetches_id_directly_and_returns_full_content(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)

    full_content = "detail-start " + ("x" * 700) + " detail-end"
    target_id = await server.bucket_mgr.create(
        content=full_content,
        name="Old archived target",
    )
    await server.bucket_mgr.archive(target_id)
    server.bucket_mgr.touch = AsyncMock(return_value=None)
    server.bucket_mgr.list_all = AsyncMock(
        side_effect=AssertionError("detail mode must not list recent buckets")
    )

    result = await server.dream(detail_ids=target_id)

    assert target_id in result
    assert "detail-start" in result
    assert "detail-end" in result
    assert full_content in result
    server.bucket_mgr.list_all.assert_not_awaited()


@pytest.mark.asyncio
async def test_dream_detail_reports_missing_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)

    result = await server.dream(detail_ids="missing-id")

    assert result.endswith("未找到记忆桶: missing-id")
