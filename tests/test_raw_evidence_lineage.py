"""Focused O5C lineage, migration, and reconciliation tests."""

from __future__ import annotations

import hashlib
import sqlite3
from unittest.mock import AsyncMock

import pytest

from bucket_manager import BucketManager
from import_memory import ImportEngine
import import_memory
from raw_evidence_import import RawEvidenceImportCoordinator
from raw_evidence_store import RawEvidenceError, RawEvidenceStore


def _captured(tmp_path, *, raw: bytes = b"User: source\nAI: answer"):
    config = {
        "buckets_dir": str(tmp_path / "buckets"),
        "raw_evidence_root": str(tmp_path / "raw-evidence"),
    }
    coordinator = RawEvidenceImportCoordinator(config)
    prepared = coordinator.prepare_run(
        raw,
        filename="source.txt",
        media_type="text/plain",
        preserve_raw=False,
        resume=False,
    )
    captured = coordinator.capture(
        prepared,
        raw,
        filename="source.txt",
        media_type="text/plain",
    )
    return config, coordinator, prepared, captured


def _payload(content: str = "derived memory"):
    return {
        "content": content,
        "tags": [],
        "importance": 5,
        "domain": ["测试"],
        "valence": 0.5,
        "arousal": 0.3,
        "name": "derived",
    }


def _item(coordinator, prepared, captured, *, operation_key, result_id, item_key):
    return coordinator.upsert_item(
        prepared.run_id,
        item_key,
        item_kind="memory",
        input_digest=hashlib.sha256(item_key.encode()).hexdigest(),
        status="memory_planned",
        evidence_id=captured["evidence_id"],
        revision_id=captured["revision_id"],
        operation_key=operation_key,
        operation_kind="create",
        result_id=result_id,
    )


def test_v2_to_v5_migration_has_lineage_table_without_backfill(tmp_path):
    root = tmp_path / "raw-evidence"
    store = RawEvidenceStore(root)
    evidence = store.create(b"legacy evidence", source_system="fixture", source_kind="item")
    with sqlite3.connect(store.registry_path) as conn:
        conn.execute(
            "UPDATE store_schema SET schema_version = 2 WHERE singleton = 1"
        )

    migrated = RawEvidenceStore(root)
    assert migrated.get_content(evidence["revision_id"]) == b"legacy evidence"
    with sqlite3.connect(migrated.registry_path) as conn:
        assert conn.execute(
            "SELECT schema_version FROM store_schema WHERE singleton = 1"
        ).fetchone()[0] == 6
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_lineage"
        ).fetchone()[0] == 0
        indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list(memory_lineage)").fetchall()
        }
        assert {
            "idx_memory_lineage_memory_time",
            "idx_memory_lineage_evidence",
            "idx_memory_lineage_run_item",
            "idx_memory_lineage_status",
        } <= indexes


def test_future_schema_refuses_safely(tmp_path):
    root = tmp_path / "raw-evidence"
    store = RawEvidenceStore(root)
    with sqlite3.connect(store.registry_path) as conn:
        conn.execute(
            "UPDATE store_schema SET schema_version = 99 WHERE singleton = 1"
        )
    with pytest.raises(RawEvidenceError, match="schema_unsupported"):
        RawEvidenceStore(root)


@pytest.mark.asyncio
async def test_lineage_intent_is_idempotent_and_reconciles_atomic_marker(tmp_path):
    config, coordinator, prepared, captured = _captured(tmp_path)
    manager = BucketManager(config)
    operation_key = "o5b:" + "1" * 64
    mutation_id = hashlib.sha256(operation_key.encode()).hexdigest()
    operation = manager.plan_import_operation(
        operation_key,
        operation_kind="create",
        payload=_payload(),
        memory_mutation_id=mutation_id,
    )
    item_key = "chunk:0:item:0"
    _item(
        coordinator,
        prepared,
        captured,
        operation_key=operation_key,
        result_id=operation["result_id"],
        item_key=item_key,
    )
    first = coordinator.create_lineage_intent(
        run_id=prepared.run_id,
        run_item_key=item_key,
        operation_key=operation_key,
        memory_id=operation["result_id"],
        memory_mutation_id=mutation_id,
        evidence_id=captured["evidence_id"],
        revision_id=captured["revision_id"],
        lineage_kind="created",
    )
    second = coordinator.create_lineage_intent(
        run_id=prepared.run_id,
        run_item_key=item_key,
        operation_key=operation_key,
        memory_id=operation["result_id"],
        memory_mutation_id=mutation_id,
        evidence_id=captured["evidence_id"],
        revision_id=captured["revision_id"],
        lineage_kind="created",
    )
    assert first["lineage_id"] == second["lineage_id"]
    assert first["status"] == "pending"

    await manager.apply_import_operation(
        operation_key,
        memory_mutation_id=mutation_id,
    )
    reconciled = coordinator.reconcile_pending_lineage(
        manager,
        run_id=prepared.run_id,
    )
    assert [row["status"] for row in reconciled] == ["complete"]
    assert coordinator.list_lineage(run_id=prepared.run_id)[0]["status"] == "complete"
    loaded = await manager.get(operation["result_id"])
    assert loaded is not None
    assert "_ob_import_operations" not in loaded["metadata"]
    assert "memory_mutation_id" not in loaded["metadata"]


