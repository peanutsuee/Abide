import frontmatter
import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backfill_embeddings import backfill_batch
from bucket_manager import BucketManager


class FakeEmbeddingEngine:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.generate_and_store = AsyncMock(return_value=True)
        self.search_similar = AsyncMock(return_value={})
        self.delete_embedding = Mock(return_value=True)
        self.last_error = ""
        self.last_error_details = {}
        self.model = "test-model"


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


@pytest.mark.asyncio
async def test_sealed_create_scrubs_locally_without_provider_call(test_config):
    engine = FakeEmbeddingEngine(enabled=False)
    manager = BucketManager(test_config, embedding_engine=engine)

    bucket_id = await manager.create("sealed body", sealed=True)

    assert (await manager.get(bucket_id))["metadata"]["sealed"] == 1
    engine.delete_embedding.assert_called_once_with(bucket_id)
    engine.generate_and_store.assert_not_awaited()


@pytest.mark.asyncio
async def test_sealed_create_cleanup_failure_prevents_record(test_config):
    engine = FakeEmbeddingEngine(enabled=False)
    engine.delete_embedding.side_effect = RuntimeError("sqlite unavailable")
    manager = BucketManager(test_config, embedding_engine=engine)

    with pytest.raises(RuntimeError, match="sealed_embedding_cleanup_failed"):
        await manager.create("must not persist", sealed=True)

    assert list(Path(manager.dynamic_dir).rglob("*.md")) == []


@pytest.mark.asyncio
async def test_sealed_transition_scrubs_before_persist_and_blocks_on_failure(test_config):
    engine = FakeEmbeddingEngine(enabled=False)
    manager = BucketManager(test_config, embedding_engine=engine)
    bucket_id = await manager.create("unsealed body")
    path = manager._find_bucket_file(bucket_id)
    engine.delete_embedding.reset_mock()

    observed = []

    def scrub(current_id):
        observed.append(frontmatter.load(path).get("sealed", 0))

    engine.delete_embedding.side_effect = scrub
    assert await manager.update(bucket_id, content="sealed body", sealed=True)
    assert observed == [0]
    stored = await manager.get(bucket_id)
    assert stored["metadata"]["sealed"] == 1
    assert stored["content"] == "sealed body"
    engine.generate_and_store.assert_not_awaited()

    engine.delete_embedding.reset_mock()
    engine.delete_embedding.side_effect = RuntimeError("cleanup failed")
    assert not await manager.update(bucket_id, content="blocked body", sealed=True)
    stored = await manager.get(bucket_id)
    assert stored["content"] == "sealed body"
    assert stored["metadata"]["sealed"] == 1


@pytest.mark.asyncio
async def test_sealed_content_update_scrubs_without_refresh_and_metadata_only_is_clean(test_config):
    engine = FakeEmbeddingEngine(enabled=False)
    manager = BucketManager(test_config, embedding_engine=engine)
    bucket_id = await manager.create("sealed body", sealed=True)
    engine.delete_embedding.reset_mock()

    assert await manager.update(bucket_id, content="sealed revision")
    engine.delete_embedding.assert_called_once_with(bucket_id)
    engine.generate_and_store.assert_not_awaited()

    engine.delete_embedding.reset_mock()
    assert await manager.update(bucket_id, name="metadata only")
    engine.delete_embedding.assert_not_called()
    engine.generate_and_store.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsealing_writes_final_content_then_generates_once(test_config):
    engine = FakeEmbeddingEngine(enabled=False)
    manager = BucketManager(test_config, embedding_engine=engine)
    bucket_id = await manager.create("sealed body", sealed=True)
    engine.delete_embedding.reset_mock()
    engine.enabled = True

    assert await manager.update(bucket_id, content="final unsealed body", sealed=False)

    engine.delete_embedding.assert_not_called()
    engine.generate_and_store.assert_awaited_once_with(bucket_id, "final unsealed body")
    assert (await manager.get(bucket_id))["metadata"]["sealed"] == 0


