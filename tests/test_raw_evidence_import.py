"""Focused O5B capture, run, and idempotency tests."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from bucket_manager import BucketManager
from import_memory import ImportEngine
from raw_evidence_import import RawEvidenceImportCoordinator, parse_capture_option
from raw_evidence_store import RawEvidenceError, RawEvidenceLimits, RawEvidenceStore


def _run_kwargs(raw: bytes, *, filename: str = "upload.md", preserve_raw: bool = False):
    return {
        "run_id": "1" * 32,
        "retry_key": "retry-1",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_size_bytes": len(raw),
        "filename": filename,
        "media_type": "text/plain",
        "source_system": "dashboard",
        "source_kind": "import_upload",
        "source_scope": "dashboard_upload",
        "actor_id": "dashboard",
        "preserve_raw": preserve_raw,
        "importer_version": "test",
        "parser_version": "test",
        "chunker_version": "test",
    }


def test_capture_option_is_strict_and_default_off():
    assert parse_capture_option(None) is False
    assert parse_capture_option("0") is False
    assert parse_capture_option("1") is True
    with pytest.raises(RawEvidenceError, match="capture_option_invalid"):
        parse_capture_option("true")


def test_v1_to_v5_migration_preserves_existing_evidence(tmp_path):
    root = tmp_path / "raw-evidence"
    store = RawEvidenceStore(root)
    result = store.create(b"before migration", source_system="fixture", source_kind="item")
    with sqlite3.connect(store.registry_path) as conn:
        conn.execute("UPDATE store_schema SET schema_version = 1 WHERE singleton = 1")

    migrated = RawEvidenceStore(root)
    assert migrated.get_content(result["revision_id"]) == b"before migration"
    with sqlite3.connect(migrated.registry_path) as conn:
        assert conn.execute(
            "SELECT schema_version FROM store_schema WHERE singleton = 1"
        ).fetchone()[0] == 6
        assert conn.execute(
            "SELECT COUNT(*) FROM import_runs"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_lineage"
        ).fetchone()[0] == 0


def test_same_run_capture_reuses_logical_evidence_and_invalid_bytes(tmp_path):
    raw = b"User: hello\nAI: hi\xff"
    store = RawEvidenceStore(tmp_path / "raw-evidence")
    run = store.create_or_get_import_run(**_run_kwargs(raw))
    first = store.create_or_reuse_import_evidence(
        raw,
        run_id=run["run_id"],
        filename="upload.md",
        media_type="text/plain",
    )
    second = store.create_or_reuse_import_evidence(
        raw,
        run_id=run["run_id"],
        filename="upload.md",
        media_type="text/plain",
    )

    assert first["evidence_id"] == second["evidence_id"]
    assert first["revision_id"] == second["revision_id"]
    assert first["content_hash"] == hashlib.sha256(raw).hexdigest()
    assert store.get_content(
        first["revision_id"], allow_restricted_admin=True
    ) == raw
    assert first["privacy_class"] == "restricted_admin"
    retried_run = store.create_or_get_import_run(**_run_kwargs(raw))
    assert retried_run["retry_count"] == 1
    assert retried_run["evidence_id"] == first["evidence_id"]
    assert retried_run["revision_id"] == first["revision_id"]


def test_new_run_same_bytes_has_new_logical_identity_and_shared_cas(tmp_path):
    raw = b"same bytes"
    store = RawEvidenceStore(tmp_path / "raw-evidence")
    first_run = store.create_or_get_import_run(**_run_kwargs(raw))
    second_values = _run_kwargs(raw)
    second_values.update(run_id="2" * 32, retry_key="retry-2")
    second_run = store.create_or_get_import_run(**second_values)
    first = store.create_or_reuse_import_evidence(raw, run_id=first_run["run_id"])
    second = store.create_or_reuse_import_evidence(raw, run_id=second_run["run_id"])

    assert first["evidence_id"] != second["evidence_id"]
    assert first["revision_id"] != second["revision_id"]
    assert first["blob_relpath"] == second["blob_relpath"]


def test_capture_off_coordinator_is_never_constructed(test_config, monkeypatch):
    import raw_evidence_import

    constructed = []
    monkeypatch.setattr(
        raw_evidence_import,
        "RawEvidenceStore",
        lambda *args, **kwargs: constructed.append((args, kwargs)),
    )
    # The legacy ImportEngine constructor has no Raw Evidence side effect.
    ImportEngine(test_config, object(), object())
    assert constructed == []


@pytest.mark.asyncio
async def test_capture_precedes_decode_and_extraction(test_config, tmp_path):
    raw = b"User: hello\nAI: hi\xff"
    config = dict(test_config, raw_evidence_root=str(tmp_path / "raw-evidence"))
    bucket_manager = BucketManager(config)
    dehydrator = type("D", (), {"api_available": True})()
    engine = ImportEngine(config, bucket_manager, dehydrator)
    extracted = [{
        "name": "captured",
        "content": "captured memory",
        "domain": ["测试"],
        "tags": [],
        "importance": 5,
        "valence": 0.5,
        "arousal": 0.3,
        "preserve_raw": False,
    }]

    async def extract(_content):
        evidence = RawEvidenceStore(config["raw_evidence_root"])
        revisions = list((Path(config["raw_evidence_root"]) / "blobs").rglob("*"))
        assert revisions
        assert any(path.is_file() and path.read_bytes() == raw for path in revisions)
        return extracted

    engine._extract_memories = extract
    result = await engine.start_raw_evidence(
        raw,
        filename="upload.md",
        media_type="text/plain",
    )

    assert result["status"] == "completed"
    assert list((Path(config["buckets_dir"]) / "dynamic").rglob("*.md"))


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", [False, True])
@pytest.mark.parametrize("preserve_raw", [False, True])
async def test_capture_and_preserve_raw_are_independent(
    test_config,
    tmp_path,
    monkeypatch,
    capture,
    preserve_raw,
):
    import import_memory

    config = dict(test_config)
    raw_root = tmp_path / "raw-evidence"
    if capture:
        config["raw_evidence_root"] = str(raw_root)
    monkeypatch.setattr(
        import_memory,
        "detect_and_parse",
        lambda _content, _filename: [{"role": "user", "content": "matrix"}],
    )
    item = {
        "name": "matrix",
        "content": "preserve matrix content",
        "domain": ["测试"],
        "tags": [],
        "importance": 5,
        "valence": 0.5,
        "arousal": 0.3,
        "preserve_raw": False,
    }
    engine = ImportEngine(
        config,
        BucketManager(config),
        type("D", (), {"api_available": True})(),
    )
    engine._extract_memories = AsyncMock(return_value=[item])

    if capture:
        result = await engine.start_raw_evidence(
            b"matrix source",
            filename="matrix.txt",
            preserve_raw=preserve_raw,
            media_type="text/plain",
        )
    else:
        result = await engine.start(
            "matrix source",
            filename="matrix.txt",
            preserve_raw=preserve_raw,
        )

    assert result["status"] == "completed"
    assert result["memories_raw"] == int(preserve_raw)
    assert len(list((Path(config["buckets_dir"]) / "dynamic").rglob("*.md"))) == 1
    assert (raw_root.exists()) is capture
    if capture:
        store = RawEvidenceStore(raw_root)
        with sqlite3.connect(store.registry_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM evidence_objects").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_generic_import_similarity_does_not_destructively_merge(test_config):
    manager = BucketManager(test_config)
    existing_id = await manager.create(
        content="Ting planned a Kyoto trip on June 1",
        domain=["测试"],
    )
    existing = await manager.get(existing_id)
    existing["score"] = 100
    manager.search = AsyncMock(return_value=[existing])
    dehydrator = type("D", (), {"api_available": True})()
    dehydrator.merge = AsyncMock(
        side_effect=AssertionError("generic import must not LLM-merge by score")
    )
    engine = ImportEngine(test_config, manager, dehydrator)

    is_merged = await engine._merge_or_create_item(
        {
            "name": "trip",
            "content": "Ting planned a Kyoto trip on September 1",
            "domain": ["测试"],
            "tags": [],
            "importance": 5,
            "valence": 0.5,
            "arousal": 0.3,
        }
    )

    assert is_merged is False
    assert dehydrator.merge.await_count == 0
    assert (await manager.get(existing_id))["content"] == "Ting planned a Kyoto trip on June 1"
    assert len(await manager.list_all(include_archive=False)) == 2


@pytest.mark.asyncio
async def test_capture_limit_fails_before_decode_or_extraction(test_config, tmp_path):
    config = dict(test_config, raw_evidence_root=str(tmp_path / "raw-evidence"))
    engine = ImportEngine(
        config,
        BucketManager(config),
        type("D", (), {"api_available": True})(),
    )
    engine._extract_memories = AsyncMock(side_effect=AssertionError("extraction must not run"))
    raw = b"\xff" * (16 * 1024 * 1024 + 1)

    with pytest.raises(RawEvidenceError, match="source_size_invalid"):
        await engine.start_raw_evidence(raw, filename="too-large.bin")

    assert engine._extract_memories.await_count == 0
    store = RawEvidenceStore(config["raw_evidence_root"])
    with sqlite3.connect(store.registry_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM import_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM evidence_objects").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_capture_failure_is_fail_closed(test_config, tmp_path, monkeypatch):
    import raw_evidence_import

    config = dict(test_config, raw_evidence_root=str(tmp_path / "raw-evidence"))
    engine = ImportEngine(
        config,
        BucketManager(config),
        type("D", (), {"api_available": True})(),
    )
    engine._extract_memories = AsyncMock(return_value=[])

    def fail_capture(*_args, **_kwargs):
        raise RawEvidenceError("storage_unavailable")

    monkeypatch.setattr(raw_evidence_import.RawEvidenceImportCoordinator, "capture", fail_capture)
    with pytest.raises(RawEvidenceError, match="storage_unavailable"):
        await engine.start_raw_evidence(b"capture failure", filename="failure.txt")

    assert engine._extract_memories.await_count == 0
    assert not list((Path(config["buckets_dir"]) / "dynamic").rglob("*.md"))


@pytest.mark.asyncio
async def test_parse_failure_retains_captured_evidence(test_config, tmp_path, monkeypatch):
    import import_memory

    raw = b"not parseable but retained\xff"
    config = dict(test_config, raw_evidence_root=str(tmp_path / "raw-evidence"))
    monkeypatch.setattr(import_memory, "detect_and_parse", lambda *_args: [])
    engine = ImportEngine(
        config,
        BucketManager(config),
        type("D", (), {"api_available": True})(),
    )

    result = await engine.start_raw_evidence(raw, filename="broken.txt")
    assert result["error"] == "No conversation turns found in file"
    store = RawEvidenceStore(config["raw_evidence_root"])
    run = store.get_import_run(
        next(iter(
            row[0]
            for row in sqlite3.connect(store.registry_path).execute(
                "SELECT run_id FROM import_runs"
            ).fetchall()
        ))
    )
    assert run["status"] == "failed"
    assert run["evidence_id"] and run["revision_id"]
    assert store.get_content(
        run["revision_id"], allow_restricted_admin=True
    ) == raw


@pytest.mark.asyncio
async def test_extraction_failure_retains_evidence_and_checkpoint(test_config, tmp_path, monkeypatch):
    import import_memory

    raw = b"[2024-01-01 00:00] User: retained"
    config = dict(test_config, raw_evidence_root=str(tmp_path / "raw-evidence"))
    monkeypatch.setattr(
        import_memory,
        "detect_and_parse",
        lambda *_args: [{"role": "user", "content": "retained"}],
    )
    engine = ImportEngine(
        config,
        BucketManager(config),
        type("D", (), {"api_available": True})(),
    )
    engine._extract_memories = AsyncMock(side_effect=RuntimeError("extract failed"))

    result = await engine.start_raw_evidence(raw, filename="extract.txt")
    assert result["status"] == "error"
    store = RawEvidenceStore(config["raw_evidence_root"])
    with sqlite3.connect(store.registry_path) as conn:
        run_id = conn.execute("SELECT run_id FROM import_runs").fetchone()[0]
        run_status = conn.execute(
            "SELECT status FROM import_runs WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        item_status = conn.execute(
            "SELECT status FROM import_run_items WHERE run_id = ? AND item_kind = 'chunk'",
            (run_id,),
        ).fetchone()[0]
        revision_id = conn.execute(
            "SELECT revision_id FROM import_runs WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
    assert run_status == "failed"
    assert item_status == "extraction_failed"
    assert store.get_content(
        revision_id, allow_restricted_admin=True
    ) == raw


@pytest.mark.asyncio
async def test_memory_failure_retains_evidence_and_retryable_item(
    test_config,
    tmp_path,
    monkeypatch,
):
    import import_memory

    raw = b"[2024-01-01 00:00] User: memory failure"
    config = dict(test_config, raw_evidence_root=str(tmp_path / "raw-evidence"))
    monkeypatch.setattr(
        import_memory,
        "detect_and_parse",
        lambda *_args: [{"role": "user", "content": "memory failure"}],
    )
    engine = ImportEngine(
        config,
        BucketManager(config),
        type("D", (), {"api_available": True})(),
    )
    engine._extract_memories = AsyncMock(
        return_value=[
            {
                "name": "failure",
                "content": "memory failure content",
                "domain": ["测试"],
                "tags": [],
                "importance": 5,
                "valence": 0.5,
                "arousal": 0.3,
                "preserve_raw": False,
            }
        ]
    )
    engine.bucket_mgr.apply_import_operation = AsyncMock(
        side_effect=RuntimeError("memory write failed")
    )

    result = await engine.start_raw_evidence(raw, filename="memory.txt")
    assert result["status"] == "error"
    store = RawEvidenceStore(config["raw_evidence_root"])
    with sqlite3.connect(store.registry_path) as conn:
        run_id = conn.execute("SELECT run_id FROM import_runs").fetchone()[0]
        item_status = conn.execute(
            "SELECT status FROM import_run_items WHERE run_id = ? AND item_kind = 'memory'",
            (run_id,),
        ).fetchone()[0]
        revision_id = conn.execute(
            "SELECT revision_id FROM import_runs WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
    assert item_status == "failed"
    assert store.get_content(
        revision_id, allow_restricted_admin=True
    ) == raw


@pytest.mark.asyncio
async def test_planned_update_target_is_reused_without_second_merge(test_config, tmp_path):
    config = dict(test_config, raw_evidence_root=str(tmp_path / "raw-evidence"))
    manager = BucketManager(config)
    target_id = await manager.create(
        content="existing target",
        domain=["测试"],
        name="target",
    )
    coordinator = RawEvidenceImportCoordinator(config)
    prepared = coordinator.prepare_run(
        b"target source",
        filename="target.txt",
        media_type="text/plain",
        preserve_raw=False,
        resume=False,
    )
    coordinator.capture(
        prepared,
        b"target source",
        filename="target.txt",
        media_type="text/plain",
    )
    dehydrator = type("D", (), {"api_available": True})()
    dehydrator.merge = AsyncMock(return_value="merged target")
    engine = ImportEngine(config, manager, dehydrator)
    engine.bucket_mgr.search = AsyncMock(
        return_value=[
            {
                "id": target_id,
                "score": 100,
                "content": "existing target",
                "metadata": {
                    "pinned": False,
                    "protected": False,
                    "tags": [],
                    "domain": ["测试"],
                    "importance": 5,
                    "valence": 0.5,
                    "arousal": 0.3,
                },
            }
        ]
    )
    item = {
        "name": "target",
        "content": "new target detail",
        "domain": ["测试"],
        "tags": [],
        "importance": 5,
        "valence": 0.5,
        "arousal": 0.3,
        "preserve_raw": False,
    }
    record = await engine._o5b_plan_item(
        coordinator,
        prepared.run_id,
        0,
        0,
        item,
        False,
    )
    assert record["target_bucket_id"] == target_id
    assert dehydrator.merge.await_count == 1

    engine.bucket_mgr.search = AsyncMock(side_effect=AssertionError("search rerun"))
    dehydrator.merge = AsyncMock(side_effect=AssertionError("merge rerun"))
    replay = await manager.apply_import_operation(record["operation_key"])
    assert replay["result_id"] == target_id
    assert (await manager.get(target_id))["content"] == "merged target"


@pytest.mark.asyncio
async def test_bucket_create_and_update_operations_are_exactly_once(test_config):
    manager = BucketManager(test_config)
    create_payload = {
        "content": "created once",
        "tags": ["test"],
        "importance": 5,
        "domain": ["测试"],
        "valence": 0.5,
        "arousal": 0.3,
        "name": "once",
    }
    create_op = manager.plan_import_operation(
        "o5b:" + "a" * 64,
        operation_kind="create",
        payload=create_payload,
    )
    first = await manager.apply_import_operation(create_op["operation_key"])
    second = await manager.apply_import_operation(create_op["operation_key"])
    assert first["result_id"] == second["result_id"]
    assert len(list((Path(test_config["buckets_dir"]) / "dynamic").rglob("*.md"))) == 1

    update_payload = {"kwargs": {
        "content": "updated once",
        "tags": ["test"],
        "importance": 5,
        "domain": ["测试"],
        "valence": 0.5,
        "arousal": 0.3,
    }}
    update_op = manager.plan_import_operation(
        "o5b:" + "b" * 64,
        operation_kind="update",
        target_bucket_id=first["result_id"],
        payload=update_payload,
    )
    await manager.apply_import_operation(update_op["operation_key"])
    await manager.apply_import_operation(update_op["operation_key"])
    assert (await manager.get(first["result_id"]))["content"] == "updated once"


@pytest.mark.asyncio
async def test_memory_commit_before_import_checkpoint_replays_without_duplicate(test_config):
    manager = BucketManager(test_config)
    payload = {
        "content": "crash-safe create",
        "tags": [],
        "importance": 5,
        "domain": ["测试"],
        "valence": 0.5,
        "arousal": 0.3,
        "name": "crash-safe",
    }
    create_op = manager.plan_import_operation(
        "o5b:" + "c" * 64,
        operation_kind="create",
        payload=payload,
    )
    committed_id = await manager.create(
        **payload,
        _o5b_operation_key=create_op["operation_key"],
        _o5b_payload_digest=create_op["payload_digest"],
    )
    replay = await manager.apply_import_operation(create_op["operation_key"])
    assert replay["result_id"] == committed_id
    assert len(list((Path(test_config["buckets_dir"]) / "dynamic").rglob("*.md"))) == 1

    update_op = manager.plan_import_operation(
        "o5b:" + "d" * 64,
        operation_kind="update",
        target_bucket_id=committed_id,
        payload={"kwargs": {"content": "crash-safe update"}},
    )
    with patch.object(manager, "_write_post_atomic", wraps=manager._write_post_atomic) as writer:
        assert await manager.update(
            committed_id,
            content="crash-safe update",
            _o5b_operation_key=update_op["operation_key"],
            _o5b_payload_digest=update_op["payload_digest"],
        )
        replay_update = await manager.apply_import_operation(update_op["operation_key"])
        assert replay_update["result_id"] == committed_id
        assert writer.call_count == 1
    assert (await manager.get(committed_id))["content"] == "crash-safe update"
