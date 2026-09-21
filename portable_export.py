"""CLI-only ordinary portable export for Ombre Brain.

This module intentionally exports only the ordinary, unsealed ``buckets_dir``
authority.  It is not a backup, restore, cloud-sync, or Raw Evidence export.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter

from asset_authority import AssetAuthority, load_asset_authority
from maintenance_write_gate import (
    DEFAULT_WRITE_COORDINATOR,
    MaintenanceWriteCoordinator,
    MaintenanceWriteError,
)


EXPORT_SCHEMA_VERSION = 1
EXPORTER_VERSION = "5.8a"
_BUCKET_DIRS = ("permanent", "dynamic", "archive", "feel")
_RELATION_FIELDS = ("source_bucket", "related_buckets", "superseded_by", "supersedes")
_INTERNAL_FRONTMATTER_FIELDS = {"_ob_import_operations"}


class PortableExportError(RuntimeError):
    """Stable, content-free errors for ordinary portable export."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _json_value(value: Any) -> Any:
    """Convert YAML scalar values without silently discarding extensions."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    raise PortableExportError("frontmatter_unsupported")


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_sealed(metadata: dict[str, Any]) -> bool:
    try:
        return int(metadata.get("sealed", 0) or 0) == 1
    except (TypeError, ValueError):
        return False


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _reject_symlink_components(path: Path) -> None:
    candidate = path.absolute()
    chain = [candidate]
    while candidate.parent != candidate:
        candidate = candidate.parent
        chain.append(candidate)
    for item in reversed(chain):
        if item.exists() and item.is_symlink():
            raise PortableExportError("path_symlink_unsafe")


def _validate_destination(source_root: Path, destination: Path) -> tuple[Path, Path]:
    try:
        source = source_root.resolve(strict=True)
        parent = destination.parent.resolve(strict=True)
    except OSError as exc:
        raise PortableExportError("destination_invalid") from exc
    if not source.is_dir() or not parent.is_dir():
        raise PortableExportError("source_invalid")
    if destination.exists():
        raise PortableExportError("destination_not_new")
    if destination.name in {"", ".", ".."}:
        raise PortableExportError("destination_invalid")
    if ".." in destination.parts:
        raise PortableExportError("destination_invalid")
    _reject_symlink_components(destination.parent)
    final = parent / destination.name
    if _is_relative_to(final.resolve(strict=False), source):
        raise PortableExportError("destination_inside_source")
    return source, final


def _discover_bucket_paths(source_root: Path) -> list[Path]:
    paths: list[Path] = []
    for name in _BUCKET_DIRS:
        root = source_root / name
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            raise PortableExportError("bucket_tree_unsafe")
        for current, directories, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            if current_path.is_symlink() or any(
                (current_path / child).is_symlink() for child in directories
            ):
                raise PortableExportError("bucket_tree_unsafe")
            for filename in filenames:
                path = current_path / filename
                if path.suffix == ".md":
                    if path.is_symlink():
                        raise PortableExportError("bucket_tree_unsafe")
                    paths.append(path)
    return sorted(paths, key=lambda item: item.relative_to(source_root).as_posix())


def _file_evidence(path: Path) -> tuple[int, int, str]:
    try:
        before = path.stat()
        payload = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise PortableExportError("source_changed") from exc
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise PortableExportError("source_changed")
    return before.st_size, before.st_mtime_ns, _sha256_bytes(payload)


def _safe_asset_path(asset_root: Path, stored_relpath: Any) -> Path:
    if not isinstance(stored_relpath, str) or not stored_relpath:
        raise PortableExportError("asset_invalid")
    relative = Path(stored_relpath)
    if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
        raise PortableExportError("asset_path_unsafe")
    candidate = asset_root / relative
    try:
        _reject_symlink_components(candidate.parent)
        resolved = candidate.resolve(strict=True)
        root = asset_root.resolve(strict=True)
    except OSError as exc:
        raise PortableExportError("asset_path_unsafe") from exc
    if not _is_relative_to(resolved, root) or resolved.is_symlink():
        raise PortableExportError("asset_path_unsafe")
    return resolved


def _snapshot_sqlite(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    try:
        source_connection = sqlite3.connect(
            f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=5
        )
        try:
            source_connection.execute("PRAGMA query_only = ON")
            with sqlite3.connect(destination) as target_connection:
                source_connection.backup(target_connection, pages=64)
        finally:
            source_connection.close()
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise PortableExportError("sqlite_snapshot_failed") from exc


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return bool(connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone())


def _read_buckets(source_root: Path) -> tuple[list[dict[str, Any]], set[str]]:
    records: list[dict[str, Any]] = []
    ids: set[str] = set()
    for path in _discover_bucket_paths(source_root):
        try:
            post = frontmatter.load(path)
            metadata = _json_value(dict(post.metadata))
        except PortableExportError:
            raise
        except Exception as exc:
            raise PortableExportError("bucket_invalid") from exc
        if not isinstance(metadata, dict):
            raise PortableExportError("bucket_invalid")
        bucket_id = metadata.get("id")
        if not isinstance(bucket_id, str) or not bucket_id.strip() or bucket_id in ids:
            raise PortableExportError("bucket_invalid")
        if _is_sealed(metadata):
            continue
        ids.add(bucket_id)
        raw_frontmatter = {
            key: value for key, value in metadata.items()
            if key not in _INTERNAL_FRONTMATTER_FIELDS
        }
        records.append({
            "bucket_id": bucket_id,
            "body": post.content,
            "frontmatter": raw_frontmatter,
        })
    return sorted(records, key=lambda item: item["bucket_id"]), ids


def _filter_relation_value(value: Any, visible_ids: set[str]) -> Any:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item in visible_ids]
    if isinstance(value, tuple):
        return [item for item in value if isinstance(item, str) and item in visible_ids]
    if isinstance(value, str):
        if "," in value:
            return ",".join(
                item for item in (part.strip() for part in value.split(","))
                if item in visible_ids
            )
        return value if value in visible_ids else ""
    return "" if value is not None else None


def _filter_visible_relations(records: list[dict[str, Any]], visible_ids: set[str]) -> None:
    for record in records:
        metadata = record["frontmatter"]
        for field in _RELATION_FIELDS:
            if field in metadata:
                metadata[field] = _filter_relation_value(metadata[field], visible_ids)
        provenance = metadata.get("todo_provenance")
        if isinstance(provenance, list):
            cleaned = []
            for item in provenance:
                if not isinstance(item, dict):
                    cleaned.append(item)
                    continue
                copied = dict(item)
                if "source_bucket" in copied and copied["source_bucket"] not in visible_ids:
                    copied["source_bucket"] = None
                cleaned.append(copied)
            metadata["todo_provenance"] = cleaned


def _read_history(snapshot: Path, visible_ids: set[str]) -> list[dict[str, Any]]:
    if not snapshot.exists() or not visible_ids:
        return []
    try:
        with sqlite3.connect(snapshot) as connection:
            connection.row_factory = sqlite3.Row
            if not _table_exists(connection, "bucket_history"):
                return []
            placeholders = ",".join("?" for _ in visible_ids)
            rows = connection.execute(
                f"""
                SELECT id, bucket_id, old_content, changed_at, change_type
                FROM bucket_history
                WHERE bucket_id IN ({placeholders})
                ORDER BY bucket_id ASC, changed_at ASC, id ASC
                """,
                sorted(visible_ids),
            ).fetchall()
    except sqlite3.Error as exc:
        raise PortableExportError("sqlite_snapshot_invalid") from exc
    return [dict(row) for row in rows]


def _read_letters(snapshot: Path) -> list[dict[str, Any]]:
    if not snapshot.exists():
        return []
    try:
        with sqlite3.connect(snapshot) as connection:
            connection.row_factory = sqlite3.Row
            if not _table_exists(connection, "letters"):
                return []
            rows = connection.execute(
                "SELECT id, content, created_at, session_id, sealed FROM letters "
                "WHERE sealed = 0 ORDER BY id ASC"
            ).fetchall()
    except sqlite3.Error as exc:
        raise PortableExportError("sqlite_snapshot_invalid") from exc
    return [dict(row) for row in rows]


def _read_notes(snapshot: Path) -> list[dict[str, Any]]:
    if not snapshot.exists():
        return []
    try:
        with sqlite3.connect(snapshot) as connection:
            connection.row_factory = sqlite3.Row
            if not _table_exists(connection, "notes"):
                return []
            rows = connection.execute(
                """
                SELECT note_id, created_at, author, via, text, sealed, open_at,
                       boot_delivered_at, read_at, skipped_at, skipped_reason,
                       dismissed_at
                FROM notes WHERE sealed = 0 ORDER BY note_id ASC
                """
            ).fetchall()
    except sqlite3.Error as exc:
        raise PortableExportError("sqlite_snapshot_invalid") from exc
    return [dict(row) for row in rows]


def _read_assets(snapshot: Path, source_root: Path) -> tuple[list[dict[str, Any]], list[tuple[Path, str]]]:
    if not snapshot.exists():
        return [], []
    try:
        with sqlite3.connect(snapshot) as connection:
            connection.row_factory = sqlite3.Row
            if not _table_exists(connection, "assets"):
                return [], []
            rows = connection.execute("SELECT * FROM assets ORDER BY asset_id ASC").fetchall()
            tags: dict[str, list[dict[str, Any]]] = {}
            if _table_exists(connection, "asset_tags"):
                for tag in connection.execute(
                    "SELECT asset_id, tag_normalized, tag_display, created_at "
                    "FROM asset_tags ORDER BY asset_id ASC, tag_normalized ASC"
                ).fetchall():
                    tags.setdefault(tag["asset_id"], []).append(dict(tag))
    except sqlite3.Error as exc:
        raise PortableExportError("sqlite_snapshot_invalid") from exc
    asset_root = source_root / "assets"
    records: list[dict[str, Any]] = []
    blobs: list[tuple[Path, str]] = []
    for row in rows:
        asset = dict(row)
        digest = asset.get("stored_sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise PortableExportError("asset_invalid")
        blob_path = _safe_asset_path(asset_root, asset.pop("stored_relpath", None))
        payload = blob_path.read_bytes()
        if len(payload) != asset.get("stored_bytes") or _sha256_bytes(payload) != digest:
            raise PortableExportError("asset_corrupt")
        asset["tags"] = tags.get(asset["asset_id"], [])
        asset["blob_sha256"] = digest
        asset["blob_path"] = f"blobs/sha256/{digest}"
        records.append(_json_value(asset))
        blobs.append((blob_path, digest))
    return records, blobs


def _discover_asset_paths(source_root: Path) -> list[Path]:
    asset_root = source_root / "assets"
    if not asset_root.exists():
        return []
    if asset_root.is_symlink() or not asset_root.is_dir():
        raise PortableExportError("asset_path_unsafe")
    paths: list[Path] = []
    for current, directories, filenames in os.walk(asset_root, followlinks=False):
        current_path = Path(current)
        if current_path.is_symlink() or any(
            (current_path / child).is_symlink() for child in directories
        ):
            raise PortableExportError("asset_path_unsafe")
        if current_path == asset_root / ".tmp":
            directories[:] = []
            continue
        for filename in filenames:
            path = current_path / filename
            if path.is_symlink():
                raise PortableExportError("asset_path_unsafe")
            paths.append(path)
    return paths


def _source_inventory(source_root: Path) -> dict[str, tuple[int, int, str]]:
    paths = _discover_bucket_paths(source_root)
    for candidate in (
        source_root / "bucket_history.sqlite3", source_root / "bucket_history.sqlite3-wal",
        source_root / "bucket_history.sqlite3-shm", source_root / "assets.sqlite3",
        source_root / "assets.sqlite3-wal", source_root / "assets.sqlite3-shm",
        source_root / ".emotion_timeline.json",
    ):
        if candidate.exists():
            paths.append(candidate)
    paths.extend(_discover_asset_paths(source_root))
    inventory: dict[str, tuple[int, int, str]] = {}
    for path in sorted(set(paths), key=lambda item: item.as_posix()):
        if path.is_symlink() or not path.is_file():
            raise PortableExportError("source_changed")
        inventory[path.relative_to(source_root).as_posix()] = _file_evidence(path)
    return inventory


def _write_payload(path: Path, payload: bytes) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return {"sha256": _sha256_bytes(payload), "bytes": len(payload)}


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    payload = b"".join(_canonical_json(record) for record in records)
    result = _write_payload(path, payload)
    result["records"] = len(records)
    return result


def _copy_blob(source: Path, destination: Path, digest: str) -> dict[str, Any]:
    payload = source.read_bytes()
    if _sha256_bytes(payload) != digest:
        raise PortableExportError("asset_corrupt")
    return _write_payload(destination, payload)


def _read_emotion_timeline(source_root: Path) -> tuple[Any, bool]:
    path = source_root / ".emotion_timeline.json"
    if not path.exists():
        return [], False
    try:
        return json.loads(path.read_text(encoding="utf-8")), True
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableExportError("emotion_timeline_invalid") from exc


async def export_ordinary_portable(
    *,
    buckets_dir: str | Path,
    destination: str | Path,
    confirm_export: bool,
    coordinator: MaintenanceWriteCoordinator = DEFAULT_WRITE_COORDINATOR,
    environ: dict[str, str] | None = None,
    clock: Callable[[], datetime] | None = None,
    drain_timeout_seconds: float = 10.0,
    max_freeze_seconds: float = 60.0,
) -> dict[str, Any]:
    """Publish a verified unsealed ordinary-memory export, or publish nothing."""
    if confirm_export is not True:
        raise PortableExportError("confirmation_required")
    try:
        authority = load_asset_authority(environ).authority
    except Exception as exc:
        raise PortableExportError("asset_authority_invalid") from exc
    if authority is not AssetAuthority.LEGACY:
        raise PortableExportError("asset_authority_unsupported")

    source_root, final_destination = _validate_destination(Path(buckets_dir), Path(destination))
    staging = Path(tempfile.mkdtemp(prefix=f".{final_destination.name}.staging-", dir=final_destination.parent))
    published = False
    try:
        async with coordinator.freeze(
            reason="portable_export",
            drain_timeout_seconds=drain_timeout_seconds,
            max_freeze_seconds=max_freeze_seconds,
        ) as lease:
            coordinator.validate_lease(lease)
            initial_inventory = _source_inventory(source_root)
            history_source = source_root / "bucket_history.sqlite3"
            assets_source = source_root / "assets.sqlite3"
            history_snapshot = staging / ".history.snapshot.sqlite3"
            assets_snapshot = staging / ".assets.snapshot.sqlite3"
            _snapshot_sqlite(history_source, history_snapshot)
            _snapshot_sqlite(assets_source, assets_snapshot)

            buckets, visible_ids = _read_buckets(source_root)
            _filter_visible_relations(buckets, visible_ids)
            assets, blob_sources = _read_assets(assets_snapshot, source_root)
            history = _read_history(history_snapshot, visible_ids)
            letters = _read_letters(history_snapshot)
            notes = _read_notes(history_snapshot)
            emotion_timeline, timeline_present = _read_emotion_timeline(source_root)

            records_dir = staging / "records"
            file_metadata: dict[str, dict[str, Any]] = {}
            file_metadata["records/buckets.jsonl"] = _write_jsonl(records_dir / "buckets.jsonl", buckets)
            file_metadata["records/bucket_history.jsonl"] = _write_jsonl(records_dir / "bucket_history.jsonl", history)
            file_metadata["records/letters.jsonl"] = _write_jsonl(records_dir / "letters.jsonl", letters)
            file_metadata["records/notes.jsonl"] = _write_jsonl(records_dir / "notes.jsonl", notes)
            emotion_payload = _canonical_json(emotion_timeline)
            file_metadata["records/emotion_timeline.json"] = _write_payload(records_dir / "emotion_timeline.json", emotion_payload)
            file_metadata["records/emotion_timeline.json"]["records"] = len(emotion_timeline) if isinstance(emotion_timeline, list) else 1
            file_metadata["records/assets.jsonl"] = _write_jsonl(records_dir / "assets.jsonl", assets)
            copied_digests: set[str] = set()
            for source_blob, digest in blob_sources:
                if digest in copied_digests:
                    continue
                copied_digests.add(digest)
                relative = f"blobs/sha256/{digest}"
                file_metadata[relative] = _copy_blob(source_blob, staging / relative, digest)
                file_metadata[relative]["records"] = 1

            coordinator.validate_lease(lease)
            final_inventory = _source_inventory(source_root)
            if initial_inventory != final_inventory:
                raise PortableExportError("source_changed")

            # Snapshot databases are staging-only read aids, never export payload.
            history_snapshot.unlink(missing_ok=True)
            assets_snapshot.unlink(missing_ok=True)

            exported_at = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc).isoformat()
            manifest = {
                "schema_version": EXPORT_SCHEMA_VERSION,
                "exported_at": exported_at,
                "producer": {"name": "ombre-brain", "exporter_version": EXPORTER_VERSION},
                "profile": "ordinary_portable",
                "emotion_timeline_present": timeline_present,
                "files": {key: file_metadata[key] for key in sorted(file_metadata)},
                "record_counts": {
                    "assets": len(assets), "bucket_history": len(history), "buckets": len(buckets),
                    "letters": len(letters), "notes": len(notes),
                },
                "omissions": [
                    "sealed buckets, history, letters, and notes",
                    "relations to non-visible buckets",
                    "Remember-Me external authority and Raw Evidence",
                    "embeddings, dehydration cache, temporary files, WAL/SHM, locks",
                    "dashboard authentication, configuration, secrets, import state and journals",
                    "boot-delta events and checkpoints",
                    "historical frontmatter, provenance, todos, seal state, and links",
                ],
            }
            # Manifest is deliberately the final file: its presence marks a completed export.
            _write_payload(staging / "manifest.json", _canonical_json(manifest))
            coordinator.validate_lease(lease)

        os.replace(staging, final_destination)
        published = True
        return {
            "status": "ok",
            "destination": str(final_destination),
            "record_counts": manifest["record_counts"],
            "blob_count": len({digest for _, digest in blob_sources}),
        }
    except MaintenanceWriteError as exc:
        raise PortableExportError(exc.code) from exc
    finally:
        if not published and staging.exists():
            shutil.rmtree(staging)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export ordinary unsealed Ombre Brain data.")
    parser.add_argument("--buckets-dir", required=True, help="Explicit ordinary buckets directory.")
    parser.add_argument("--destination", required=True, help="New export directory to create.")
    parser.add_argument("--confirm-export", action="store_true", help="Required acknowledgement for writing an export.")
    parser.add_argument("--profile", default="portable", choices=("portable",), help="Only ordinary portable export is supported.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = asyncio.run(export_ordinary_portable(
            buckets_dir=args.buckets_dir,
            destination=args.destination,
            confirm_export=args.confirm_export,
        ))
    except PortableExportError as exc:
        print(json.dumps({"status": "error", "error": exc.code}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