@pytest.mark.asyncio
async def test_pending_lineage_waits_for_commit_then_reconciles_once(tmp_path):
    config, coordinator, prepared, captured = _captured(tmp_path)
    manager = BucketManager(config)
    operation_key = "o5b:" + "3" * 64
    mutation_id = hashlib.sha256(operation_key.encode()).hexdigest()
    operation = manager.plan_import_operation(
        operation_key,
        operation_kind="create",
        payload=_payload("retryable"),
        memory_mutation_id=mutation_id,
    )
    item_key = "chunk:0:item:0"
    _item(
        coordinator,
        prepared,
        captured,
        operation_key=operation_key,
        result_id=operation["result_id"],
        item_key=item_key,
    )
    coordinator.create_lineage_intent(
        run_id=prepared.run_id,
        run_item_key=item_key,
        operation_key=operation_key,
        memory_id=operation["result_id"],
        memory_mutation_id=mutation_id,
        evidence_id=captured["evidence_id"],
        revision_id=captured["revision_id"],
        lineage_kind="created",
    )
    assert coordinator.reconcile_pending_lineage(
        manager, run_id=prepared.run_id
    ) == []
    assert coordinator.list_lineage(run_id=prepared.run_id)[0]["status"] == "pending"

    await manager.apply_import_operation(
        operation_key,
        memory_mutation_id=mutation_id,
    )
    first = coordinator.reconcile_pending_lineage(manager, run_id=prepared.run_id)
    second = coordinator.reconcile_pending_lineage(manager, run_id=prepared.run_id)
    assert [row["status"] for row in first] == ["complete"]
    assert second == []
    assert len(coordinator.list_lineage(run_id=prepared.run_id)) == 1


@pytest.mark.asyncio
async def test_one_evidence_can_prove_multiple_memory_mutations(tmp_path):
    config, coordinator, prepared, captured = _captured(tmp_path)
    manager = BucketManager(config)
    for index in (4, 5):
        operation_key = "o5b:" + str(index) * 64
        mutation_id = hashlib.sha256(operation_key.encode()).hexdigest()
        operation = manager.plan_import_operation(
            operation_key,
            operation_kind="create",
            payload=_payload(f"memory {index}"),
            memory_mutation_id=mutation_id,
        )
        item_key = f"chunk:0:item:{index}"
        _item(
            coordinator,
            prepared,
            captured,
            operation_key=operation_key,
            result_id=operation["result_id"],
            item_key=item_key,
        )
        coordinator.create_lineage_intent(
            run_id=prepared.run_id,
            run_item_key=item_key,
            operation_key=operation_key,
            memory_id=operation["result_id"],
            memory_mutation_id=mutation_id,
            evidence_id=captured["evidence_id"],
            revision_id=captured["revision_id"],
            lineage_kind="created",
        )
        await manager.apply_import_operation(
            operation_key,
            memory_mutation_id=mutation_id,
        )
    reconciled = coordinator.reconcile_pending_lineage(manager, run_id=prepared.run_id)
    edges = coordinator.list_lineage(run_id=prepared.run_id)
    assert len(reconciled) == len(edges) == 2
    assert {edge["status"] for edge in edges} == {"complete"}
    assert {edge["evidence_id"] for edge in edges} == {captured["evidence_id"]}
    assert len({edge["memory_id"] for edge in edges}) == 2
    assert len({edge["memory_mutation_id"] for edge in edges}) == 2


