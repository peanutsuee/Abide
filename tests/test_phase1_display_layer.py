import importlib
import re
import sys
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "phase1-display-seal")
    monkeypatch.delenv("OMBRE_BREATH_MIN_SCORE", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.bucket_mgr.touch = AsyncMock(return_value=True)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None, **kwargs: content
    )
    return server


def _bucket(bucket_id, content, *, name=None, pinned=False, protected=False):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "name": name or bucket_id,
            "pinned": pinned,
            "protected": protected,
            "created_at": "2026-09-15T00:00:00",
            "updated_at": "2026-09-15T00:00:00",
            "tags": [],
        },
    }


def _search_with_scores(buckets, scores):
    async def search(*args, trace=None, **kwargs):
        if trace is not None:
            trace["candidates"] = [
                {"id": bucket["id"], "scores": scores[bucket["id"]]}
                for bucket in buckets
            ]
        return buckets

    return search


def _next_cursor(result):
    match = re.search(r"^下一页 cursor: (\S+)$", result, re.MULTILINE)
    return match.group(1) if match else ""


@pytest.mark.asyncio
async def test_breath_exact_anchor_has_normalized_score_and_exact_channel(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket = _bucket("anchor", "only 琥珀风筝2309 appears here", name="anchor memory")
    server.bucket_mgr.search = AsyncMock(
        side_effect=_search_with_scores(
            [bucket], {"anchor": {"fuzzy_lexical": 0.31, "semantic": 0.72}}
        )
    )

    result = await server.breath(query=" 琥珀 风筝2309 ")

    assert "[sim=1.00]" in result
    assert "[通道:精确]" in result


@pytest.mark.asyncio
async def test_breath_weak_matches_have_no_body_and_do_not_consume_budget(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    strong = _bucket("strong", "strong body", name="Strong")
    weak = _bucket("weak", "weak body must stay hidden", name="Weak")
    server.bucket_mgr.search = AsyncMock(
        side_effect=_search_with_scores(
            [strong, weak],
            {
                "strong": {"fuzzy_lexical": 0.80, "semantic": 0.0},
                "weak": {"fuzzy_lexical": 0.30, "semantic": 0.0},
            },
        )
    )

    result = await server.breath(query="not an exact anchor", min_score=0.45)

    assert "strong body" in result
    assert "--- 弱匹配（仅列名） ---" in result
    assert "[bucket_id:weak] Weak sim=0.30" in result
    assert "weak body must stay hidden" not in result
    assert "因低于阈值降级 1" in result
    assert server.dehydrator.dehydrate.await_count == 1


@pytest.mark.asyncio
async def test_breath_cursor_freezes_scores_and_weak_group(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    strong = _bucket("strong", "strong body", name="Strong")
    weak = _bucket("weak", "weak body", name="Weak")
    by_id = {"strong": strong, "weak": weak}
    server.bucket_mgr.search = AsyncMock(
        side_effect=_search_with_scores(
            [strong, weak],
            {
                "strong": {"fuzzy_lexical": 0.80, "semantic": 0.0},
                "weak": {"fuzzy_lexical": 0.20, "semantic": 0.0},
            },
        )
    )
    server.bucket_mgr.get = AsyncMock(side_effect=lambda bucket_id: by_id.get(bucket_id))

    first = await server.breath(query="not an exact anchor", max_results=1, min_score=0.45)
    cursor = _next_cursor(first)
    by_id["weak"] = _bucket("weak", "not an exact anchor now appears here", name="Weak")
    second = await server.breath(
        query="not an exact anchor", max_results=1, min_score=0.45, cursor=cursor
    )

    assert cursor
    assert "[bucket_id:weak] Weak sim=0.20" in second
    assert "not an exact anchor now appears here" not in second
    assert server.bucket_mgr.search.await_count == 1


@pytest.mark.asyncio
async def test_breath_dual_channel_uses_max_score(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket = _bucket("dual", "ordinary body")
    server.bucket_mgr.search = AsyncMock(
        side_effect=_search_with_scores(
            [bucket], {"dual": {"fuzzy_lexical": 0.41, "semantic": 0.83}}
        )
    )

    result = await server.breath(query="not an exact anchor")

    assert "[sim=0.83]" in result
    assert "[通道:双]" in result


@pytest.mark.asyncio
async def test_breath_default_threshold_is_zero_and_explicit_value_overrides_env(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket = _bucket("candidate", "ordinary body")
    server.bucket_mgr.search = AsyncMock(
        side_effect=_search_with_scores(
            [bucket], {"candidate": {"fuzzy_lexical": 0.20, "semantic": 0.0}}
        )
    )

    default_result = await server.breath(query="not an exact anchor")
    monkeypatch.setenv("OMBRE_BREATH_MIN_SCORE", "0.90")
    explicit_result = await server.breath(query="not an exact anchor", min_score=0.10)

    assert server._resolve_breath_min_score(-1) == 0.90
    assert "ordinary body" in default_result
    assert "弱匹配" not in default_result
    assert "ordinary body" in explicit_result
    assert "弱匹配" not in explicit_result


@pytest.mark.asyncio
async def test_breath_pin_marker_requires_pinned_true(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    protected = _bucket("protected", "protected body", protected=True)
    pinned = _bucket("pinned", "pinned body", pinned=True)
    server.bucket_mgr.search = AsyncMock(
        side_effect=_search_with_scores(
            [protected, pinned],
            {
                "protected": {"fuzzy_lexical": 0.70, "semantic": 0.0},
                "pinned": {"fuzzy_lexical": 0.70, "semantic": 0.0},
            },
        )
    )

    result = await server.breath(query="not an exact anchor")
    protected_line = next(line for line in result.splitlines() if "bucket_id:protected" in line)
    pinned_line = next(line for line in result.splitlines() if "bucket_id:pinned" in line)

    assert "📌" not in protected_line
    assert "📌" in pinned_line


@pytest.mark.asyncio
async def test_filtered_breath_uses_same_score_and_weak_match_presentation(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    strong = _bucket("strong", "strong body", name="Strong")
    weak = _bucket("weak", "weak filtered body", name="Weak", pinned=True)
    server.bucket_mgr.list_all = AsyncMock(return_value=[strong, weak])
    server.bucket_mgr.search = AsyncMock(
        side_effect=_search_with_scores(
            [strong, weak],
            {
                "strong": {"fuzzy_lexical": 0.80, "semantic": 0.0},
                "weak": {"fuzzy_lexical": 0.20, "semantic": 0.0},
            },
        )
    )

    result = await server.breath(
        query="not an exact anchor", tags_filter=["tag"], min_score=0.45
    )

    assert "[sim=0.80]" in result
    assert "[bucket_id:weak] Weak sim=0.20" in result
    assert "weak filtered body" not in result


@pytest.mark.asyncio
async def test_pulse_default_mode_honors_limit_and_show_all_remains_bounded(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    for index in range(8):
        await server.bucket_mgr.create(content=f"pulse limit marker {index}")

    default_result = await server.pulse(show_all=False, limit=5)
    show_all_result = await server.pulse(show_all=True, limit=3)

    default_lines = [line for line in default_result.splitlines() if "bucket_id:" in line]
    all_lines = [line for line in show_all_result.splitlines() if "bucket_id:" in line]
    assert len(default_lines) <= 5
    assert len(all_lines) == 3
    assert "limit=5" in default_result


@pytest.mark.asyncio
async def test_pulse_limit_boundary_values(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    assert "limit 必须大于等于 1" in await server.pulse(limit=0)
    assert "limit 和 offset 必须是整数" in await server.pulse(limit="invalid")
