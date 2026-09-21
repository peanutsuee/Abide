import importlib
import sys
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "phase2-superseded-seal")
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.bucket_mgr.touch = AsyncMock(return_value=True)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None, **kwargs: content
    )
    return server


async def _bucket(server, content, *, name, **kwargs):
    return await server.bucket_mgr.create(content=content, name=name, **kwargs)


@pytest.mark.asyncio
async def test_supersession_forward_reverse_none_cancel_relink_and_omitted(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    old_id = await _bucket(server, "old body", name="Old")
    first_id = await _bucket(server, "first successor", name="First", pinned=True)
    second_id = await _bucket(server, "second successor", name="Second")

    assert server._superseded_by_id((await server.bucket_mgr.get(old_id))["metadata"]) == ""
    result = await server.trace(old_id, superseded_by=first_id)
    old = await server.bucket_mgr.get(old_id)
    first = await server.bucket_mgr.get(first_id)
    assert f"superseded_by={first_id} (First)" in result
    assert old["metadata"]["superseded_by"] == first_id
    assert old["metadata"].get("superseded_at")
    assert first["metadata"]["supersedes"] == [old_id]

    await server.trace(old_id)
    assert (await server.bucket_mgr.get(old_id))["metadata"]["superseded_by"] == first_id

    await server.trace(old_id, superseded_by=second_id)
    assert old_id not in (await server.bucket_mgr.get(first_id))["metadata"].get("supersedes", [])
    assert (await server.bucket_mgr.get(second_id))["metadata"]["supersedes"] == [old_id]

    await server.trace(old_id, superseded_by=second_id)
    assert (await server.bucket_mgr.get(second_id))["metadata"]["supersedes"] == [old_id]

    await server.trace(old_id, superseded_by="")
    old = await server.bucket_mgr.get(old_id)
    assert "superseded_by" not in old["metadata"]
    assert "superseded_at" not in old["metadata"]
    assert old_id not in (await server.bucket_mgr.get(second_id))["metadata"].get("supersedes", [])

    await server.trace(old_id, superseded_by="none")
    old = await server.bucket_mgr.get(old_id)
    assert old["metadata"]["superseded_by"] == "none"
    assert old["metadata"].get("superseded_at")
    assert old_id not in (await server.bucket_mgr.get(second_id))["metadata"].get("supersedes", [])


@pytest.mark.asyncio
async def test_supersession_rejects_invalid_targets_and_batch_without_partial_write(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    source_id = await _bucket(server, "source", name="Source")
    peer_id = await _bucket(server, "peer", name="Peer")
    sealed_id = await _bucket(server, "sealed", name="Sealed", sealed=True)

    for target, expected in [
        ("missing", "未找到"),
        (source_id, "不能指向自身"),
        (sealed_id, "已封存"),
    ]:
        result = await server.trace(source_id, superseded_by=target)
        assert expected in result
        assert "superseded_by" not in (await server.bucket_mgr.get(source_id))["metadata"]

    result = await server.trace(f"{source_id},{peer_id}", superseded_by="none")
    assert "批量 trace 不支持 superseded_by" in result
    assert "superseded_by" not in (await server.bucket_mgr.get(source_id))["metadata"]
    assert "superseded_by" not in (await server.bucket_mgr.get(peer_id))["metadata"]


@pytest.mark.asyncio
async def test_supersession_metadata_has_no_body_history_snapshot(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    source_id = await _bucket(server, "original source body", name="Source")
    target_id = await _bucket(server, "target body", name="Target")

    await server.trace(source_id, superseded_by=target_id)

    assert (await server.bucket_mgr.get(source_id))["content"] == "original source body"
    assert server.bucket_mgr.get_history(source_id) == []


@pytest.mark.asyncio
async def test_merge_rewires_inbound_and_cleans_outgoing_reverse_without_waking_dormant(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    successor_id = await _bucket(server, "successor", name="Successor")
    source_id = await _bucket(server, "source", name="Source")
    target_id = await _bucket(server, "target", name="Target")
    inbound_one = await _bucket(server, "inbound one", name="Inbound one")
    inbound_two = await _bucket(server, "inbound two", name="Inbound two")
    await server.trace(source_id, superseded_by=successor_id)
    await server.trace(inbound_one, superseded_by=source_id)
    await server.trace(inbound_two, superseded_by=source_id)
    await server.trace(target_id, dormant=1)

    result = await server.trace(target_id, merge=source_id)

    assert "已合并" in result
    assert await server.bucket_mgr.get(source_id) is None
    successor = await server.bucket_mgr.get(successor_id)
    target = await server.bucket_mgr.get(target_id)
    assert source_id not in successor["metadata"].get("supersedes", [])
    assert target["metadata"]["dormant"] is True
    for inbound_id in (inbound_one, inbound_two):
        inbound = await server.bucket_mgr.get(inbound_id)
        assert inbound["metadata"]["superseded_by"] == target_id
        assert inbound_id in target["metadata"].get("supersedes", [])
    all_buckets = await server.bucket_mgr.list_all(include_archive=True)
    assert all(source_id not in server._supersedes_ids(bucket["metadata"]) for bucket in all_buckets)


@pytest.mark.asyncio
async def test_merge_into_active_target_keeps_active_behavior(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    source_id = await _bucket(server, "source", name="Source")
    target_id = await _bucket(server, "target", name="Target")

    result = await server.trace(target_id, merge=source_id)

    assert "已合并" in result
    assert (await server.bucket_mgr.get(target_id))["metadata"]["dormant"] is False


@pytest.mark.asyncio
async def test_delete_rejects_inbound_and_cleans_own_outgoing_reverse(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    target_id = await _bucket(server, "target", name="Target")
    inbound_id = await _bucket(server, "inbound", name="Inbound")
    await server.trace(inbound_id, superseded_by=target_id)

    blocked = await server.trace(target_id, delete=True)
    assert "仍被以下作废关系引用" in blocked
    assert await server.bucket_mgr.get(target_id) is not None
    assert (await server.bucket_mgr.get(inbound_id))["metadata"]["superseded_by"] == target_id

    successor_id = await _bucket(server, "successor", name="Successor")
    old_id = await _bucket(server, "old", name="Old")
    await server.trace(old_id, superseded_by=successor_id)
    preview = await server.trace(old_id, delete=True)
    token = next(
        line.split(":", 1)[1].strip()
        for line in preview.splitlines()
        if line.startswith("confirm_token:")
    )
    deleted = await server.trace(old_id, delete=True, confirm_token=token)
    assert "已遗忘" in deleted
    assert old_id not in (await server.bucket_mgr.get(successor_id))["metadata"].get("supersedes", [])


@pytest.mark.asyncio
async def test_superseded_readouts_and_ranking_keep_phase1_presentation(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    old_id = await _bucket(server, "phase two searchable old body", name="Old")
    target_id = await _bucket(server, "replacement body", name="Current")
    none_id = await _bucket(server, "phase two no successor body", name="No successor")
    await server.trace(old_id, superseded_by=target_id)
    await server.trace(none_id, superseded_by="none")

    old = await server.bucket_mgr.get(old_id)
    none_bucket = await server.bucket_mgr.get(none_id)

    async def search(*args, trace=None, **kwargs):
        if trace is not None:
            trace["candidates"] = [
                {"id": old_id, "scores": {"fuzzy_lexical": 0.8, "semantic": 0.0}},
                {"id": none_id, "scores": {"fuzzy_lexical": 0.7, "semantic": 0.0}},
            ]
        return [old, none_bucket]

    server.bucket_mgr.search = AsyncMock(side_effect=search)
    breath = await server.breath(query="not an exact anchor")
    assert "[sim=0.80]" in breath
    assert f"⊘已作废→{target_id}(Current)" in breath
    assert "⊘已作废" in breath
    assert f"当前有效：[{target_id}] Current（取代了 {old_id}）" in breath
    assert "replacement body" not in breath

    detail = await server.dream(detail_ids=old_id)
    assert f"此桶已被 {target_id}(Current) 取代于" in detail
    pulse = await server.pulse(show_all=True)
    pulse_line = next(line for line in pulse.splitlines() if f"bucket_id:{old_id}" in line)
    assert pulse_line.startswith("⊘")

    results = await server.bucket_mgr.search("phase two searchable")
    assert any(bucket["id"] == old_id for bucket in results)


@pytest.mark.asyncio
async def test_breath_does_not_repeat_successor_when_it_is_already_returned(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    old_id = await _bucket(server, "obsolete body", name="Old")
    target_id = await _bucket(server, "current body", name="Current")
    await server.trace(old_id, superseded_by=target_id)
    old = await server.bucket_mgr.get(old_id)
    target = await server.bucket_mgr.get(target_id)

    async def search(*args, trace=None, **kwargs):
        if trace is not None:
            trace["candidates"] = [
                {"id": old_id, "scores": {"fuzzy_lexical": 0.8, "semantic": 0.0}},
                {"id": target_id, "scores": {"fuzzy_lexical": 0.7, "semantic": 0.0}},
            ]
        return [old, target]

    server.bucket_mgr.search = AsyncMock(side_effect=search)
    result = await server.breath(query="not an exact anchor")

    assert f"⊘已作废→{target_id}(Current)" in result
    assert "当前有效：" not in result