@pytest.mark.asyncio
async def test_unsealing_provider_failure_keeps_final_state_without_retry(test_config):
    engine = FakeEmbeddingEngine(enabled=False)
    manager = BucketManager(test_config, embedding_engine=engine)
    bucket_id = await manager.create("sealed body", sealed=True)
    engine.delete_embedding.reset_mock()
    engine.enabled = True
    engine.generate_and_store.side_effect = RuntimeError("provider down")

    assert await manager.update(bucket_id, content="final unsealed body", sealed=False)

    stored = await manager.get(bucket_id)
    assert stored["metadata"]["sealed"] == 0
    assert stored["content"] == "final unsealed body"
    engine.delete_embedding.assert_not_called()
    engine.generate_and_store.assert_awaited_once_with(bucket_id, "final unsealed body")


@pytest.mark.asyncio
async def test_unsealed_content_provider_failure_keeps_content_without_retry(test_config):
    engine = FakeEmbeddingEngine(enabled=False)
    manager = BucketManager(test_config, embedding_engine=engine)
    bucket_id = await manager.create("ordinary body")
    engine.enabled = True
    engine.generate_and_store.reset_mock()
    engine.generate_and_store.side_effect = RuntimeError("provider down")

    assert await manager.update(bucket_id, content="ordinary revised body")

    assert (await manager.get(bucket_id))["content"] == "ordinary revised body"
    engine.generate_and_store.assert_awaited_once_with(bucket_id, "ordinary revised body")


@pytest.mark.asyncio
async def test_delete_scrubs_before_file_removal_and_preserves_file_on_failure(test_config):
    engine = FakeEmbeddingEngine(enabled=False)
    manager = BucketManager(test_config, embedding_engine=engine)
    bucket_id = await manager.create("deletable")
    path = manager._find_bucket_file(bucket_id)
    engine.delete_embedding.reset_mock()

    observed = []

    def scrub(current_id):
        observed.append(path and os.path.exists(path))

    engine.delete_embedding.side_effect = scrub
    assert await manager.delete(bucket_id)
    assert observed == [True]
    assert not os.path.exists(path)

    missing_id = "missing-vector-id"
    assert not await manager.delete(missing_id)
    engine.delete_embedding.assert_any_call(missing_id)

    surviving_id = await manager.create("surviving")
    surviving_path = manager._find_bucket_file(surviving_id)
    engine.delete_embedding.side_effect = RuntimeError("vector delete failed")
    assert not await manager.delete(surviving_id)
    assert os.path.exists(surviving_path)


@pytest.mark.asyncio
async def test_o5b_sealed_cleanup_failure_does_not_apply_marker(test_config):
    engine = FakeEmbeddingEngine(enabled=False)
    manager = BucketManager(test_config, embedding_engine=engine)
    payload = {
        "content": "o5b sealed",
        "tags": [],
        "importance": 5,
        "domain": ["测试"],
        "valence": 0.5,
        "arousal": 0.3,
        "name": "sealed-o5b",
    }
    operation = manager.plan_import_operation(
        "o5b:" + "e" * 64,
        operation_kind="create",
        payload=payload,
    )
    engine.delete_embedding.side_effect = RuntimeError("cleanup failed")

    with pytest.raises(RuntimeError):
        await manager.create(
            **payload,
            sealed=True,
            _o5b_operation_key=operation["operation_key"],
            _o5b_payload_digest=operation["payload_digest"],
        )

    assert manager._get_import_operation(operation["operation_key"])["status"] == "planned"


