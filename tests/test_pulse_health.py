import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import frontmatter
import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=AssertionError("health must not dehydrate")
    )
    server.embedding_engine._generate_embedding = AsyncMock(
        side_effect=AssertionError("health must not generate embeddings")
    )
    return server


def _set_metadata(server, bucket_id, **updates):
    path = Path(server.bucket_mgr._find_bucket_file(bucket_id))
    post = frontmatter.load(path)
    for key, value in updates.items():
        if value is _MISSING:
            post.metadata.pop(key, None)
        else:
            post[key] = value
    path.write_text(frontmatter.dumps(post), encoding="utf-8")


_MISSING = object()


def _health_section(result):
    return result.split("=== maintenance health ===\n", 1)[1]


@pytest.mark.asyncio
async def test_pulse_default_behavior_has_no_health_section(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(content="ordinary pulse bucket", name="ordinary")

    result = await server.pulse(show_all=True, touch=False)

    assert bucket_id in result
    assert "=== maintenance health ===" not in result
    assert "总数:1个可见桶" in result


@pytest.mark.asyncio
async def test_health_rejects_touch_before_decay_or_dormant_marking(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    dormant_marker = AsyncMock(return_value=0)
    monkeypatch.setattr(server, "_mark_dormant_buckets", dormant_marker)

    result = await server.pulse(health=True)

    assert "health=True 需要 touch=False" in result
    server.decay_engine.ensure_started.assert_not_awaited()
    dormant_marker.assert_not_awaited()


@pytest.mark.asyncio
async def test_health_is_read_only_and_count_only(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        content="read only health sentinel", name="READ_ONLY_NAME", todos=["keep"]
    )
    path = Path(server.bucket_mgr._find_bucket_file(bucket_id))
    before = path.read_bytes()
    embedding_before = Path(server.embedding_engine.db_path).read_bytes()

    result = await server.pulse(health=True, touch=False)
    section = _health_section(result)

    assert path.read_bytes() == before
    assert Path(server.embedding_engine.db_path).read_bytes() == embedding_before
    server.decay_engine.ensure_started.assert_not_awaited()
    server.dehydrator.dehydrate.assert_not_awaited()
    server.embedding_engine._generate_embedding.assert_not_awaited()
    assert "READ_ONLY_NAME" not in section
    assert bucket_id not in section


@pytest.mark.asyncio
async def test_health_fails_closed_when_full_visibility_scan_is_unavailable(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(content="health fallback", name="visible")
    bucket = await server.bucket_mgr.get(bucket_id)
    server.bucket_mgr.list_all = AsyncMock(
        side_effect=[[bucket], RuntimeError("health scan unavailable")]
    )

    result = await server.pulse(health=True, touch=False)

    assert _health_section(result) == "health unavailable"


@pytest.mark.asyncio
async def test_health_unnamed_and_untagged_normalization(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    missing_name = await server.bucket_mgr.create(content="missing name", tags=[])
    blank_name = await server.bucket_mgr.create(content="blank name", tags=[])
    default_name = await server.bucket_mgr.create(content="default name", tags=[])
    named = await server.bucket_mgr.create(content="named", name="named", tags=["tag"])
    legacy_empty_tags = await server.bucket_mgr.create(
        content="legacy tags", name="legacy", tags=["tag"]
    )
    _set_metadata(server, missing_name, name=_MISSING)
    _set_metadata(server, blank_name, name="   ")
    _set_metadata(server, legacy_empty_tags, tags=" , , ")

    result = await server.pulse(health=True, touch=False)
    section = _health_section(result)

    assert f"unnamed buckets: 3" in section
    assert f"untagged buckets: 4" in section
    assert named not in section


@pytest.mark.asyncio
async def test_health_stale_todos_uses_bucket_activity_fallbacks(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    old = (datetime.now() - timedelta(days=40)).isoformat()
    recent = (datetime.now() - timedelta(days=2)).isoformat()
    by_last_active = await server.bucket_mgr.create(content="a", todos=["todo"])
    by_updated_at = await server.bucket_mgr.create(content="b", todos=["todo"])
    by_created = await server.bucket_mgr.create(content="c", todos=["todo"])
    recent_id = await server.bucket_mgr.create(content="d", todos=["todo"])
    resolved_id = await server.bucket_mgr.create(content="e", todos=["todo"])
    no_todo_id = await server.bucket_mgr.create(content="f")
    unavailable = await server.bucket_mgr.create(content="g", todos=["todo"])
    _set_metadata(server, by_last_active, last_active=old)
    _set_metadata(server, by_updated_at, last_active=_MISSING, updated_at=old)
    _set_metadata(server, by_created, last_active=_MISSING, updated_at=_MISSING, created=old)
    _set_metadata(server, recent_id, last_active=recent)
    _set_metadata(server, resolved_id, last_active=old, resolved=True)
    _set_metadata(server, no_todo_id, last_active=old)
    _set_metadata(
        server,
        unavailable,
        last_active="not-a-time",
        updated_at="also-not-a-time",
        created="still-not-a-time",
    )

    result = await server.pulse(health=True, touch=False, todo_stale_days=30)
    section = _health_section(result)

    assert "stale todo buckets (>30d): 3" in section
    assert "todo age unavailable: 1" in section


@pytest.mark.asyncio
async def test_supersession_health_detects_currently_representable_invalid_states_async(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    valid_source = await server.bucket_mgr.create(content="valid source")
    valid_target = await server.bucket_mgr.create(content="valid target")
    none_source = await server.bucket_mgr.create(content="none source")
    missing = await server.bucket_mgr.create(content="missing")
    missing_inverse = await server.bucket_mgr.create(content="missing inverse")
    inverse_holder = await server.bucket_mgr.create(content="inverse holder")
    stale_source = await server.bucket_mgr.create(content="stale source")
    forward_self = await server.bucket_mgr.create(content="forward self")
    reverse_self = await server.bucket_mgr.create(content="reverse self")
    cycle_a = await server.bucket_mgr.create(content="cycle a")
    cycle_b = await server.bucket_mgr.create(content="cycle b")
    malformed = await server.bucket_mgr.create(content="malformed")
    _set_metadata(server, valid_source, superseded_by=valid_target)
    _set_metadata(server, valid_target, supersedes=[valid_source])
    _set_metadata(server, none_source, superseded_by="none")
    _set_metadata(server, missing, superseded_by="missing-target")
    _set_metadata(server, missing_inverse, superseded_by=inverse_holder)
    _set_metadata(server, inverse_holder, supersedes=[stale_source])
    _set_metadata(server, forward_self, superseded_by=forward_self)
    _set_metadata(server, reverse_self, supersedes=[reverse_self])
    _set_metadata(server, cycle_a, superseded_by=cycle_b, supersedes=[cycle_b])
    _set_metadata(server, cycle_b, superseded_by=cycle_a, supersedes=[cycle_a])
    _set_metadata(server, malformed, superseded_by="None")

    buckets = await server.bucket_mgr.list_all(include_archive=True)
    report = server._health_supersession_report(buckets, buckets)

    assert report["missing_successor"] == 1
    assert report["missing_reverse"] == 1
    assert report["stale_reverse"] == 2
    assert report["forward_self"] == 1
    assert report["reverse_self"] == 1
    assert report["cycle"] == 2
    assert report["malformed_successor"] == 1
    assert report["total"] == 9


@pytest.mark.asyncio
async def test_health_sealed_buckets_and_relations_do_not_affect_counts(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    visible = await server.bucket_mgr.create(content="visible", name="visible", tags=["tag"])
    sealed = await server.bucket_mgr.create(content="sealed", todos=["hidden"])
    _set_metadata(server, visible, superseded_by=sealed)
    _set_metadata(
        server,
        sealed,
        sealed=1,
        name="",
        tags=[],
        pinned=True,
        importance=1,
        superseded_by="missing",
    )

    result = await server.pulse(health=True, touch=False)
    section = _health_section(result)

    assert "unnamed buckets: 0" in section
    assert "untagged buckets: 0" in section
    assert "supersession problems: 0" in section
    assert "pinned importance <3: 0" in section
    assert sealed not in section


@pytest.mark.asyncio
async def test_health_counts_pinned_low_importance_and_validates_inputs(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(content="legacy pinned", name="legacy")
    _set_metadata(server, bucket_id, pinned=True, importance=2)

    result = await server.pulse(health=True, touch=False)

    assert "pinned importance <3: 1" in _health_section(result)
    assert "todo_stale_days 必须是正整数" in await server.pulse(
        health=True, touch=False, todo_stale_days=0
    )
    assert "health=True 不支持 include_sealed" in await server.pulse(
        health=True, touch=False, include_sealed=True
    )
