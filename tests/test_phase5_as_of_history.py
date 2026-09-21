import importlib
import re
import sys
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.delenv("OMBRE_RESPONSE_SEAL", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.bucket_mgr.touch = AsyncMock(return_value=True)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=AssertionError("historical retrieval must not dehydrate or cache")
    )
    return server


def _set_time(monkeypatch, value):
    import bucket_manager

    monkeypatch.setattr(bucket_manager, "now_iso", lambda: value)


async def _create(server, monkeypatch, content, created_at, **kwargs):
    _set_time(monkeypatch, created_at)
    return await server.bucket_mgr.create(content=content, **kwargs)


async def _replace(server, monkeypatch, bucket_id, content, changed_at):
    _set_time(monkeypatch, changed_at)
    assert await server.bucket_mgr.update(bucket_id, content=content)


def _next_cursor(result: str) -> str:
    match = re.search(r"^下一页 cursor: (\S+)$", result, re.MULTILINE)
    return match.group(1) if match else ""


@pytest.mark.asyncio
async def test_as_of_omitted_keeps_normal_breath_path(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    await _create(server, monkeypatch, "normal-current-keyword", "2026-08-01T09:00:00")
    server.dehydrator.dehydrate = AsyncMock(return_value="normal-current-keyword")

    result = await server.breath(query="normal-current-keyword")

    assert "历史版本" not in result
    assert "normal-current-keyword" in result
    server.decay_engine.ensure_started.assert_awaited_once()
    server.bucket_mgr.touch.assert_awaited()


@pytest.mark.asyncio
async def test_as_of_excludes_pre_creation_and_uses_unchanged_current_body(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await _create(
        server, monkeypatch, "unchanged-history-keyword", "2026-08-10T09:00:00"
    )

    before = await server.breath(query="unchanged-history-keyword", as_of="2026-08-09")
    after = await server.breath(query="unchanged-history-keyword", as_of="2026-08-10")

    assert bucket_id not in before
    assert "未找到在该时点存在的相关历史记忆" in before
    assert bucket_id in after
    assert "[历史版本 · as_of=2026-08-10 · metadata=当前]" in after
    assert "unchanged-history-keyword" in after


@pytest.mark.asyncio
async def test_as_of_selects_one_change_on_both_sides_and_at_boundary(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await _create(server, monkeypatch, "before-once-keyword", "2026-08-01T09:00:00")
    await _replace(server, monkeypatch, bucket_id, "after-once-keyword", "2026-08-02T12:00:00")

    before = await server.breath(query="before-once-keyword", as_of="2026-08-02T11:59:59")
    boundary = await server.breath(query="after-once-keyword", as_of="2026-08-02T12:00:00")
    after = await server.breath(query="after-once-keyword", as_of="2026-08-03T00:00:00")

    assert "before-once-keyword" in before
    assert "after-once-keyword" not in before
    assert "after-once-keyword" in boundary
    assert "before-once-keyword" not in boundary
    assert "after-once-keyword" in after


@pytest.mark.asyncio
async def test_as_of_selects_correct_version_across_multiple_changes(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await _create(server, monkeypatch, "version-one-keyword", "2026-08-01T09:00:00")
    await _replace(server, monkeypatch, bucket_id, "version-two-keyword", "2026-08-02T09:00:00")
    await _replace(server, monkeypatch, bucket_id, "version-three-keyword", "2026-08-03T09:00:00")

    first = await server.breath(query="version-one-keyword", as_of="2026-08-01T12:00:00")
    second = await server.breath(query="version-two-keyword", as_of="2026-08-02T12:00:00")
    third = await server.breath(query="version-three-keyword", as_of="2026-08-03T12:00:00")

    assert "version-one-keyword" in first
    assert "version-two-keyword" in second
    assert "version-three-keyword" in third


@pytest.mark.asyncio
async def test_as_of_date_only_means_local_end_of_day_and_future_uses_current(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await _create(server, monkeypatch, "date-before-keyword", "2026-08-01T09:00:00")
    await _replace(server, monkeypatch, bucket_id, "date-after-keyword", "2026-08-02T12:00:00")

    date_only = await server.breath(query="date-after-keyword", as_of="2026-08-02")
    future = await server.breath(query="date-after-keyword", as_of="2099-01-01T00:00:00")

    assert "date-after-keyword" in date_only
    assert "date-after-keyword" in future


@pytest.mark.asyncio
async def test_as_of_rejects_invalid_and_mutating_or_ambiguous_modes(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    await _create(server, monkeypatch, "mode-keyword", "2026-08-01T09:00:00")

    assert "ISO8601" in await server.breath(query="mode-keyword", as_of="2026/08/01")
    assert "需要提供 query" in await server.breath(as_of="2026-08-01")
    assert "不能 wake_dormant" in await server.breath(
        query="mode-keyword", as_of="2026-08-01", wake_dormant=True
    )
    assert "不支持 tags_filter/topic_filter" in await server.breath(
        query="mode-keyword", as_of="2026-08-01", tags_filter=["tag"]
    )


@pytest.mark.asyncio
async def test_as_of_is_zero_touch_and_never_uses_current_embedding_or_api(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await _create(server, monkeypatch, "history-only-needle", "2026-08-01T09:00:00")
    await _replace(server, monkeypatch, bucket_id, "current body without anchor", "2026-08-02T09:00:00")
    server.embedding_engine.enabled = True
    server.embedding_engine.search_similar = AsyncMock(
        side_effect=AssertionError("historical retrieval must not use current embeddings")
    )

    result = await server.breath(query="history-only-needle", as_of="2026-08-01T12:00:00")

    assert bucket_id in result
    assert "history-only-needle" in result
    assert "current body without anchor" not in result
    server.bucket_mgr.touch.assert_not_awaited()
    server.decay_engine.ensure_started.assert_not_awaited()
    server.dehydrator.dehydrate.assert_not_awaited()
    server.embedding_engine.search_similar.assert_not_awaited()


@pytest.mark.asyncio
async def test_as_of_respects_sealed_and_temporal_supersession_markers(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    sealed_id = await _create(
        server,
        monkeypatch,
        "sealed-history-needle",
        "2026-08-01T09:00:00",
        sealed=True,
    )
    old_id = await _create(server, monkeypatch, "superseded-history-needle", "2026-08-01T09:00:00")
    successor_id = await _create(server, monkeypatch, "successor-current-body", "2026-08-01T09:00:00")
    await server.bucket_mgr.update(
        old_id,
        superseded_by=successor_id,
        superseded_at="2026-08-03T09:00:00",
    )

    hidden = await server.breath(query="sealed-history-needle", as_of="2026-08-01T12:00:00")
    before = await server.breath(query="superseded-history-needle", as_of="2026-08-02T12:00:00")
    after = await server.breath(query="superseded-history-needle", as_of="2026-08-04T12:00:00")

    assert sealed_id not in hidden
    assert "⊘已作废" not in before
    assert "⊘已作废" in after


@pytest.mark.asyncio
async def test_as_of_cursor_freezes_time_and_reconstructs_history_on_next_page(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    first_id = await _create(server, monkeypatch, "paged-history-needle one", "2026-08-01T09:00:00")
    second_id = await _create(server, monkeypatch, "paged-history-needle two", "2026-08-01T09:00:00")
    await _replace(server, monkeypatch, first_id, "current first", "2026-08-02T09:00:00")
    await _replace(server, monkeypatch, second_id, "current second", "2026-08-02T09:00:00")

    first = await server.breath(
        query="paged-history-needle", as_of="2026-08-01T12:00:00", max_results=1
    )
    cursor = _next_cursor(first)
    second = await server.breath(
        query="paged-history-needle",
        as_of="2026-08-01T12:00:00",
        max_results=1,
        cursor=cursor,
    )
    swapped = await server.breath(
        query="paged-history-needle",
        as_of="2026-08-02T12:00:00",
        max_results=1,
        cursor=cursor,
    )

    assert cursor
    assert "[历史版本 · as_of=2026-08-01T12:00:00" in first
    assert "[历史版本 · as_of=2026-08-01T12:00:00" in second
    assert {first_id, second_id} <= {bucket_id for bucket_id in (first_id, second_id) if bucket_id in first or bucket_id in second}
    assert "current first" not in second
    assert "current second" not in second
    assert "无效、已过期或与当前检索条件不匹配" in swapped