@pytest.mark.asyncio
async def test_o5b_sealed_create_replay_returns_before_lifecycle(test_config):
    engine = FakeEmbeddingEngine(enabled=False)
    manager = BucketManager(test_config, embedding_engine=engine)
    payload = {
        "content": "o5b sealed create",
        "tags": [],
        "importance": 5,
        "domain": ["测试"],
        "valence": 0.5,
        "arousal": 0.3,
        "name": "sealed-o5b-create",
    }
    operation = manager.plan_import_operation(
        "o5b:" + "f" * 64,
        operation_kind="create",
        payload=payload,
    )

    result_id = await manager.create(
        **payload,
        sealed=True,
        _o5b_operation_key=operation["operation_key"],
        _o5b_payload_digest=operation["payload_digest"],
    )
    path = manager._find_bucket_file(result_id)
    first_bytes = Path(path).read_bytes()
    first_delete_count = engine.delete_embedding.call_count
    first_generate_count = engine.generate_and_store.await_count
    first_marker = manager.inspect_import_operation(operation["operation_key"])["marker"]

    replay = await manager.apply_import_operation(operation["operation_key"])

    assert replay["result_id"] == result_id
    assert Path(path).read_bytes() == first_bytes
    assert engine.delete_embedding.call_count == first_delete_count
    assert engine.generate_and_store.await_count == first_generate_count == 0
    inspected = manager.inspect_import_operation(operation["operation_key"])
    assert inspected["status"] == "applied"
    assert inspected["marker"] == first_marker


@pytest.mark.asyncio
async def test_o5b_sealed_update_replay_returns_before_lifecycle(test_config):
    engine = FakeEmbeddingEngine(enabled=False)
    manager = BucketManager(test_config, embedding_engine=engine)
    bucket_id = await manager.create("ordinary body")
    operation = manager.plan_import_operation(
        "o5b:" + "g" * 64,
        operation_kind="update",
        target_bucket_id=bucket_id,
        payload={"kwargs": {"content": "sealed update", "sealed": True}},
    )
    engine.delete_embedding.reset_mock()

    assert await manager.update(
        bucket_id,
        content="sealed update",
        sealed=True,
        _o5b_operation_key=operation["operation_key"],
        _o5b_payload_digest=operation["payload_digest"],
    )
    path = manager._find_bucket_file(bucket_id)
    first_bytes = Path(path).read_bytes()
    first_delete_count = engine.delete_embedding.call_count
    first_marker = manager.inspect_import_operation(operation["operation_key"])["marker"]

    replay = await manager.apply_import_operation(operation["operation_key"])

    assert replay["result_id"] == bucket_id
    assert Path(path).read_bytes() == first_bytes
    assert engine.delete_embedding.call_count == first_delete_count == 1
    engine.generate_and_store.assert_not_awaited()
    inspected = manager.inspect_import_operation(operation["operation_key"])
    assert inspected["status"] == "applied"
    assert inspected["marker"] == first_marker


@pytest.mark.asyncio
async def test_ordinary_search_excludes_sealed_before_vector_ranking(test_config):
    engine = FakeEmbeddingEngine(enabled=False)
    manager = BucketManager(test_config, embedding_engine=engine)
    ordinary_id = await manager.create("shared search phrase ordinary")
    sealed_id = await manager.create("shared search phrase sealed", sealed=True)
    engine.enabled = True
    engine.search_similar.return_value = {
        sealed_id: 1.0,
        ordinary_id: 0.9,
    }

    ordinary = await manager.search("shared search phrase")
    call = engine.search_similar.await_args
    assert sealed_id not in {str(item) for item in call.kwargs["candidate_ids"]}
    assert sealed_id not in {bucket["id"] for bucket in ordinary}

    engine.search_similar.reset_mock()
    explicit = await manager.search("shared search phrase", include_sealed=True)
    assert "candidate_ids" not in engine.search_similar.await_args.kwargs
    assert sealed_id in {bucket["id"] for bucket in explicit}


@pytest.mark.asyncio
async def test_backfill_skips_sealed_active_and_archive_buckets():
    buckets = [
        {"id": "ordinary", "content": "ordinary"},
        {"id": "sealed", "content": "private", "metadata": {"sealed": 1}},
        {"id": "empty", "content": "  "},
    ]
    bucket_mgr = SimpleNamespace(list_all=AsyncMock(return_value=buckets))
    engine = SimpleNamespace(
        enabled=True,
        model="test-model",
        get_embedding=AsyncMock(return_value=None),
        generate_and_store=AsyncMock(return_value=True),
        last_error="",
        last_error_details={},
    )

    result = await backfill_batch(bucket_mgr, engine, limit=20)

    assert result["eligible_buckets"] == 1
    assert result["attempted"] == 1
    engine.generate_and_store.assert_awaited_once_with("ordinary", "ordinary")


