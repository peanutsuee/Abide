import importlib
import re
import sys
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "breath-accounting-seal")
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.bucket_mgr.touch = AsyncMock(return_value=True)
    return server


def _bucket(index, *, vector_match=False):
    return {
        "id": f"breath-accounting-{index:02d}",
        "content": f"memory content {index}",
        "vector_match": vector_match,
        "metadata": {
            "name": f"memory-{index}",
            "created_at": "2026-09-12T00:00:00",
            "updated_at": "2026-09-12T00:00:00",
            "tags": [],
        },
    }


def _next_cursor(result: str) -> str:
    match = re.search(r"^下一页 cursor: (\S+)$", result, re.MULTILINE)
    return match.group(1) if match else ""


@pytest.mark.asyncio
async def test_breath_vector_result_uses_utf8_semantic_label(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server.bucket_mgr.search = AsyncMock(return_value=[_bucket(1, vector_match=True)])
    server.dehydrator.dehydrate = AsyncMock(return_value="ordinary query summary")

    result = await server.breath(query="unique anchor")

    assert "[语义关联]" in result
    assert "璇箟鍏宠仈" not in result
    assert "杩樻湁" not in result


@pytest.mark.asyncio
async def test_breath_31_matches_displays_8_and_reports_23(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server.bucket_mgr.search = AsyncMock(
        return_value=[_bucket(index) for index in range(31)]
    )
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: content
    )

    result = await server.breath(query="accounting anchor", max_results=8)

    assert "还有23个相关记忆未显示" in result
    assert (
        "共匹配 31 / 本次显示 8 / 因结果上限省略 23 / "
        "因 token 预算省略 0 / 因低于阈值降级 0"
    ) in result
    assert "杩樻湁" not in result


@pytest.mark.asyncio
async def test_breath_counts_selected_items_omitted_by_token_budget(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch)
    server.bucket_mgr.search = AsyncMock(
        return_value=[_bucket(index) for index in range(4)]
    )
    summaries = iter(["short", "长" * 100, "unused third summary"])
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: next(summaries)
    )

    result = await server.breath(
        query="token budget anchor",
        max_results=3,
        max_tokens=5,
    )

    assert "还有3个相关记忆未显示" in result
    assert (
        "共匹配 4 / 本次显示 1 / 因结果上限省略 1 / "
        "因 token 预算省略 2 / 因低于阈值降级 0"
    ) in result
    assert server.dehydrator.dehydrate.await_count == 2


@pytest.mark.asyncio
async def test_breath_cursor_pages_are_stable_without_duplicates_or_gaps(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch)
    buckets = [_bucket(index) for index in range(12)]
    by_id = {bucket["id"]: bucket for bucket in buckets}
    server.bucket_mgr.search = AsyncMock(return_value=buckets)
    server.bucket_mgr.get = AsyncMock(side_effect=lambda bucket_id: by_id.get(bucket_id))
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: content
    )

    first = await server.breath(query="paged anchor", max_results=5)
    second = await server.breath(
        query="paged anchor",
        max_results=5,
        cursor=_next_cursor(first),
    )
    third = await server.breath(
        query="paged anchor",
        max_results=5,
        cursor=_next_cursor(second),
    )

    expected_pages = [buckets[:5], buckets[5:10], buckets[10:]]
    actual_pages = [first, second, third]
    for result, expected in zip(actual_pages, expected_pages):
        expected_ids = {bucket["id"] for bucket in expected}
        assert all(bucket_id in result for bucket_id in expected_ids)
        assert all(
            bucket["id"] not in result
            for bucket in buckets
            if bucket["id"] not in expected_ids
        )

    assert (
        "共匹配 12 / 本次显示 5 / 因结果上限省略 7 / "
        "因 token 预算省略 0 / 因低于阈值降级 0"
    ) in second
    assert (
        "共匹配 12 / 本次显示 2 / 因结果上限省略 10 / "
        "因 token 预算省略 0 / 因低于阈值降级 0"
    ) in third
    assert _next_cursor(first)
    assert _next_cursor(second)
    assert not _next_cursor(third)
    assert server.bucket_mgr.search.await_count == 1
    assert server.bucket_mgr.get.await_count == 7


@pytest.mark.asyncio
async def test_breath_cursor_rejects_changed_query_and_tampering(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    buckets = [_bucket(index) for index in range(6)]
    server.bucket_mgr.search = AsyncMock(return_value=buckets)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: content
    )

    first = await server.breath(query="original anchor", max_results=5)
    cursor = _next_cursor(first)

    changed_scope = await server.breath(
        query="different anchor",
        max_results=5,
        cursor=cursor,
    )
    tampered = await server.breath(
        query="original anchor",
        max_results=5,
        cursor=cursor[:10] + ("A" if cursor[10] != "A" else "B") + cursor[11:],
    )

    assert "无效、已过期或与当前检索条件不匹配" in changed_scope
    assert "无效、已过期或与当前检索条件不匹配" in tampered
