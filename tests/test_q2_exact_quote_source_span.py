"""Permanent Q2 exact quote/source-span regression tests."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3

import pytest

from raw_evidence_backup import (
    RawEvidenceBackupError,
    _build_manifest,
    _canonical_json,
    _validate_manifest,
    _verify_registry_snapshot,
)
from raw_evidence_restore import _verify_staged_root
from raw_evidence_import import RawEvidenceImportCoordinator
from raw_evidence_lifecycle import RawEvidenceLifecycle
from raw_evidence_store import RawEvidenceError, RawEvidenceStore
from source_span_producer import (
    MODEL_CANDIDATE_CONTEXT_MAX_BYTES,
    MODEL_CANDIDATE_CONTEXT_MAX_CHARS,
    MODEL_CANDIDATE_SEGMENT_MAX_BYTES,
    MODEL_CANDIDATE_SEGMENT_MAX_CHARS,
    SourceAwareSpanProducer,
    raw_byte_slice,
)


def _captured(tmp_path, raw: bytes = b"alpha\nbeta\n"):
    coordinator = RawEvidenceImportCoordinator(
        {"buckets_dir": str(tmp_path / "buckets"), "raw_evidence_root": str(tmp_path / "raw")}
    )
    prepared = coordinator.prepare_run(
        raw, filename="source.txt", media_type="text/plain", preserve_raw=False, resume=False
    )
    captured = coordinator.capture(prepared, raw, filename="source.txt", media_type="text/plain")
    coordinator.upsert_item(
        prepared.run_id,
        "memory-item",
        item_kind="memory",
        input_digest=hashlib.sha256(b"memory").hexdigest(),
        status="memory_planned",
        evidence_id=captured["evidence_id"],
        revision_id=captured["revision_id"],
        operation_key="operation:0",
        operation_kind="create",
        result_id="a" * 12,
    )
    lineage = coordinator.create_lineage_intent(
        run_id=prepared.run_id,
        run_item_key="memory-item",
        operation_key="operation:0",
        memory_id="a" * 12,
        memory_mutation_id=hashlib.sha256(b"mutation").hexdigest(),
        evidence_id=captured["evidence_id"],
        revision_id=captured["revision_id"],
        lineage_kind="created",
    )
    return coordinator.store, coordinator, prepared, captured, lineage


def _set_privacy(store, evidence_id: str, privacy_class: str) -> None:
    with sqlite3.connect(store.registry_path) as conn:
        conn.execute(
            "UPDATE evidence_objects SET privacy_class = ? WHERE evidence_id = ?",
            (privacy_class, evidence_id),
        )


def _candidate_span_id(store, candidate_token: str) -> str:
    with sqlite3.connect(store.registry_path) as conn:
        row = conn.execute(
            "SELECT span_id FROM source_span_candidate_tokens WHERE candidate_token = ?",
            (candidate_token,),
        ).fetchone()
    assert row is not None
    return row[0]


def _stage_store(store, root, repository_id: str = "9" * 32):
    store.bind_backup_repository(repository_id)
    root.mkdir(parents=True, exist_ok=True)
    staged = root / "staged"
    staged.mkdir()
    shutil.copy2(store.registry_path, staged / "registry.sqlite3")
    shutil.copytree(store.blobs_root, staged / "blobs" / "sha256")
    return staged, repository_id


def test_schema_v5_to_v6_has_structure_only_and_no_backfill(tmp_path):
    store = RawEvidenceStore(tmp_path / "raw")
    evidence = store.create(b"legacy", source_system="test", source_kind="item")
    with sqlite3.connect(store.registry_path) as conn:
        conn.execute("UPDATE store_schema SET schema_version = 5 WHERE singleton = 1")
    migrated = RawEvidenceStore(store.root)
    with sqlite3.connect(migrated.registry_path) as conn:
        assert conn.execute("SELECT schema_version FROM store_schema WHERE singleton = 1").fetchone()[0] == 6
        assert conn.execute("SELECT COUNT(*) FROM source_span_descriptors").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_lineage_citations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM source_span_candidate_tokens").fetchone()[0] == 0
    assert migrated.get_content(evidence["revision_id"]) == b"legacy"


def test_descriptor_is_immutable_explicit_revision_hash_and_range_bound(tmp_path):
    store = RawEvidenceStore(tmp_path / "raw")
    evidence = store.create(b"alpha\nbeta\n", source_system="test", source_kind="item")
    descriptor = store.create_span_descriptor(
        evidence["revision_id"], 6, 10, span_id="1" * 32, span_hash=hashlib.sha256(b"beta").hexdigest()
    )
    assert descriptor["revision_id"] == evidence["revision_id"]
    assert store.get_span_descriptor("1" * 32, "2" * 32) is None
    with pytest.raises(RawEvidenceError, match="raw_byte_range_invalid"):
        store.create_span_descriptor(evidence["revision_id"], True, 2, span_hash="0" * 64)
    with pytest.raises(RawEvidenceError, match="raw_byte_range_invalid"):
        store.create_span_descriptor(evidence["revision_id"], 0, 100, span_hash="0" * 64)
    with pytest.raises(RawEvidenceError, match="span_hash_invalid"):
        store.create_span_descriptor(evidence["revision_id"], 0, 5, span_id="2" * 32, span_hash="0" * 64)
    with pytest.raises(sqlite3.IntegrityError, match="immutable_source_span_descriptor"):
        with sqlite3.connect(store.registry_path) as conn:
            conn.execute("UPDATE source_span_descriptors SET span_hash = ? WHERE span_id = ?", ("0" * 64, descriptor["span_id"]))
    with pytest.raises(sqlite3.IntegrityError, match="immutable_source_span_descriptor"):
        with sqlite3.connect(store.registry_path) as conn:
            conn.execute("DELETE FROM source_span_descriptors WHERE span_id = ?", (descriptor["span_id"],))


def test_privacy_is_orthogonal_and_lifecycle_keeps_identity(tmp_path):
    store = RawEvidenceStore(tmp_path / "raw")
    evidence = store.create(b"secret", source_system="test", source_kind="item", privacy_class="restricted_admin")
    with pytest.raises(RawEvidenceError, match="restricted_admin_access_denied"):
        store.create_span_descriptor(evidence["revision_id"], 0, 6, span_hash=hashlib.sha256(b"secret").hexdigest(), allow_sealed=True)
    descriptor = store.create_span_descriptor(evidence["revision_id"], 0, 6, span_id="3" * 32, span_hash=hashlib.sha256(b"secret").hexdigest(), allow_restricted_admin=True)
    assert store.verify_span(descriptor["span_id"], evidence["revision_id"])["state"] == "ACCESS_DENIED"
    assert store.verify_span(descriptor["span_id"], evidence["revision_id"], allow_sealed=True)["state"] == "ACCESS_DENIED"
    assert store.verify_span(descriptor["span_id"], evidence["revision_id"], allow_restricted_admin=True)["state"] == "VERIFIED"
    RawEvidenceLifecycle(store).redact(evidence["evidence_id"], reason="q2-test")
    assert store.verify_span(descriptor["span_id"], evidence["revision_id"], allow_restricted_admin=True)["state"] == "UNAVAILABLE"
    assert store.get_span_descriptor(descriptor["span_id"], evidence["revision_id"], allow_restricted_admin=True)["span_id"] == descriptor["span_id"]


def test_citation_is_many_to_many_and_revision_bound(tmp_path):
    store, coordinator, prepared, captured, lineage = _captured(tmp_path)
    first = store.create_span_descriptor(captured["revision_id"], 0, 5, span_id="4" * 32, span_hash=hashlib.sha256(b"alpha").hexdigest(), allow_restricted_admin=True)
    second = store.create_span_descriptor(captured["revision_id"], 6, 10, span_id="5" * 32, span_hash=hashlib.sha256(b"beta").hexdigest(), allow_restricted_admin=True)
    store.create_lineage_citation(lineage["lineage_id"], first["span_id"], revision_id=captured["revision_id"], allow_restricted_admin=True)
    store.create_lineage_citation(lineage["lineage_id"], second["span_id"], revision_id=captured["revision_id"], allow_restricted_admin=True)
    assert len(store.list_lineage_citations(lineage["lineage_id"], captured["revision_id"], allow_restricted_admin=True)) == 2
    other = store.create(b"other", source_system="test", source_kind="item")
    other_span = store.create_span_descriptor(other["revision_id"], 0, 5, span_id="6" * 32, span_hash=hashlib.sha256(b"other").hexdigest())
    with pytest.raises(RawEvidenceError, match="lineage_span_revision_mismatch"):
        store.create_lineage_citation(lineage["lineage_id"], other_span["span_id"], allow_restricted_admin=True)


def test_strict_utf8_raw_byte_segments_retry_tokens_and_spoof_fail_closed(tmp_path):
    raw = b"\xce\xb1\r\nbeta\n"
    store, coordinator, prepared, captured, lineage = _captured(tmp_path, raw)
    producer = SourceAwareSpanProducer(store)
    digest = hashlib.sha256(raw).hexdigest()
    first = producer.produce_candidates(raw, source_format="utf8_plain_text", revision_id=captured["revision_id"], run_id=prepared.run_id, run_item_key="source_snapshot", input_digest=digest, allow_restricted_admin=True)
    retry = producer.produce_candidates(raw, source_format="utf8_plain_text", revision_id=captured["revision_id"], run_id=prepared.run_id, run_item_key="source_snapshot", input_digest=digest, allow_restricted_admin=True)
    assert first.status == retry.status == "READY"
    assert [item.opaque_candidate_token for item in first.candidates] == [item.opaque_candidate_token for item in retry.candidates]
    assert raw_byte_slice(raw, 0, 4) == b"\xce\xb1\r\n"
    for item in first.model_candidates():
        assert set(item) == {"opaque_candidate_token", "source_segment", "source_context"}
        assert "revision_id" not in item and "span_id" not in item and "raw_byte_start" not in item
    selected = producer.create_selected_citations([first.candidates[1].opaque_candidate_token], lineage_id=lineage["lineage_id"], revision_id=captured["revision_id"], run_id=prepared.run_id, run_item_key="source_snapshot", input_digest=digest, allow_restricted_admin=True)
    assert selected == {"status": "valid", "citation_count": 1}
    assert producer.resolve_model_attribution(["0" * 32], revision_id=captured["revision_id"], run_id=prepared.run_id, run_item_key="source_snapshot", input_digest=digest, allow_restricted_admin=True)["status"] == "invalid"
    assert producer.produce_candidates(raw, source_format="claude_json", revision_id=captured["revision_id"], run_id=prepared.run_id, run_item_key="source_snapshot", input_digest=digest, allow_restricted_admin=True).status == "UNSUPPORTED"

    bad = b"valid\xff"
    bad_store, bad_coordinator, bad_prepared, bad_capture, _ = _captured(tmp_path / "bad", bad)
    bad_result = SourceAwareSpanProducer(bad_store).produce_candidates(bad, source_format="utf8_plain_text", revision_id=bad_capture["revision_id"], run_id=bad_prepared.run_id, run_item_key="source_snapshot", input_digest=hashlib.sha256(bad).hexdigest(), allow_restricted_admin=True)
    assert bad_result.status == "UNSUPPORTED" and bad_result.candidates == ()
    with sqlite3.connect(bad_store.registry_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_span_descriptors").fetchone()[0] == 0


def test_backup_manifest_v2_and_registry_span_semantics(tmp_path):
    store, coordinator, prepared, captured, lineage = _captured(tmp_path)
    span = store.create_span_descriptor(captured["revision_id"], 0, 5, span_id="7" * 32, span_hash=hashlib.sha256(b"alpha").hexdigest(), allow_restricted_admin=True)
    store.create_lineage_citation(lineage["lineage_id"], span["span_id"], revision_id=captured["revision_id"], allow_restricted_admin=True)
    registry = _verify_registry_snapshot(store.registry_path, cas_root=store.blobs_root)
    manifest = _build_manifest(backup_id="a" * 32, operation_id="b" * 32, repository_id="c" * 32, restore_epoch=0, created_at="2026-08-21T00:00:00+00:00", expires_at="2026-08-28T00:00:00+00:00", registry_info=registry, cas_entries=[], recipient_fingerprint="f" * 64)
    assert manifest["backup_format_version"] == 2
    assert manifest["span_descriptors"] == {"count": 1, "schema_version": 1}
    assert manifest["citations"] == {"count": 1, "schema_version": 1}
    _validate_manifest(manifest, _canonical_json(manifest))
    tampered = dict(manifest)
    tampered["span_descriptors"] = {"count": 0, "schema_version": 1}
    digest_input = dict(tampered)
    digest_input.pop("manifest_sha256")
    tampered["manifest_sha256"] = hashlib.sha256(_canonical_json(digest_input)).hexdigest()
    with pytest.raises(Exception, match="backup_manifest_invalid"):
        _validate_manifest(tampered, _canonical_json(tampered))


def test_restore_staged_root_rechecks_span_payload_semantics(tmp_path):
    store = RawEvidenceStore(tmp_path / "live")
    raw = b"alpha\n"
    evidence = store.create(raw, source_system="test", source_kind="item")
    store.create_span_descriptor(
        evidence["revision_id"],
        0,
        len(raw),
        span_id="8" * 32,
        span_hash=hashlib.sha256(raw).hexdigest(),
    )
    repository_id = "9" * 32
    store.bind_backup_repository(repository_id)
    staged = tmp_path / "staged"
    staged.mkdir()
    shutil.copy2(store.registry_path, staged / "registry.sqlite3")
    shutil.copytree(store.blobs_root, staged / "blobs" / "sha256")
    _verify_staged_root(staged, repository_id)

    payload = staged / "blobs" / "sha256" / evidence["content_hash"][:2] / evidence["content_hash"]
    payload.write_bytes(b"tamper")
    with pytest.raises(RawEvidenceBackupError, match="^span_semantic_invalid$"):
        _verify_staged_root(staged, repository_id)



def test_long_single_line_model_views_are_bounded(tmp_path):
    raw = ("?" * 3000 + "\n").encode("utf-8")
    store, coordinator, prepared, captured, _ = _captured(tmp_path, raw)
    result = SourceAwareSpanProducer(store).produce_candidates(
        raw,
        source_format="utf8_plain_text",
        revision_id=captured["revision_id"],
        run_id=prepared.run_id,
        run_item_key="source_snapshot",
        input_digest=hashlib.sha256(raw).hexdigest(),
        allow_restricted_admin=True,
    )
    assert result.status == "READY" and len(result.candidates) == 1
    candidate = result.candidates[0]
    assert len(candidate.source_segment) <= MODEL_CANDIDATE_SEGMENT_MAX_CHARS
    assert len(candidate.source_segment.encode("utf-8")) <= MODEL_CANDIDATE_SEGMENT_MAX_BYTES
    assert len(candidate.source_context) <= MODEL_CANDIDATE_CONTEXT_MAX_CHARS
    assert len(candidate.source_context.encode("utf-8")) <= MODEL_CANDIDATE_CONTEXT_MAX_BYTES
    with sqlite3.connect(store.registry_path) as conn:
        row = conn.execute(
            "SELECT raw_byte_start, raw_byte_end FROM source_span_descriptors"
        ).fetchone()
    assert row == (0, len(raw))


def test_candidate_token_replay_is_bound_to_every_explicit_scope(tmp_path):
    raw = b"alpha\n"
    store, coordinator, prepared, captured, _ = _captured(tmp_path, raw)
    producer = SourceAwareSpanProducer(store)
    digest = hashlib.sha256(raw).hexdigest()
    result = producer.produce_candidates(
        raw,
        source_format="utf8_plain_text",
        revision_id=captured["revision_id"],
        run_id=prepared.run_id,
        run_item_key="source_snapshot",
        input_digest=digest,
        allow_restricted_admin=True,
    )
    token = result.candidates[0].opaque_candidate_token
    assert producer.resolve_model_attribution(
        [token], revision_id=captured["revision_id"], run_id="e" * 32,
        run_item_key="source_snapshot", input_digest=digest,
        allow_restricted_admin=True,
    )["status"] == "invalid"
    assert producer.resolve_model_attribution(
        [token], revision_id=captured["revision_id"], run_id=prepared.run_id,
        run_item_key="wrong-item", input_digest=digest,
        allow_restricted_admin=True,
    )["status"] == "invalid"
    assert producer.resolve_model_attribution(
        [token], revision_id=captured["revision_id"], run_id=prepared.run_id,
        run_item_key="source_snapshot",
        input_digest=hashlib.sha256(b"other").hexdigest(),
        allow_restricted_admin=True,
    )["status"] == "invalid"
    assert SourceAwareSpanProducer(store, producer_version="other-v1").resolve_model_attribution(
        [token], revision_id=captured["revision_id"], run_id=prepared.run_id,
        run_item_key="source_snapshot", input_digest=digest,
        allow_restricted_admin=True,
    )["status"] == "invalid"


def test_sealed_and_restricted_admin_gates_are_orthogonal_for_tokens(tmp_path):
    restricted_store, _, restricted_run, restricted_capture, _ = _captured(tmp_path / "restricted", b"restricted\n")
    restricted_producer = SourceAwareSpanProducer(restricted_store)
    restricted_digest = hashlib.sha256(b"restricted\n").hexdigest()
    restricted_result = restricted_producer.produce_candidates(
        b"restricted\n", source_format="utf8_plain_text",
        revision_id=restricted_capture["revision_id"], run_id=restricted_run.run_id,
        run_item_key="source_snapshot", input_digest=restricted_digest,
        allow_restricted_admin=True,
    )
    restricted_token = restricted_result.candidates[0].opaque_candidate_token
    restricted_span = _candidate_span_id(restricted_store, restricted_token)
    assert restricted_producer.resolve_model_attribution(
        [restricted_token], revision_id=restricted_capture["revision_id"],
        run_id=restricted_run.run_id, run_item_key="source_snapshot",
        input_digest=restricted_digest, allow_sealed=True,
    )["status"] == "access_denied"
    assert restricted_producer.resolve_model_attribution(
        [restricted_token], revision_id=restricted_capture["revision_id"],
        run_id=restricted_run.run_id, run_item_key="source_snapshot",
        input_digest=restricted_digest, allow_restricted_admin=True,
    )["status"] == "valid"
    with pytest.raises(RawEvidenceError, match="restricted_admin_access_denied"):
        restricted_store.get_span_descriptor(
            restricted_span, restricted_capture["revision_id"], allow_sealed=True
        )

    sealed_store, _, sealed_run, sealed_capture, _ = _captured(tmp_path / "sealed", b"sealed\n")
    _set_privacy(sealed_store, sealed_capture["evidence_id"], "sealed")
    sealed_producer = SourceAwareSpanProducer(sealed_store)
    sealed_digest = hashlib.sha256(b"sealed\n").hexdigest()
    sealed_result = sealed_producer.produce_candidates(
        b"sealed\n", source_format="utf8_plain_text",
        revision_id=sealed_capture["revision_id"], run_id=sealed_run.run_id,
        run_item_key="source_snapshot", input_digest=sealed_digest,
        allow_sealed=True,
    )
    sealed_token = sealed_result.candidates[0].opaque_candidate_token
    sealed_span = _candidate_span_id(sealed_store, sealed_token)
    assert sealed_producer.resolve_model_attribution(
        [sealed_token], revision_id=sealed_capture["revision_id"],
        run_id=sealed_run.run_id, run_item_key="source_snapshot",
        input_digest=sealed_digest, allow_restricted_admin=True,
    )["status"] == "access_denied"
    assert sealed_producer.resolve_model_attribution(
        [sealed_token], revision_id=sealed_capture["revision_id"],
        run_id=sealed_run.run_id, run_item_key="source_snapshot",
        input_digest=sealed_digest, allow_sealed=True,
    )["status"] == "valid"
    with pytest.raises(RawEvidenceError, match="sealed_access_denied"):
        sealed_store.get_span_descriptor(
            sealed_span, sealed_capture["revision_id"], allow_restricted_admin=True
        )


def test_cross_evidence_lineage_spoof_fails_db_and_restore_semantics(tmp_path):
    store, coordinator, prepared, captured, lineage = _captured(tmp_path)
    other = store.create(b"other", source_system="test", source_kind="item")
    span = store.create_span_descriptor(
        captured["revision_id"], 0, 5, span_id="a" * 32,
        span_hash=hashlib.sha256(b"alpha").hexdigest(),
        allow_restricted_admin=True,
    )
    with sqlite3.connect(store.registry_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="lineage_revision_evidence_mismatch"):
            conn.execute(
                "UPDATE memory_lineage SET evidence_id = ? WHERE lineage_id = ?",
                (other["evidence_id"], lineage["lineage_id"]),
            )
    with sqlite3.connect(store.registry_path) as conn:
        conn.execute("DROP TRIGGER memory_lineage_validate_revision_evidence_update")
        conn.execute("DROP TRIGGER memory_lineage_citations_validate_insert")
        conn.execute(
            "UPDATE memory_lineage SET evidence_id = ? WHERE lineage_id = ?",
            (other["evidence_id"], lineage["lineage_id"]),
        )
        conn.execute(
            "INSERT INTO memory_lineage_citations (lineage_id, span_id, created_at, citation_schema_version) VALUES (?, ?, ?, 1)",
            (lineage["lineage_id"], span["span_id"], "2026-08-21T00:00:00+00:00"),
        )
    with pytest.raises(RawEvidenceBackupError, match="lineage_semantic_invalid"):
        _verify_registry_snapshot(store.registry_path, cas_root=store.blobs_root)


def test_span_backup_restore_rejects_unsupported_revision_metadata(tmp_path):
    store, coordinator, prepared, captured, lineage = _captured(tmp_path)
    span = store.create_span_descriptor(
        captured["revision_id"], 0, 5, span_id="b" * 32,
        span_hash=hashlib.sha256(b"alpha").hexdigest(),
        allow_restricted_admin=True,
    )
    store.create_lineage_citation(
        lineage["lineage_id"], span["span_id"],
        revision_id=captured["revision_id"], allow_restricted_admin=True,
    )
    for field, value in (("revision_schema_version", 99), ("hash_algorithm", "sha1")):
        staged, repository_id = _stage_store(store, tmp_path / field)
        with sqlite3.connect(staged / "registry.sqlite3") as conn:
            conn.execute("DROP TRIGGER evidence_revisions_immutable_content")
            conn.execute(
                f"UPDATE evidence_revisions SET {field} = ? WHERE revision_id = ?",
                (value, captured["revision_id"]),
            )
        with pytest.raises(RawEvidenceBackupError, match="span_revision_metadata_invalid"):
            _verify_registry_snapshot(
                staged / "registry.sqlite3", cas_root=staged / "blobs" / "sha256"
            )
        with pytest.raises(RawEvidenceBackupError, match="span_revision_metadata_invalid"):
            _verify_staged_root(staged, repository_id)


def test_candidate_production_rolls_back_partial_authority_and_retries(tmp_path):
    raw = b"alpha\nbeta\n"
    store, coordinator, prepared, captured, _ = _captured(tmp_path, raw)
    digest = hashlib.sha256(raw).hexdigest()
    with pytest.raises(RawEvidenceError, match="candidate_scope_conflict"):
        store.create_candidate_set(
            revision_id=captured["revision_id"], run_id=prepared.run_id,
            run_item_key="source_snapshot", producer_version="atomic-test",
            input_digest=digest, allow_restricted_admin=True,
            candidates=[
                {
                    "span_id": "c" * 32, "raw_byte_start": 0,
                    "raw_byte_end": 6,
                    "span_hash": hashlib.sha256(b"alpha\n").hexdigest(),
                    "candidate_token": "d" * 32,
                },
                {
                    "span_id": "e" * 32, "raw_byte_start": 6,
                    "raw_byte_end": 11,
                    "span_hash": hashlib.sha256(b"beta\n").hexdigest(),
                    "candidate_token": "d" * 32,
                },
            ],
        )
    with sqlite3.connect(store.registry_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_span_descriptors").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM source_span_candidate_tokens").fetchone()[0] == 0
    producer = SourceAwareSpanProducer(store)
    first = producer.produce_candidates(
        raw, source_format="utf8_plain_text", revision_id=captured["revision_id"],
        run_id=prepared.run_id, run_item_key="source_snapshot",
        input_digest=digest, allow_restricted_admin=True,
    )
    retry = producer.produce_candidates(
        raw, source_format="utf8_plain_text", revision_id=captured["revision_id"],
        run_id=prepared.run_id, run_item_key="source_snapshot",
        input_digest=digest, allow_restricted_admin=True,
    )
    assert first.status == retry.status == "READY"
    assert [c.opaque_candidate_token for c in first.candidates] == [c.opaque_candidate_token for c in retry.candidates]


def test_explicit_revision_lifecycle_isolation_preserves_old_citation(tmp_path):
    store, coordinator, prepared, captured, lineage = _captured(tmp_path)
    old_span = store.create_span_descriptor(
        captured["revision_id"], 0, 5, span_id="f" * 32,
        span_hash=hashlib.sha256(b"alpha").hexdigest(),
        allow_restricted_admin=True,
    )
    store.create_lineage_citation(
        lineage["lineage_id"], old_span["span_id"],
        revision_id=captured["revision_id"], allow_restricted_admin=True,
    )
    newer = store.create(b"newer", source_system="test", source_kind="item")
    new_span = store.create_span_descriptor(
        newer["revision_id"], 0, 5, span_id="1" * 32,
        span_hash=hashlib.sha256(b"newer").hexdigest(),
    )
    assert store.verify_span(
        old_span["span_id"], captured["revision_id"], allow_restricted_admin=True
    )["state"] == "VERIFIED"
    RawEvidenceLifecycle(store).redact(newer["evidence_id"], reason="newer-only")
    assert store.verify_span(
        old_span["span_id"], captured["revision_id"], allow_restricted_admin=True
    )["state"] == "VERIFIED"
    assert store.verify_span(new_span["span_id"], newer["revision_id"])["state"] == "UNAVAILABLE"
    assert len(
        store.list_lineage_citations(
            lineage["lineage_id"], captured["revision_id"],
            allow_restricted_admin=True,
        )
    ) == 1
