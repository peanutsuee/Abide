import importlib
import sys
from datetime import datetime
from unittest.mock import AsyncMock

import frontmatter
import pytest

from bucket_manager import normalize_provenance_kind


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_RAW_EVIDENCE_ROOT", str(tmp_path / "raw-evidence"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.delenv("OMBRE_DIGEST_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    return server


def _metadata(bucket):
    return bucket["metadata"]["provenance_kind"]


def test_normalization_fails_closed_for_legacy_frontmatter():
    for value in (None, "", "quote", " SUMMARY ", 7, {"kind": "summary"}):
        assert normalize_provenance_kind(value) == "unknown"
    for value in ("unknown", "summary", "inference", "system"):
        assert normalize_provenance_kind(value) == value
    with pytest.raises(ValueError, match="provenance_kind"):
        normalize_provenance_kind("quote", strict=True)


@pytest.mark.asyncio
async def test_generic_create_accepts_every_canonical_provenance_kind(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    for provenance_kind in ("unknown", "summary", "inference", "system"):
        bucket_id = await server.bucket_mgr.create(
            f"generic {provenance_kind}", provenance_kind=provenance_kind
        )
        assert _metadata(await server.bucket_mgr.get(bucket_id)) == provenance_kind


@pytest.mark.asyncio
async def test_legacy_and_malformed_bucket_frontmatter_read_as_unknown(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    legacy_id = await server.bucket_mgr.create("legacy")
    malformed_id = await server.bucket_mgr.create("malformed", provenance_kind="summary")
    path = server.bucket_mgr._find_bucket_file(malformed_id)
    post = frontmatter.load(path)
    post["provenance_kind"] = "quote"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(post))

    assert _metadata(await server.bucket_mgr.get(legacy_id)) == "unknown"
    assert _metadata(await server.bucket_mgr.get(malformed_id)) == "unknown"


@pytest.mark.asyncio
async def test_hold_explicit_default_and_feel_provenance(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._detect_conflict_warning = AsyncMock(return_value="")
    server.dehydrator.analyze = AsyncMock(return_value={
        "domain": ["test"], "valence": 0.5, "arousal": 0.5,
        "tags": [], "suggested_name": "held", "todos": [],
    })

    await server.hold("ordinary hold")
    ordinary = next(iter(await server.bucket_mgr.list_all()))
    assert _metadata(ordinary) == "unknown"

    await server.hold("explicit summary", provenance_kind="summary")
    assert any(
        bucket["content"] == "explicit summary" and _metadata(bucket) == "summary"
        for bucket in await server.bucket_mgr.list_all()
    )
    assert "provenance_kind" in await server.hold("bad", provenance_kind="quote")

    await server.hold("model reflection", feel=True)
    feel = next(
        bucket for bucket in await server.bucket_mgr.list_all()
        if bucket["metadata"].get("type") == "feel"
    )
    assert _metadata(feel) == "inference"


@pytest.mark.asyncio
async def test_grow_and_archive_trusted_writer_defaults(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._detect_conflict_warning = AsyncMock(return_value="")
    server.dehydrator.analyze = AsyncMock(return_value={
        "domain": ["test"], "valence": 0.5, "arousal": 0.5,
        "tags": [], "suggested_name": "short", "todos": [],
    })
    await server.grow("short retained input")
    server.dehydrator.digest = AsyncMock(return_value=[{
        "name": "long", "content": "generated summary body", "domain": ["test"],
        "valence": 0.5, "arousal": 0.5, "tags": [], "importance": 5,
        "todos": [],
    }])
    await server.grow("This sufficiently long input takes the digest path for a generated bucket.")
    archive_result = await server.archive_session("session summary")

    buckets = await server.bucket_mgr.list_all(include_archive=True)
    assert next(bucket for bucket in buckets if bucket["content"] == "short retained input")["metadata"]["provenance_kind"] == "unknown"
    assert next(bucket for bucket in buckets if bucket["content"] == "generated summary body")["metadata"]["provenance_kind"] == "summary"
    archive_id = archive_result.rsplit("bucket_id:", 1)[1]
    assert _metadata(await server.bucket_mgr.get(archive_id)) == "summary"


@pytest.mark.asyncio
async def test_trace_and_supersedes_body_mutations_invalidate_unless_explicit(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create("before", provenance_kind="summary")
    assert "summary->summary" in await server.trace(bucket_id, tags="tag", provenance_kind="summary")
    assert _metadata(await server.bucket_mgr.get(bucket_id)) == "summary"
    assert "summary->unknown" in await server.trace(bucket_id, content="after")
    assert _metadata(await server.bucket_mgr.get(bucket_id)) == "unknown"
    assert "unknown->inference" in await server.trace(
        bucket_id, content="deduced", provenance_kind="inference"
    )
    assert _metadata(await server.bucket_mgr.get(bucket_id)) == "inference"
    assert "批量 trace 不支持 provenance_kind" in await server.trace(
        f"{bucket_id},{await server.bucket_mgr.create('other')}", provenance_kind="summary"
    )

    evolved_id = await server.bucket_mgr.create("old", provenance_kind="summary")
    await server.hold("new", supersedes_id=evolved_id)
    assert _metadata(await server.bucket_mgr.get(evolved_id)) == "unknown"
    await server.hold("newer", supersedes_id=evolved_id, provenance_kind="system")
    assert _metadata(await server.bucket_mgr.get(evolved_id)) == "system"


@pytest.mark.asyncio
async def test_merge_duplicate_and_lifecycle_preserve_or_clear_provenance(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    target_id = await server.bucket_mgr.create("target", provenance_kind="summary")
    source_id = await server.bucket_mgr.create("source", provenance_kind="summary")
    assert "已合并" in await server.trace(target_id, merge=source_id)
    assert _metadata(await server.bucket_mgr.get(target_id)) == "unknown"

    duplicate_id = await server.bucket_mgr.create("duplicate", provenance_kind="inference")
    duplicate = await server.bucket_mgr.get(duplicate_id)
    server.bucket_mgr.search = AsyncMock(return_value=[duplicate])
    _, reused = await server._merge_or_create(
        "duplicate", [], 5, ["未分类"], 0.5, 0.3
    )
    assert reused is True
    assert _metadata(await server.bucket_mgr.get(duplicate_id)) == "inference"

    successor_id = await server.bucket_mgr.create("successor", provenance_kind="system")
    await server.trace(duplicate_id, superseded_by=successor_id)
    await server.trace(duplicate_id, dormant=1, sealed=1)
    assert _metadata(await server.bucket_mgr.get(duplicate_id)) == "inference"
    await server.trace(duplicate_id, sealed=0)
    assert await server.bucket_mgr.archive(duplicate_id)
    assert _metadata(await server.bucket_mgr.get(duplicate_id)) == "inference"


@pytest.mark.asyncio
async def test_normal_breath_badge_and_historical_breath_omission(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr("bucket_manager.now_iso", lambda: "2026-08-01T09:00:00")
    bucket_id = await server.bucket_mgr.create("history token", provenance_kind="summary")
    server.dehydrator.dehydrate = AsyncMock(return_value="current summary")

    current = await server.breath(query="history token", touch=False)
    historical = await server.breath(query="history token", as_of="2026-08-01T12:00:00")
    assert "[prov=summary]" in current
    assert "[prov=" not in historical

    sealed_id = await server.bucket_mgr.create("sealed token", provenance_kind="system", sealed=True)
    hidden = await server.breath(query="sealed token", touch=False)
    assert sealed_id not in hidden
    assert "[prov=system]" not in hidden