@pytest.mark.asyncio
async def test_new_run_same_bytes_creates_distinct_lineage_edges(tmp_path):
    config, first_coordinator, first_prepared, first_capture = _captured(tmp_path)
    second_coordinator = RawEvidenceImportCoordinator(config)
    second_prepared = second_coordinator.prepare_run(
        b"User: source\nAI: answer",
        filename="source.txt",
        media_type="text/plain",
        preserve_raw=False,
        resume=False,
    )
    second_capture = second_coordinator.capture(
        second_prepared,
        b"User: source\nAI: answer",
        filename="source.txt",
        media_type="text/plain",
    )
    assert first_capture["blob_relpath"] == second_capture["blob_relpath"]
    assert first_capture["evidence_id"] != second_capture["evidence_id"]

    manager = BucketManager(config)
    memory_id = await manager.create(**_payload())
    for coordinator, prepared, capture, suffix in (
        (first_coordinator, first_prepared, first_capture, "1"),
        (second_coordinator, second_prepared, second_capture, "2"),
    ):
        operation_key = "o5b:" + suffix * 64
        mutation_id = hashlib.sha256(operation_key.encode()).hexdigest()
        item_key = f"chunk:0:item:{suffix}"
        coordinator.upsert_item(
            prepared.run_id,
            item_key,
            item_kind="memory",
            input_digest=hashlib.sha256(item_key.encode()).hexdigest(),
            status="memory_planned",
            evidence_id=capture["evidence_id"],
            revision_id=capture["revision_id"],
            operation_key=operation_key,
            operation_kind="update",
            target_bucket_id=memory_id,
            result_id=memory_id,
        )
        coordinator.create_lineage_intent(
            run_id=prepared.run_id,
            run_item_key=item_key,
            operation_key=operation_key,
            memory_id=memory_id,
            memory_mutation_id=mutation_id,
            evidence_id=capture["evidence_id"],
            revision_id=capture["revision_id"],
            lineage_kind="contributed_update",
        )
    first_edges = first_coordinator.list_lineage(run_id=first_prepared.run_id)
    second_edges = second_coordinator.list_lineage(run_id=second_prepared.run_id)
    assert len(first_edges) == len(second_edges) == 1
    assert first_edges[0]["memory_id"] == second_edges[0]["memory_id"] == memory_id
    assert first_edges[0]["lineage_id"] != second_edges[0]["lineage_id"]


@pytest.mark.asyncio
async def test_missing_marker_never_fabricates_completed_lineage(tmp_path):
    config, coordinator, prepared, captured = _captured(tmp_path)
    manager = BucketManager(config)
    operation_key = "o5b:" + "2" * 64
    mutation_id = hashlib.sha256(operation_key.encode()).hexdigest()
    operation = manager.plan_import_operation(
        operation_key,
        operation_kind="create",
        payload=_payload("missing marker"),
        memory_mutation_id=mutation_id,
    )
    item_key = "chunk:0:item:0"
    _item(
        coordinator,
        prepared,
        captured,
        operation_key=operation_key,
        result_id=operation["result_id"],
        item_key=item_key,
    )
    lineage = coordinator.create_lineage_intent(
        run_id=prepared.run_id,
        run_item_key=item_key,
        operation_key=operation_key,
        memory_id=operation["result_id"],
        memory_mutation_id=mutation_id,
        evidence_id=captured["evidence_id"],
        revision_id=captured["revision_id"],
        lineage_kind="created",
    )
    await manager.apply_import_operation(
        operation_key,
        memory_mutation_id=mutation_id,
    )
    assert await manager.delete(operation["result_id"])
    result = coordinator.reconcile_pending_lineage(manager, run_id=prepared.run_id)
    assert result[0]["status"] == "provenance_broken"
    assert coordinator.list_lineage(run_id=prepared.run_id)[0]["lineage_id"] == lineage["lineage_id"]
    assert coordinator.store.get_content(
        captured["revision_id"], allow_restricted_admin=True
    ) == b"User: source\nAI: answer"


@pytest.mark.asyncio
async def test_legacy_memory_and_tombstoned_evidence_do_not_gain_inferred_lineage(tmp_path):
    config, coordinator, prepared, captured = _captured(tmp_path)
    manager = BucketManager(config)
    legacy_id = await manager.create(**_payload("legacy memory"))
    assert coordinator.list_lineage(memory_id=legacy_id) == []

    operation_key = "o5b:" + "6" * 64
    mutation_id = hashlib.sha256(operation_key.encode()).hexdigest()
    operation = manager.plan_import_operation(
        operation_key,
        operation_kind="create",
        payload=_payload("lineage memory"),
        memory_mutation_id=mutation_id,
    )
    item_key = "chunk:0:item:6"
    _item(
        coordinator,
        prepared,
        captured,
        operation_key=operation_key,
        result_id=operation["result_id"],
        item_key=item_key,
    )
    edge = coordinator.create_lineage_intent(
        run_id=prepared.run_id,
        run_item_key=item_key,
        operation_key=operation_key,
        memory_id=operation["result_id"],
        memory_mutation_id=mutation_id,
        evidence_id=captured["evidence_id"],
        revision_id=captured["revision_id"],
        lineage_kind="created",
    )
    tombstoned = coordinator.store.update_state(
        captured["evidence_id"], "tombstoned"
    )
    marked = coordinator.store.update_lineage_status(
        edge["lineage_id"], status="source_expired"
    )
    assert tombstoned["lifecycle_state"] == "tombstoned"
    assert marked["status"] == "source_expired"
    assert marked["evidence_id"] == captured["evidence_id"]
    assert marked["revision_id"] == captured["revision_id"]
    assert "content" not in marked
    assert await manager.get(legacy_id) is not None


