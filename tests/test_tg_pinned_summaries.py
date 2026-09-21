import hashlib
import importlib
import sys
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "tg-summary-seal")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: content[:120]
    )
    return server


@pytest.mark.asyncio
async def test_legacy_pinned_bucket_uses_tg_preview_and_missing_summary_notice(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    body = ("原文" * 300) + "TG_SUMMARY_HIDDEN_TAIL"
    bucket_id = await server.bucket_mgr.create(body, name="legacy pinned", pinned=True)

    result = await server.boot(profile="tg")
    source_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    assert body[:600] in result
    assert "TG_SUMMARY_HIDDEN_TAIL" not in result
    assert f"TG summary 尚未生成：bucket {bucket_id}" in result
    assert f"source_hash:{source_hash}" in result
    assert f'dream(detail_ids="{bucket_id}")' in result


@pytest.mark.asyncio
async def test_valid_tg_summary_replaces_pinned_body_only_for_tg(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    body = ("原文" * 1300) + "TG_SUMMARY_SOURCE_TAIL"
    bucket_id = await server.bucket_mgr.create(body, name="summary pinned", pinned=True)
    source_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    summary = "当前状态：已压缩。协作规则：保留原文为唯一真实来源。"

    refreshed = await server.refresh_tg_summary(bucket_id, summary, source_hash)
    tg = await server.boot(profile="tg")
    talk = await server.boot(profile="talk")
    code = await server.boot(profile="code")

    assert "TG summary 已刷新" in refreshed
    assert summary in tg
    assert f"[TG 压缩版：bucket {bucket_id}；source_hash:{source_hash}]" in tg
    assert "这是压缩版；原 bucket 是唯一真实来源" in tg
    assert tg.count("这是压缩版") == 1
    assert tg.count(f'dream(detail_ids="{bucket_id}")') == 1
    assert "TG_SUMMARY_SOURCE_TAIL" not in tg
    assert "TG_SUMMARY_SOURCE_TAIL" in talk
    assert "TG_SUMMARY_SOURCE_TAIL" not in code


@pytest.mark.asyncio
async def test_content_change_makes_tg_summary_stale_but_metadata_change_does_not(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    body = "原始正文 " * 100
    bucket_id = await server.bucket_mgr.create(body, name="stale pinned", pinned=True)
    stored = await server.bucket_mgr.get(bucket_id)
    source_hash = hashlib.sha256(stored["content"].encode("utf-8")).hexdigest()
    summary = "原始摘要"
    assert "TG summary 已刷新" in await server.refresh_tg_summary(
        bucket_id, summary, source_hash
    )

    assert await server.bucket_mgr.update(bucket_id, tags=["metadata-only"])
    metadata_only = await server.boot(profile="tg")
    assert summary in metadata_only
    assert "TG summary 已过期" not in metadata_only

    changed_body = body + "内容已更新"
    assert await server.bucket_mgr.update(bucket_id, content=changed_body)
    stale = await server.boot(profile="tg")
    changed = await server.bucket_mgr.get(bucket_id)
    changed_hash = hashlib.sha256(changed["content"].encode("utf-8")).hexdigest()

    assert summary not in stale
    assert f"TG summary 已过期：bucket {bucket_id}" in stale
    assert f"source_hash:{changed_hash}" in stale
    assert f'dream(detail_ids="{bucket_id}")' in stale


@pytest.mark.asyncio
async def test_refresh_rejects_stale_source_hash_without_overwriting_summary(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    body = "初始正文"
    bucket_id = await server.bucket_mgr.create(body, name="race pinned", pinned=True)
    stale_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert await server.bucket_mgr.update(bucket_id, content="更新后的正文")

    result = await server.refresh_tg_summary(bucket_id, "不应保存", stale_hash)
    bucket = await server.bucket_mgr.get(bucket_id)

    assert "TG summary 未保存：原文已变化" in result
    assert "tg_summary" not in bucket["metadata"]
    assert "source_hash:" in result


@pytest.mark.asyncio
async def test_refresh_persists_hash_and_updated_time_without_creating_bucket(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    body = "一、当前状态\n稳定\n二、协作纪律\n明确"
    bucket_id = await server.bucket_mgr.create(body, name="contract pinned", pinned=True)
    source_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    before = await server.bucket_mgr.list_all(include_archive=True)

    result = await server.refresh_tg_summary(bucket_id, "覆盖两个 part 的紧凑摘要", source_hash)
    bucket = await server.bucket_mgr.get(bucket_id)
    after = await server.bucket_mgr.list_all(include_archive=True)

    assert "TG summary 已刷新" in result
    assert bucket["metadata"]["tg_summary"] == "覆盖两个 part 的紧凑摘要"
    assert bucket["metadata"]["tg_summary_source_hash"] == source_hash
    assert bucket["metadata"]["tg_summary_updated_at"]
    assert [item["id"] for item in after] == [item["id"] for item in before]


@pytest.mark.asyncio
async def test_refresh_rejects_oversized_summary_and_sealed_bucket(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    body = "正文"
    bucket_id = await server.bucket_mgr.create(body, name="limit pinned", pinned=True)
    source_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    sealed_id = await server.bucket_mgr.create(
        body, name="sealed pinned", pinned=True, sealed=True
    )

    oversized = await server.refresh_tg_summary(
        bucket_id, "长" * (server.TG_SUMMARY_MAX_CHARS + 1), source_hash
    )
    sealed = await server.refresh_tg_summary(sealed_id, "摘要", source_hash)

    assert "字符上限" in oversized
    assert "已封存" in sealed


def test_tg_summary_generation_contract_is_server_owned_and_covers_parts():
    from server import (
        TG_SUMMARY_GENERATION_CONTRACT,
        TG_SUMMARY_TOOL_DESCRIPTION,
    )

    assert "原 bucket 正文永远是 source of truth" in TG_SUMMARY_GENERATION_CONTRACT
    assert "dream(detail_ids=...)" in TG_SUMMARY_GENERATION_CONTRACT
    assert "中文编号 part" in TG_SUMMARY_GENERATION_CONTRACT
    assert "1200" in TG_SUMMARY_GENERATION_CONTRACT
    assert "不要附加“这是压缩版”" in TG_SUMMARY_GENERATION_CONTRACT
    assert TG_SUMMARY_TOOL_DESCRIPTION.endswith(TG_SUMMARY_GENERATION_CONTRACT)
