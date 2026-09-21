"""Focused O5A tests for the isolated Raw Evidence store."""

from __future__ import annotations

import ast
import hashlib
import sqlite3
from pathlib import Path

import pytest

import raw_evidence_store as raw_store_module
from raw_evidence_store import (
    HASH_ALGORITHM,
    RawEvidenceError,
    RawEvidenceLimits,
    RawEvidenceStore,
)


def _make_store(tmp_path: Path, *, limits: RawEvidenceLimits | None = None, forbidden_roots=()):
    return RawEvidenceStore(
        tmp_path / "raw-evidence",
        limits=limits,
        forbidden_roots=forbidden_roots,
    )


def _row_count(store: RawEvidenceStore, table: str) -> int:
    with store._connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_disabled_store_is_inert_and_has_no_filesystem_side_effect(tmp_path):
    root = tmp_path / "disabled"
    store = RawEvidenceStore(root, enabled=False)

    assert store.is_disabled
    assert not root.exists()
    with pytest.raises(RawEvidenceError, match="store_disabled"):
        store.create(b"bytes", source_system="test", source_kind="item")


def test_creation_read_and_metadata_round_trip(tmp_path):
    store = _make_store(tmp_path)
    content = b"O5A exact bytes\x00\xff"

    result = store.create(
        content,
        source_system="fixture",
        source_kind="item",
        source_scope="scope-a",
        upstream_source_id="source-1",
        upstream_item_id="item-1",
        identity_origin="upstream",
        fidelity_level="IMPORT_SNAPSHOT",
        media_type="application/octet-stream",
        captured_at="2026-08-19T00:00:00+00:00",
    )

    assert result["evidence_id"]
    assert result["revision_id"]
    assert result["source_scope"] == "scope-a"
    assert result["upstream_item_id"] == "item-1"
    assert result["fidelity_level"] == "IMPORT_SNAPSHOT"
    assert result["hash_algorithm"] == HASH_ALGORITHM
    assert result["content_hash"] == hashlib.sha256(content).hexdigest()
    assert result["content_size_bytes"] == len(content)
    assert result["lifecycle_state"] == "available"
    assert result["verification_state"] == "verified"
    assert store.get_content(result["revision_id"]) == content
    assert store.get_revision(result["revision_id"])["evidence_id"] == result["evidence_id"]


def test_ids_remain_addressable_after_store_reload(tmp_path):
    root = tmp_path / "raw-evidence"
    first = RawEvidenceStore(root)
    result = first.create(b"stable", source_system="fixture", source_kind="item")

    second = RawEvidenceStore(root)
    loaded = second.get_evidence(result["evidence_id"])

    assert loaded["evidence_id"] == result["evidence_id"]
    assert loaded["revision_id"] == result["revision_id"]
    assert second.get_content(result["revision_id"]) == b"stable"


def test_same_bytes_from_different_sources_keep_distinct_logical_identity(tmp_path):
    store = _make_store(tmp_path)
    first = store.create(
        b"same physical bytes",
        source_system="fixture",
        source_kind="item",
        source_scope="scope-a",
        source_occurrence_key="occurrence-a",
        identity_origin="local",
    )
    second = store.create(
        b"same physical bytes",
        source_system="fixture",
        source_kind="item",
        source_scope="scope-b",
        source_occurrence_key="occurrence-b",
        identity_origin="local",
    )

    assert first["evidence_id"] != second["evidence_id"]
    assert first["revision_id"] != second["revision_id"]
    assert first["blob_relpath"] == second["blob_relpath"]
    assert _row_count(store, "evidence_objects") == 2
    assert _row_count(store, "evidence_revisions") == 2


def test_identity_validation_does_not_invent_upstream_ids(tmp_path):
    store = _make_store(tmp_path)

    with pytest.raises(RawEvidenceError, match="identity_invalid"):
        store.create(
            b"bytes",
            source_system="fixture",
            source_kind="item",
            identity_origin="upstream",
        )
    with pytest.raises(RawEvidenceError, match="identity_invalid"):
        store.create(
            b"bytes",
            source_system="fixture",
            source_kind="item",
            identity_origin="local",
        )