@pytest.mark.asyncio
async def test_merge_rejects_cross_boundary_and_allows_sealed_to_sealed(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server.embedding_engine.enabled = False
    server.bucket_mgr.embedding_engine.enabled = False

    target = await server.bucket_mgr.create("ordinary target")
    sealed_source = await server.bucket_mgr.create("sealed source", sealed=True)
    target_before = await server.bucket_mgr.get(target)
    source_before = await server.bucket_mgr.get(sealed_source)

    rejected = await server._merge_bucket_into_target(target, sealed_source)

    assert "隐私边界" in rejected
    assert (await server.bucket_mgr.get(target))["content"] == target_before["content"]
    assert (await server.bucket_mgr.get(sealed_source))["content"] == source_before["content"]

    sealed_target = await server.bucket_mgr.create("sealed target", sealed=True)
    sealed_source_2 = await server.bucket_mgr.create("sealed source 2", sealed=True)
    merged = await server._merge_bucket_into_target(sealed_target, sealed_source_2)

    assert "已合并" in merged
    assert (await server.bucket_mgr.get(sealed_source_2)) is None
    assert (await server.bucket_mgr.get(sealed_target))["metadata"]["sealed"] == 1


@pytest.mark.asyncio
async def test_merge_keeps_source_when_target_update_succeeds_but_delete_fails(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server.embedding_engine.enabled = False
    server.bucket_mgr.embedding_engine.enabled = False

    target = await server.bucket_mgr.create("merge target")
    source = await server.bucket_mgr.create("merge source")
    server.bucket_mgr.delete = AsyncMock(return_value=False)

    result = await server._merge_bucket_into_target(target, source)

    assert "源桶删除失败" in result
    assert "merge source" in (await server.bucket_mgr.get(target))["content"]
    assert (await server.bucket_mgr.get(source)) is not None


@pytest.mark.asyncio
async def test_merge_rejects_protected_targets_and_feel_buckets(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server.embedding_engine.enabled = False
    server.bucket_mgr.embedding_engine.enabled = False

    protected_target = await server.bucket_mgr.create("protected target", pinned=True)
    source = await server.bucket_mgr.create("ordinary source")
    rejected = await server._merge_bucket_into_target(protected_target, source)
    assert "目标桶" in rejected
    assert (await server.bucket_mgr.get(source)) is not None

    feel_target = await server.bucket_mgr.create("feel target", bucket_type="feel")
    ordinary_source = await server.bucket_mgr.create("ordinary source 2")
    rejected_feel = await server._merge_bucket_into_target(feel_target, ordinary_source)
    assert "feel" in rejected_feel
    assert (await server.bucket_mgr.get(ordinary_source)) is not None


@pytest.mark.asyncio
async def test_automatic_merge_defensively_rejects_a_sealed_search_result(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server.embedding_engine.enabled = False
    server.bucket_mgr.embedding_engine.enabled = False
    sealed_id = await server.bucket_mgr.create("sealed automatic candidate", sealed=True)
    sealed = await server.bucket_mgr.get(sealed_id)
    sealed["score"] = 100
    server.bucket_mgr.search = AsyncMock(return_value=[sealed])
    server.dehydrator.merge = AsyncMock(side_effect=AssertionError("sealed target selected"))

    result_id, merged = await server._merge_or_create(
        "new automatic content",
        [],
        5,
        ["测试"],
        0.5,
        0.3,
    )

    assert merged is False
    assert result_id != sealed_id
    assert (await server.bucket_mgr.get(sealed_id))["content"] == "sealed automatic candidate"


@pytest.mark.asyncio
async def test_dashboard_search_explicitly_keeps_existing_sealed_visibility(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._require_auth = lambda request: None
    server.bucket_mgr.search = AsyncMock(return_value=[])

    from starlette.requests import Request

    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/api/search",
        "query_string": b"q=private",
        "headers": [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "root_path": "",
    })
    await server.api_search(request)

    server.bucket_mgr.search.assert_awaited_once_with(
        "private",
        limit=10,
        include_sealed=True,
    )
