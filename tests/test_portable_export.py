import asyncio
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import frontmatter
import pytest

import portable_export
from maintenance_write_gate import MaintenanceWriteCoordinator
from portable_export import PortableExportError, export_ordinary_portable


def _run(**kwargs):
    return asyncio.run(export_ordinary_portable(confirm_export=True, **kwargs))


def _bucket(root: Path, category: str, filename: str, metadata: dict, body: str) -> Path:
    path = root / category / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(frontmatter.Post(body, **metadata)), encoding="utf-8")
    return path


def _history_db(root: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(root / "bucket_history.sqlite3")
    connection.executescript(
        """
        CREATE TABLE bucket_history (id INTEGER PRIMARY KEY, bucket_id TEXT, old_content TEXT, changed_at TEXT, change_type TEXT);
        CREATE TABLE letters (id INTEGER PRIMARY KEY, content TEXT, created_at TEXT, session_id TEXT, sealed INTEGER);
        CREATE TABLE notes (note_id INTEGER PRIMARY KEY, created_at TEXT, author TEXT, via TEXT, text TEXT, sealed INTEGER, open_at TEXT, boot_delivered_at TEXT, read_at TEXT, skipped_at TEXT, skipped_reason TEXT, dismissed_at TEXT);
        """
    )
    return connection


def _asset_db(root: Path, asset_id: str, payload: bytes) -> None:
    assets = root / "assets"
    assets.mkdir(parents=True)
    digest = hashlib.sha256(payload).hexdigest()
    (assets / digest).write_bytes(payload)
    with sqlite3.connect(root / "assets.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TABLE assets (asset_id TEXT PRIMARY KEY, source_sha256 TEXT, stored_sha256 TEXT, stored_relpath TEXT, original_filename TEXT, mime_type TEXT, kind TEXT, decoded_bytes INTEGER, stored_bytes INTEGER, width INTEGER, height INTEGER, created_at TEXT, title TEXT, description TEXT, updated_at TEXT);
            CREATE TABLE asset_tags (asset_id TEXT, tag_normalized TEXT, tag_display TEXT, created_at TEXT);
            """
        )
        connection.execute(
            "INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (asset_id, digest, digest, digest, "photo.jpg", "image/jpeg", "image", len(payload), len(payload), 1, 1, "2026-01-01T00:00:00+00:00", "title", "description", "2026-01-01T00:00:00+00:00"),
        )
        connection.execute("INSERT INTO asset_tags VALUES (?, ?, ?, ?)", (asset_id, "tag", "Tag", "2026-01-01T00:00:00+00:00"))


def _records(destination: Path, name: str) -> list[dict]:
    return [json.loads(line) for line in (destination / "records" / name).read_text(encoding="utf-8").splitlines()]


def test_export_preserves_visible_ordinary_data_and_hides_sealed(tmp_path):
    root = tmp_path / "buckets"
    visible = "visible-1"
    hidden = "sealed-1"
    _bucket(root, "dynamic", "visible.md", {
        "id": visible, "sealed": 0, "todos": ["keep"],
        "todo_provenance": [{"text": "keep", "said_by": "ting", "source_bucket": hidden}],
        "provenance_kind": "summary", "related_buckets": f"{visible},{hidden},missing",
        "source_bucket": hidden, "superseded_by": hidden, "supersedes": [visible, hidden],
        "extension": {"future": ["value"]},
    }, "visible body")
    _bucket(root, "archive", "sealed.md", {"id": hidden, "sealed": 1}, "sealed body")
    with _history_db(root) as connection:
        connection.executemany("INSERT INTO bucket_history VALUES (?, ?, ?, ?, ?)", [
            (2, visible, "newer", "2026-01-02", "update"),
            (1, visible, "older", "2026-01-01", "update"),
            (3, hidden, "sealed history", "2026-01-03", "update"),
        ])
        connection.executemany("INSERT INTO letters VALUES (?, ?, ?, ?, ?)", [
            (1, "visible letter", "2026-01-01", "s", 0), (2, "sealed letter", "2026-01-01", "s", 1),
        ])
        connection.executemany("INSERT INTO notes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            (1, "2026-01-01", "ting", "mcp", "visible note", 0, None, None, None, None, None, None),
            (2, "2026-01-01", "ting", "mcp", "sealed note", 1, None, None, None, None, None, None),
        ])
    (root / ".emotion_timeline.json").write_text('[{"valence": 0.5}]', encoding="utf-8")
    _asset_db(root, "a" * 32, b"clean asset")

    destination = tmp_path / "ombre-export"
    result = _run(buckets_dir=root, destination=destination, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert result["record_counts"] == {"assets": 1, "bucket_history": 2, "buckets": 1, "letters": 1, "notes": 1}
    bucket = _records(destination, "buckets.jsonl")[0]
    assert bucket["body"] == "visible body"
    assert bucket["frontmatter"]["extension"] == {"future": ["value"]}
    assert bucket["frontmatter"]["todos"] == ["keep"]
    assert bucket["frontmatter"]["provenance_kind"] == "summary"
    assert bucket["frontmatter"]["related_buckets"] == visible
    assert bucket["frontmatter"]["source_bucket"] == ""
    assert bucket["frontmatter"]["superseded_by"] == ""
    assert bucket["frontmatter"]["supersedes"] == [visible]
    assert bucket["frontmatter"]["todo_provenance"][0]["source_bucket"] is None
    assert [row["old_content"] for row in _records(destination, "bucket_history.jsonl")] == ["older", "newer"]
    assert _records(destination, "letters.jsonl")[0]["content"] == "visible letter"
    assert _records(destination, "notes.jsonl")[0]["text"] == "visible note"
    asset = _records(destination, "assets.jsonl")[0]
    assert (destination / asset["blob_path"]).read_bytes() == b"clean asset"
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert "historical frontmatter" in " ".join(manifest["omissions"])
    exported_bytes = b"".join(path.read_bytes() for path in destination.rglob("*") if path.is_file())
    assert hidden.encode() not in exported_bytes
    assert not any(path.name.endswith(".snapshot.sqlite3") for path in destination.rglob("*"))


def test_empty_store_is_deterministic_and_manifest_is_last(tmp_path):
    root = tmp_path / "buckets"
    root.mkdir()
    now = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = tmp_path / "first"
    second = tmp_path / "second"
    _run(buckets_dir=root, destination=first, clock=now)
    _run(buckets_dir=root, destination=second, clock=now)
    first_files = {path.relative_to(first): path.read_bytes() for path in first.rglob("*") if path.is_file()}
    second_files = {path.relative_to(second): path.read_bytes() for path in second.rglob("*") if path.is_file()}
    assert first_files == second_files
    assert set(path.name for path in (first / "records").iterdir()) == {
        "assets.jsonl", "bucket_history.jsonl", "buckets.jsonl", "emotion_timeline.json", "letters.jsonl", "notes.jsonl"
    }
    manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    for relative, metadata in manifest["files"].items():
        content = (first / relative).read_bytes()
        assert hashlib.sha256(content).hexdigest() == metadata["sha256"]
        assert len(content) == metadata["bytes"]


def test_asset_corruption_never_publishes_manifest(tmp_path):
    root = tmp_path / "buckets"
    root.mkdir()
    _asset_db(root, "b" * 32, b"clean")
    (root / "assets" / hashlib.sha256(b"clean").hexdigest()).write_bytes(b"corrupt")
    destination = tmp_path / "out"
    with pytest.raises(PortableExportError, match="asset_corrupt"):
        _run(buckets_dir=root, destination=destination)
    assert not destination.exists()


def test_destination_and_external_authority_fail_closed(tmp_path):
    root = tmp_path / "buckets"
    root.mkdir()
    with pytest.raises(PortableExportError, match="destination_inside_source"):
        _run(buckets_dir=root, destination=root / "out")
    with pytest.raises(PortableExportError, match="asset_authority_unsupported"):
        _run(buckets_dir=root, destination=tmp_path / "out", environ={"OMBRE_ASSET_AUTHORITY": "rm"})
    with pytest.raises(PortableExportError, match="destination_invalid"):
        _run(buckets_dir=root, destination=tmp_path / "nested" / ".." / "traversal")


def test_destination_symlink_is_rejected_and_source_is_unchanged(tmp_path):
    root = tmp_path / "buckets"
    source = _bucket(root, "permanent", "item.md", {"id": "one", "sealed": 0}, "body")
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    _run(buckets_dir=root, destination=tmp_path / "ordinary-export")
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before

    linked_parent = tmp_path / "linked-parent"
    os.symlink(tmp_path, linked_parent)
    with pytest.raises(PortableExportError, match="path_symlink_unsafe"):
        _run(buckets_dir=root, destination=linked_parent / "out")


def test_source_change_and_expired_lease_do_not_publish(tmp_path, monkeypatch):
    root = tmp_path / "buckets"
    path = _bucket(root, "dynamic", "item.md", {"id": "one", "sealed": 0}, "body")
    destination = tmp_path / "changed"
    original_inventory = portable_export._source_inventory
    calls = 0

    def change_on_final(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text(frontmatter.dumps(frontmatter.Post("changed", id="one", sealed=0)), encoding="utf-8")
        return original_inventory(*args, **kwargs)

    monkeypatch.setattr(portable_export, "_source_inventory", change_on_final)
    with pytest.raises(PortableExportError, match="source_changed"):
        _run(buckets_dir=root, destination=destination)
    assert not destination.exists()

    value = [0.0]
    coordinator = MaintenanceWriteCoordinator(monotonic=lambda: value[0])
    monkeypatch.setattr(portable_export, "_source_inventory", original_inventory)
    original_write = portable_export._write_payload

    def expire(*args, **kwargs):
        value[0] = 2.0
        return original_write(*args, **kwargs)

    monkeypatch.setattr(portable_export, "_write_payload", expire)
    with pytest.raises(PortableExportError, match="freeze_lease_expired"):
        _run(buckets_dir=root, destination=tmp_path / "expired", coordinator=coordinator, max_freeze_seconds=1)
    assert not (tmp_path / "expired").exists()


def test_freeze_contention_and_cli_confirmation(tmp_path):
    root = tmp_path / "buckets"
    root.mkdir()
    coordinator = MaintenanceWriteCoordinator()

    async def exercise():
        async with coordinator.freeze(reason="test", drain_timeout_seconds=1, max_freeze_seconds=1):
            with pytest.raises(PortableExportError, match="freeze_unavailable"):
                await export_ordinary_portable(
                    buckets_dir=root, destination=tmp_path / "blocked", confirm_export=True, coordinator=coordinator
                )

    asyncio.run(exercise())
    assert portable_export.main(["--buckets-dir", str(root), "--destination", str(tmp_path / "cli")]) == 2
    assert not (tmp_path / "cli").exists()