def test_content_corruption_fails_closed_and_marks_integrity_state(tmp_path):
    store = _make_store(tmp_path)
    result = store.create(b"original", source_system="fixture", source_kind="item")
    blob = store.root / result["blob_relpath"]
    blob.write_bytes(b"tampered")

    with pytest.raises(RawEvidenceError, match="integrity_failed"):
        store.get_content(result["revision_id"])

    failed = store.get_revision(result["revision_id"])
    assert failed["verification_state"] == "failed"
    assert failed["lifecycle_state"] == "integrity_failed"


def test_verify_content_returns_true_only_for_verified_bytes(tmp_path):
    store = _make_store(tmp_path)
    result = store.create(b"verified", source_system="fixture", source_kind="item")

    assert store.verify_content(result["revision_id"])


def test_append_only_revision_rejects_public_and_direct_content_mutation(tmp_path):
    store = _make_store(tmp_path)
    result = store.create(b"immutable", source_system="fixture", source_kind="item")

    assert not hasattr(store, "update_content")
    with sqlite3.connect(store.registry_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE evidence_revisions SET content_hash = ? WHERE revision_id = ?",
                ("0" * 64, result["revision_id"]),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE evidence_objects SET source_system = ? WHERE evidence_id = ?",
                ("changed", result["evidence_id"]),
            )

    assert store.get_content(result["revision_id"]) == b"immutable"


def test_metadata_update_does_not_change_content_identity(tmp_path):
    store = _make_store(tmp_path)
    result = store.create(b"metadata-only", source_system="fixture", source_kind="item")

    updated = store.update_metadata(
        result["evidence_id"],
        privacy_class="sealed",
        lifecycle_state="available",
    )

    assert updated["content_hash"] == result["content_hash"]
    assert updated["blob_relpath"] == result["blob_relpath"]
    with pytest.raises(RawEvidenceError, match="sealed_access_denied"):
        store.get_evidence(result["evidence_id"])
    assert store.get_content(result["revision_id"], allow_sealed=True) == b"metadata-only"


def test_cas_collision_is_rejected_without_overwrite(tmp_path):
    store = _make_store(tmp_path)
    result = store.create(b"cas-content", source_system="fixture", source_kind="item")
    blob = store.root / result["blob_relpath"]
    blob.write_bytes(b"wrong content")

    with pytest.raises(RawEvidenceError, match="integrity_conflict"):
        store.create(b"cas-content", source_system="fixture", source_kind="other")
    assert blob.read_bytes() == b"wrong content"


def test_path_inputs_are_metadata_only_and_arbitrary_paths_are_not_read(tmp_path):
    store = _make_store(tmp_path)
    result = store.create(
        b"path-safe",
        source_system="fixture",
        source_kind="item",
        upstream_source_id="..\\outside\\source",
    )

    assert result["upstream_source_id"] == "..\\outside\\source"
    with pytest.raises(RawEvidenceError, match="revision_id_invalid"):
        store.get_content(str(tmp_path / "outside" / "file"))
    assert store.get_content(result["revision_id"]) == b"path-safe"


def test_root_and_forbidden_root_overlap_are_rejected(tmp_path):
    ordinary_root = tmp_path / "buckets"
    ordinary_root.mkdir()

    with pytest.raises(RawEvidenceError, match="root_overlap"):
        RawEvidenceStore(ordinary_root / "evidence", forbidden_roots=(ordinary_root,))
    with pytest.raises(RawEvidenceError, match="root_overlap"):
        RawEvidenceStore(ordinary_root, forbidden_roots=(ordinary_root / "evidence",))
    with pytest.raises(RawEvidenceError, match="evidence_root_not_absolute"):
        RawEvidenceStore("relative-evidence-root")


def test_source_path_objects_are_not_accepted_as_content(tmp_path):
    store = _make_store(tmp_path)
    source = tmp_path / "source.bin"
    source.write_bytes(b"not read")

    with pytest.raises(RawEvidenceError, match="invalid_input"):
        store.create(source, source_system="fixture", source_kind="item")


def test_oversized_content_leaves_no_formal_record_or_temp_file(tmp_path):
    store = _make_store(
        tmp_path,
        limits=RawEvidenceLimits(
            max_evidence_bytes=3,
            max_temp_bytes=3,
            max_metadata_chars=128,
            max_store_bytes=128,
        ),
    )

    with pytest.raises(RawEvidenceError, match="limit_exceeded"):
        store.create(b"four", source_system="fixture", source_kind="item")

    assert _row_count(store, "evidence_objects") == 0
    assert list(store.temp_root.iterdir()) == []