@pytest.mark.asyncio
async def test_import_create_and_preserve_raw_lineage_kinds(tmp_path, monkeypatch):
    monkeypatch.setattr(
        import_memory,
        "detect_and_parse",
        lambda _content, _filename: [{"role": "user", "content": "turn"}],
    )
    item = {
        "name": "created",
        "content": "captured memory",
        "domain": ["测试"],
        "tags": [],
        "importance": 5,
        "valence": 0.5,
        "arousal": 0.3,
        "preserve_raw": False,
    }
    for suffix, preserve_raw in (("create", False), ("raw", True)):
        config = {
            "buckets_dir": str(tmp_path / f"buckets-{suffix}"),
            "raw_evidence_root": str(tmp_path / f"raw-evidence-{suffix}"),
            "merge_threshold": 75,
            "wikilink": {"enabled": False},
        }
        manager = BucketManager(config)
        engine = ImportEngine(
            config,
            manager,
            type("D", (), {"api_available": True})(),
        )
        engine._extract_memories = AsyncMock(return_value=[item])
        result = await engine.start_raw_evidence(
            f"source-{suffix}".encode(),
            filename=f"{suffix}.txt",
            preserve_raw=preserve_raw,
            media_type="text/plain",
        )
        assert result["status"] == "completed"
        coordinator = RawEvidenceImportCoordinator(config)
        edges = coordinator.list_lineage()
        assert len(edges) == 1
        assert edges[0]["status"] == "complete"
        assert edges[0]["lineage_kind"] == (
            "preserve_raw_created" if preserve_raw else "created"
        )
        created = await manager.get(edges[0]["memory_id"])
        assert created["metadata"]["provenance_kind"] == "unknown"


@pytest.mark.asyncio
async def test_import_update_preserves_prior_lineage_edge(tmp_path, monkeypatch):
    monkeypatch.setattr(
        import_memory,
        "detect_and_parse",
        lambda _content, _filename: [{"role": "user", "content": "turn"}],
    )
    config = {
        "buckets_dir": str(tmp_path / "buckets"),
        "raw_evidence_root": str(tmp_path / "raw-evidence"),
        "merge_threshold": 75,
        "wikilink": {"enabled": False},
    }
    manager = BucketManager(config)
    dehydrator = type("D", (), {"api_available": True})()
    engine = ImportEngine(config, manager, dehydrator)
    item = {
        "name": "source",
        "content": "first contribution",
        "domain": ["测试"],
        "tags": [],
        "importance": 5,
        "valence": 0.5,
        "arousal": 0.3,
        "preserve_raw": False,
    }
    engine._extract_memories = AsyncMock(return_value=[item])
    manager.search = AsyncMock(return_value=[])
    first = await engine.start_raw_evidence(
        b"first source",
        filename="first.txt",
        media_type="text/plain",
    )
    assert first["status"] == "completed"
    first_edges = RawEvidenceImportCoordinator(config).list_lineage()
    assert len(first_edges) == 1
    memory_id = first_edges[0]["memory_id"]

    item["content"] = "second contribution"
    manager.search = AsyncMock(
        return_value=[
            {
                "id": memory_id,
                "score": 100,
                "content": "first contribution",
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
    dehydrator.merge = AsyncMock(return_value="merged contribution")
    second = await engine.start_raw_evidence(
        b"second source",
        filename="second.txt",
        media_type="text/plain",
    )
    assert second["status"] == "completed"
    edges = RawEvidenceImportCoordinator(config).list_lineage()
    assert len(edges) == 2
    assert {edge["lineage_kind"] for edge in edges} == {
        "created",
        "contributed_update",
    }
    assert {edge["memory_id"] for edge in edges} == {memory_id}
    assert len({edge["memory_mutation_id"] for edge in edges}) == 2
