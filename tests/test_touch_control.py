import importlib
import re
import sys
from unittest.mock import AsyncMock

import frontmatter
import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.delenv("OMBRE_RESPONSE_SEAL", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None, **kwargs: content
    )
    if server.embedding_engine:
        server.embedding_engine.enabled = False
    return server


def _next_cursor(result: str) -> str:
    match = re.search(r"^下一页 cursor: (\S+)$", result, re.MULTILINE)
    return match.group(1) if match else ""


async def _metadata(server, bucket_id):
    return (await server.bucket_mgr.get(bucket_id))["metadata"]


async def _make_old_low_importance(server, bucket_id):
    path = server.bucket_mgr._find_bucket_file(bucket_id)
    post = frontmatter.load(path)
    post["last_active"] = "2000-01-01T00:00:00"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(post))


@pytest.mark.asyncio
async def test_breath_touch_false_keeps_query_results_but_not_activation_or_cache(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(content="touch control keyword")
    before = await _metadata(server, bucket_id)

    result = await server.breath(query="touch control keyword", touch=False)
    after = await _metadata(server, bucket_id)

    assert bucket_id in result
    assert after["activation_count"] == before["activation_count"]
    assert after["last_active"] == before["last_active"]
    server.decay_engine.ensure_started.assert_not_awaited()
    assert server.dehydrator.dehydrate.await_args.kwargs["cache"] is False


@pytest.mark.asyncio
async def test_breath_default_touch_preserves_activation_behavior(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(content="default touch keyword")
    before = await _metadata(server, bucket_id)

    result = await server.breath(query="default touch keyword")
    after = await _metadata(server, bucket_id)

    assert bucket_id in result
    assert after["activation_count"] > before["activation_count"]
    server.decay_engine.ensure_started.assert_awaited_once()
    assert "cache" not in server.dehydrator.dehydrate.await_args.kwargs


@pytest.mark.asyncio
async def test_breath_touch_false_reads_dormant_without_waking_it(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(content="dormant maintenance keyword")
    await server.bucket_mgr.set_dormant(bucket_id, True)
    await _make_old_low_importance(server, bucket_id)
    before = await _metadata(server, bucket_id)

    result = await server.breath(
        query="dormant maintenance keyword",
        include_dormant=True,
        wake_dormant=True,
        touch=False,
    )
    after = await _metadata(server, bucket_id)

    assert bucket_id in result
    assert after["dormant"] is True
    assert after["activation_count"] == before["activation_count"]
    assert after["last_active"] == before["last_active"]


@pytest.mark.asyncio
async def test_breath_cursor_freezes_touch_choice(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    first_id = await server.bucket_mgr.create(content="cursor touch keyword one")
    second_id = await server.bucket_mgr.create(content="cursor touch keyword two")

    first = await server.breath(
        query="cursor touch keyword", max_results=1, touch=False
    )
    cursor = _next_cursor(first)
    mismatched = await server.breath(
        query="cursor touch keyword", max_results=1, cursor=cursor
    )
    second = await server.breath(
        query="cursor touch keyword", max_results=1, cursor=cursor, touch=False
    )

    assert cursor
    assert "无效、已过期或与当前检索条件不匹配" in mismatched
    assert {first_id, second_id} <= {
        bucket_id
        for bucket_id in (first_id, second_id)
        if bucket_id in first or bucket_id in second
    }
    assert (await _metadata(server, first_id))["activation_count"] == 0
    assert (await _metadata(server, second_id))["activation_count"] == 0


@pytest.mark.asyncio
async def test_as_of_remains_read_only_when_touch_true(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(content="historical explicit touch keyword")
    before = await _metadata(server, bucket_id)

    result = await server.breath(
        query="historical explicit touch keyword",
        as_of="2099-01-01",
        touch=True,
    )
    after = await _metadata(server, bucket_id)

    assert bucket_id in result
    assert after["activation_count"] == before["activation_count"]
    assert after["last_active"] == before["last_active"]
    server.decay_engine.ensure_started.assert_not_awaited()


@pytest.mark.asyncio
async def test_pulse_touch_false_does_not_start_decay_or_mark_dormant(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        content="old maintenance listing", importance=1
    )
    await _make_old_low_importance(server, bucket_id)

    result = await server.pulse(show_all=True, touch=False)
    metadata = await _metadata(server, bucket_id)

    assert bucket_id in result
    assert metadata["dormant"] is False
    server.decay_engine.ensure_started.assert_not_awaited()


@pytest.mark.asyncio
async def test_pulse_default_touch_still_marks_dormant(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(content="old ordinary listing", importance=1)
    await _make_old_low_importance(server, bucket_id)

    await server.pulse(show_all=True)

    assert (await _metadata(server, bucket_id))["dormant"] is True
    server.decay_engine.ensure_started.assert_awaited_once()