def test_failed_publish_cleans_staging_and_writes_no_record(tmp_path, monkeypatch):
    store = _make_store(tmp_path)

    def fail_publish(temp_path, content_hash, size):
        raise RawEvidenceError("content_publish_failed")

    monkeypatch.setattr(store, "_publish_blob", fail_publish)
    with pytest.raises(RawEvidenceError, match="content_publish_failed"):
        store.create(b"staged", source_system="fixture", source_kind="item")

    assert _row_count(store, "evidence_objects") == 0
    assert list(store.temp_root.iterdir()) == []


def test_fsync_failure_fails_closed_and_cleans_staging(tmp_path, monkeypatch):
    store = _make_store(tmp_path)

    def fail_fsync(handle):
        raise OSError("synthetic")

    monkeypatch.setattr(raw_store_module.os, "fsync", fail_fsync)
    with pytest.raises(RawEvidenceError, match="content_write_failed"):
        store.create(b"staged", source_system="fixture", source_kind="item")

    assert _row_count(store, "evidence_objects") == 0
    assert list(store.temp_root.iterdir()) == []


def test_sealed_evidence_is_not_visible_without_explicit_internal_access(tmp_path):
    store = _make_store(tmp_path)
    result = store.create(
        b"sealed",
        source_system="fixture",
        source_kind="item",
        privacy_class="sealed",
    )

    with pytest.raises(RawEvidenceError, match="sealed_access_denied"):
        store.get_evidence(result["evidence_id"])
    with pytest.raises(RawEvidenceError, match="sealed_access_denied"):
        store.get_content(result["revision_id"])
    assert store.get_evidence(result["evidence_id"], allow_sealed=True)["privacy_class"] == "sealed"
    assert store.get_content(result["revision_id"], allow_sealed=True) == b"sealed"
    assert not hasattr(store, "list_evidence")
    assert not hasattr(store, "search")


def test_restricted_admin_capability_is_separate_from_sealed(tmp_path):
    store = _make_store(tmp_path)
    result = store.create(
        b"restricted",
        source_system="fixture",
        source_kind="item",
        privacy_class="restricted_admin",
    )

    with pytest.raises(RawEvidenceError, match="restricted_admin_access_denied"):
        store.get_evidence(result["evidence_id"])
    with pytest.raises(RawEvidenceError, match="restricted_admin_access_denied"):
        store.get_content(result["revision_id"])
    with pytest.raises(RawEvidenceError, match="restricted_admin_access_denied"):
        store.get_revision(result["revision_id"], allow_sealed=True)
    with pytest.raises(RawEvidenceError, match="restricted_admin_access_denied"):
        store.verify(result["revision_id"], allow_sealed=True)

    assert store.get_evidence(
        result["evidence_id"],
        allow_restricted_admin=True,
    )["privacy_class"] == "restricted_admin"
    assert store.get_revision(
        result["revision_id"],
        allow_restricted_admin=True,
    )["privacy_class"] == "restricted_admin"
    assert store.get_content(
        result["revision_id"],
        allow_restricted_admin=True,
    ) == b"restricted"
    assert store.verify(
        result["revision_id"],
        allow_restricted_admin=True,
    )


def test_schema_version_is_fail_closed(tmp_path):
    root = tmp_path / "raw-evidence"
    store = RawEvidenceStore(root)
    store.close()
    with sqlite3.connect(store.registry_path) as conn:
        conn.execute("UPDATE store_schema SET schema_version = 999 WHERE singleton = 1")

    with pytest.raises(RawEvidenceError, match="schema_unsupported"):
        RawEvidenceStore(root)


def test_reparse_root_is_rejected_when_platform_allows_symlink_creation(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {type(exc).__name__}")

    with pytest.raises(RawEvidenceError, match="path_reparse_unsupported"):
        RawEvidenceStore(link / "raw-evidence")


def test_module_has_no_application_runtime_imports():
    source = Path(raw_store_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])

    assert not imported_roots.intersection(
        {"server", "import_memory", "bucket_manager", "dehydrator", "remember_me_adapter"}
    )
