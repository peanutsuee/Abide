from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import unicodedata
import warnings
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from asset_migration_state import (
    AssetWriteGuard,
    HostMigrationState,
    HostMigrationStateError,
)
from maintenance_write_gate import (
    DEFAULT_WRITE_COORDINATOR,
    guarded_mutation,
)


MAX_IMAGE_PIXELS = 20_000_000
SUPPORTED_MIME_TYPES = {
    "application/octet-stream",
    "image/jpeg",
    "image/png",
}


class AssetStoreError(Exception):
    pass


class InvalidAssetImage(AssetStoreError):
    pass


class _NoopAssetWriteGuard:
    def __enter__(self) -> "_NoopAssetWriteGuard":
        return self

    def mark_changed(self) -> None:
        return None

    def mark_unchanged(self) -> None:
        return None

    def mark_rolled_back(self) -> None:
        return None

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None


class AssetStore:
    def __init__(
        self,
        data_root: str,
        write_gate: HostMigrationState | None = None,
        write_coordinator=None,
    ):
        self.write_coordinator = write_coordinator or DEFAULT_WRITE_COORDINATOR
        self.data_root = Path(data_root).resolve()
        if write_gate is not None and (
            not isinstance(write_gate, HostMigrationState)
            or not write_gate.validate_legacy_root(self.data_root)
        ):
            raise AssetStoreError("asset_write_gate_unavailable")
        self._write_gate = write_gate
        self.assets_dir = self.data_root / "assets"
        self.temp_dir = self.assets_dir / ".tmp"
        self.db_path = self.data_root / "assets.sqlite3"
        self._lock = threading.Lock()
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_db(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assets (
                    asset_id TEXT PRIMARY KEY,
                    source_sha256 TEXT NOT NULL,
                    stored_sha256 TEXT NOT NULL UNIQUE,
                    stored_relpath TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    decoded_bytes INTEGER NOT NULL,
                    stored_bytes INTEGER NOT NULL,
                    width INTEGER NOT NULL DEFAULT 0,
                    height INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(assets)").fetchall()
            }
            if "title" not in columns:
                conn.execute(
                    "ALTER TABLE assets ADD COLUMN title TEXT NOT NULL DEFAULT ''"
                )
            if "description" not in columns:
                conn.execute(
                    "ALTER TABLE assets ADD COLUMN description TEXT NOT NULL DEFAULT ''"
                )
            if "updated_at" not in columns:
                conn.execute(
                    "ALTER TABLE assets ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                "UPDATE assets SET updated_at = created_at WHERE updated_at = ''"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS asset_tags (
                    asset_id TEXT NOT NULL,
                    tag_normalized TEXT NOT NULL,
                    tag_display TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (asset_id, tag_normalized),
                    FOREIGN KEY (asset_id) REFERENCES assets(asset_id)
                        ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_stored_sha256 "
                "ON assets(stored_sha256)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_asset_tags_asset_id "
                "ON asset_tags(asset_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_asset_tags_normalized "
                "ON asset_tags(tag_normalized)"
            )

    @property
    def migration_write_gate(self) -> HostMigrationState | None:
        return self._write_gate

    def _asset_write_guard(
        self,
    ) -> AssetWriteGuard | _NoopAssetWriteGuard:
        if self._write_gate is None:
            return _NoopAssetWriteGuard()
        return self._write_gate.write_guard()

    @staticmethod
    def _translate_write_gate_error(exc: HostMigrationStateError) -> AssetStoreError:
        if exc.code == "asset_write_frozen":
            return AssetStoreError("asset_write_frozen")
        return AssetStoreError("asset_write_gate_unavailable")

    def _create_temp_path_unchecked(self, suffix: str = ".upload") -> Path:
        fd, raw_path = tempfile.mkstemp(prefix="rm-", suffix=suffix, dir=self.temp_dir)
        os.close(fd)
        return Path(raw_path)

    @guarded_mutation("asset_temp_create")
    def create_temp_path(self, suffix: str = ".upload") -> Path:
        try:
            with self._asset_write_guard() as write_guard:
                try:
                    path = self._create_temp_path_unchecked(suffix)
                except Exception:
                    write_guard.mark_rolled_back()
                    raise
                write_guard.mark_unchanged()
                return path
        except HostMigrationStateError as exc:
            raise self._translate_write_gate_error(exc) from exc

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        cleaned = re.sub(r"[\x00-\x1f\x7f/\\:]+", "_", (filename or "").strip())
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
        return cleaned[:255] or "asset.bin"

    @staticmethod
    def _clean_metadata_text(value: str, max_chars: int, field: str) -> str:
        if not isinstance(value, str):
            raise AssetStoreError(f"invalid_{field}")
        normalized = unicodedata.normalize("NFKC", value)
        cleaned = "".join(
            " " if unicodedata.category(char) in {"Cc", "Cf"} else char
            for char in normalized
        ).strip()
        if len(cleaned) > max_chars:
            raise AssetStoreError(f"{field}_too_long")
        return cleaned

    @classmethod
    def _normalize_tags(cls, tags: list[str]) -> list[tuple[str, str]]:
        if not isinstance(tags, list):
            raise AssetStoreError("invalid_tags")
        normalized_tags = []
        seen = set()
        for raw_tag in tags:
            display = re.sub(
                r"\s+",
                " ",
                cls._clean_metadata_text(raw_tag, 64, "tag"),
            ).strip()
            if not display:
                continue
            normalized = display.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            normalized_tags.append((normalized, display))
        if len(normalized_tags) > 30:
            raise AssetStoreError("too_many_tags")
        return normalized_tags

    @staticmethod
    def _parse_iso8601(value: str, field: str, end_of_day: bool = False):
        if not value:
            return None
        if not isinstance(value, str):
            raise AssetStoreError(f"invalid_{field}")
        raw = value.strip()
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                parsed = datetime.fromisoformat(raw)
                if end_of_day:
                    parsed = parsed.replace(
                        hour=23,
                        minute=59,
                        second=59,
                        microsecond=999999,
                    )
            else:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AssetStoreError(f"invalid_{field}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _row_datetime(value: str) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _tags_for_assets(
        conn: sqlite3.Connection,
        asset_ids: list[str],
    ) -> dict[str, list[dict]]:
        result = {asset_id: [] for asset_id in asset_ids}
        if not asset_ids:
            return result
        for start in range(0, len(asset_ids), 500):
            batch = asset_ids[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""
                SELECT asset_id, tag_normalized, tag_display, created_at
                FROM asset_tags
                WHERE asset_id IN ({placeholders})
                ORDER BY tag_normalized, tag_display
                """,
                batch,
            ).fetchall()
            for row in rows:
                result[row["asset_id"]].append(dict(row))
        return result

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(128 * 1024), b""):
                digest.update(block)
                size += len(block)
        return digest.hexdigest(), size

    def _clean_image(self, source_path: Path) -> tuple[Path, str, str, int, int]:
        output_path: Path | None = None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(source_path) as opened:
                    image_format = (opened.format or "").upper()
                    if image_format not in {"JPEG", "PNG"}:
                        raise InvalidAssetImage("unsupported_image_format")
                    width, height = opened.size
                    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                        raise InvalidAssetImage("image_pixel_limit")
                    opened.load()
                    oriented = ImageOps.exif_transpose(opened)
                    output_format = "PNG" if image_format == "PNG" else "JPEG"
                    output_mime = "image/png" if output_format == "PNG" else "image/jpeg"
                    extension = ".png" if output_format == "PNG" else ".jpg"
                    mode = "RGBA" if output_format == "PNG" and "A" in oriented.getbands() else "RGB"
                    converted = oriented.convert(mode)
                    clean = Image.new(mode, converted.size)
                    clean.paste(converted)
                    output_path = self._create_temp_path_unchecked(extension)
                    if output_format == "PNG":
                        clean.save(output_path, format="PNG", optimize=True)
                    else:
                        clean.save(
                            output_path,
                            format="JPEG",
                            quality=90,
                            optimize=True,
                            exif=b"",
                        )
                    width, height = clean.size
                    clean.close()
                    converted.close()
                    if oriented is not opened:
                        oriented.close()
            return output_path, output_mime, extension, width, height
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            if output_path:
                output_path.unlink(missing_ok=True)
            raise InvalidAssetImage("image_pixel_limit") from exc
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            if output_path:
                output_path.unlink(missing_ok=True)
            raise InvalidAssetImage("invalid_image") from exc
        except Exception:
            if output_path:
                output_path.unlink(missing_ok=True)
            raise

    def _prepare_candidate(
        self,
        source_path: Path,
        claimed_mime: str,
        require_image: bool = False,
    ) -> tuple[Path, str, str, str, int, int]:
        try:
            with Image.open(source_path) as probe:
                detected = (probe.format or "").upper()
        except UnidentifiedImageError:
            detected = ""
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise InvalidAssetImage("image_pixel_limit") from exc
        except OSError:
            detected = ""

        if detected:
            if detected not in {"JPEG", "PNG"}:
                raise InvalidAssetImage("unsupported_image_format")
            expected_mime = "image/jpeg" if detected == "JPEG" else "image/png"
            if claimed_mime.startswith("image/") and claimed_mime != expected_mime:
                raise InvalidAssetImage("image_mime_mismatch")
            candidate, mime_type, extension, width, height = self._clean_image(source_path)
            return candidate, mime_type, "image", extension, width, height
        if claimed_mime in {"image/jpeg", "image/png"}:
            raise InvalidAssetImage("invalid_image")
        if require_image:
            raise InvalidAssetImage("invalid_image")
        return source_path, "application/octet-stream", "file", ".bin", 0, 0

    @guarded_mutation("asset_persist")
    def persist_upload(
        self,
        source_path: str | Path,
        source_sha256: str,
        decoded_bytes: int,
        original_filename: str,
        mime_type: str,
        require_image: bool = False,
    ) -> dict:
        try:
            with self._asset_write_guard() as write_guard:
                return self._persist_upload_unchecked(
                    source_path=source_path,
                    source_sha256=source_sha256,
                    decoded_bytes=decoded_bytes,
                    original_filename=original_filename,
                    mime_type=mime_type,
                    require_image=require_image,
                    write_guard=write_guard,
                )
        except HostMigrationStateError as exc:
            raise self._translate_write_gate_error(exc) from exc

    @guarded_mutation("asset_persist_unchecked")
    def _persist_upload_unchecked(
        self,
        *,
        source_path: str | Path,
        source_sha256: str,
        decoded_bytes: int,
        original_filename: str,
        mime_type: str,
        require_image: bool,
        write_guard: AssetWriteGuard | _NoopAssetWriteGuard,
    ) -> dict:
        source = Path(source_path)
        candidate: Path | None = None
        destination: Path | None = None
        moved_new_file = False
        mutation_started = False
        commit_attempted = False
        outcome_marked = False
        claimed_mime = (mime_type or "application/octet-stream").strip().lower()

        try:
            if claimed_mime not in SUPPORTED_MIME_TYPES:
                raise AssetStoreError("unsupported_mime_type")
            actual_source_sha, actual_source_bytes = self._hash_file(source)
            if actual_source_bytes != decoded_bytes:
                raise AssetStoreError("source_size_mismatch")
            if not re.fullmatch(r"[0-9a-f]{64}", source_sha256 or ""):
                raise AssetStoreError("invalid_source_sha256")
            if not secrets.compare_digest(actual_source_sha, source_sha256):
                raise AssetStoreError("source_hash_mismatch")

            candidate, stored_mime, kind, extension, width, height = self._prepare_candidate(
                source, claimed_mime, require_image=require_image
            )
            stored_sha256, stored_bytes = self._hash_file(candidate)
            stored_relpath = Path("assets") / stored_sha256[:2] / f"{stored_sha256}{extension}"
            destination = self.data_root / stored_relpath
            filename = self.sanitize_filename(original_filename)

            with self._lock:
                conn = self._connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    existing = conn.execute(
                        "SELECT * FROM assets WHERE stored_sha256 = ?",
                        (stored_sha256,),
                    ).fetchone()
                    if existing:
                        conn.commit()
                        write_guard.mark_unchanged()
                        outcome_marked = True
                        result = self.get(existing["asset_id"]) or dict(existing)
                        result["deduplicated"] = True
                        return result

                    mutation_started = True
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists():
                        existing_sha, _ = self._hash_file(destination)
                        if not secrets.compare_digest(existing_sha, stored_sha256):
                            raise AssetStoreError("stored_file_conflict")
                    else:
                        os.replace(candidate, destination)
                        moved_new_file = True

                    asset_id = secrets.token_hex(16)
                    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    conn.execute(
                        """
                        INSERT INTO assets (
                            asset_id, source_sha256, stored_sha256, stored_relpath,
                            original_filename, mime_type, kind, decoded_bytes,
                            stored_bytes, width, height, created_at, title,
                            description, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?)
                        """,
                        (
                            asset_id,
                            source_sha256,
                            stored_sha256,
                            stored_relpath.as_posix(),
                            filename,
                            stored_mime,
                            kind,
                            decoded_bytes,
                            stored_bytes,
                            width,
                            height,
                            created_at,
                            created_at,
                        ),
                    )
                    commit_attempted = True
                    conn.commit()
                    write_guard.mark_changed()
                    outcome_marked = True
                    result = self.get(asset_id)
                    if result is None:
                        raise AssetStoreError("asset_insert_failed")
                    result["deduplicated"] = False
                    return result
                except Exception:
                    conn.rollback()
                    if (
                        moved_new_file
                        and destination
                        and not commit_attempted
                    ):
                        destination.unlink(missing_ok=True)
                    raise
                finally:
                    conn.close()
        except Exception:
            if not mutation_started and not outcome_marked:
                write_guard.mark_rolled_back()
            raise
        finally:
            source.unlink(missing_ok=True)
            if candidate and candidate != destination:
                candidate.unlink(missing_ok=True)

    def get(self, asset_id: str) -> dict | None:
        if not re.fullmatch(r"[0-9a-f]{32}", asset_id or ""):
            return None
        with self._connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT * FROM assets WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            if not row:
                return None
            tags = self._tags_for_assets(conn, [asset_id])[asset_id]
        result = dict(row)
        result["tags"] = [tag["tag_display"] for tag in tags]
        return result

    def get_import_record(self, asset_id: str) -> dict | None:
        """Return one legacy record with tag timestamps for trusted Host import."""
        if not re.fullmatch(r"[0-9a-f]{32}", asset_id or ""):
            return None
        with self._connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT * FROM assets WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            if not row:
                return None
            tags = self._tags_for_assets(conn, [asset_id])[asset_id]
        result = dict(row)
        result["tags"] = [
            {
                "value": tag["tag_display"],
                "created_at": tag["created_at"],
            }
            for tag in tags
        ]
        return result

    @staticmethod
    def _validate_migration_asset_id(
        value: str | None,
        error_code: str,
    ) -> None:
        if value is not None and (
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{32}", value) is None
        ):
            raise AssetStoreError(error_code)

    def get_migration_snapshot_bounds(self) -> tuple[str | None, int]:
        """Return the fixed maximum asset ID and row count in one read snapshot."""
        with self._connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute(
                """
                SELECT MAX(asset_id) AS upper_bound_asset_id,
                       COUNT(*) AS asset_count
                FROM assets
                """
            ).fetchone()
        return row["upper_bound_asset_id"], int(row["asset_count"])

    def list_asset_ids_for_migration(
        self,
        *,
        last_asset_id: str | None,
        upper_bound_asset_id: str | None,
        batch_size: int,
    ) -> list[str]:
        """Read one deterministic keyset page for the Host migration runner."""
        self._validate_migration_asset_id(
            last_asset_id,
            "invalid_migration_cursor",
        )
        self._validate_migration_asset_id(
            upper_bound_asset_id,
            "invalid_migration_upper_bound",
        )
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or not 1 <= batch_size <= 500
        ):
            raise AssetStoreError("invalid_migration_batch_size")
        if upper_bound_asset_id is None:
            return []
        with self._connect() as conn:
            conn.execute("BEGIN")
            if last_asset_id is None:
                rows = conn.execute(
                    """
                    SELECT asset_id
                    FROM assets
                    WHERE asset_id <= ?
                    ORDER BY asset_id ASC
                    LIMIT ?
                    """,
                    (upper_bound_asset_id, batch_size),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT asset_id
                    FROM assets
                    WHERE asset_id > ?
                      AND asset_id <= ?
                    ORDER BY asset_id ASC
                    LIMIT ?
                    """,
                    (
                        last_asset_id,
                        upper_bound_asset_id,
                        batch_size,
                    ),
                ).fetchall()
        return [row["asset_id"] for row in rows]

    def list_for_embedding(self, limit: int = 100) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise AssetStoreError("invalid_limit")
        with self._connect() as conn:
            conn.execute("BEGIN")
            rows = conn.execute(
                """
                SELECT * FROM assets
                ORDER BY updated_at DESC, asset_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            tags_by_asset = self._tags_for_assets(
                conn,
                [row["asset_id"] for row in rows],
            )
        assets = []
        for row in rows:
            asset = dict(row)
            asset["tags"] = [
                tag["tag_display"]
                for tag in tags_by_asset[row["asset_id"]]
            ]
            assets.append(asset)
        return assets

    @guarded_mutation("asset_metadata_update")
    def update_metadata(
        self,
        asset_id: str,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        try:
            with self._asset_write_guard() as write_guard:
                return self._update_metadata_unchecked(
                    asset_id=asset_id,
                    title=title,
                    description=description,
                    tags=tags,
                    write_guard=write_guard,
                )
        except HostMigrationStateError as exc:
            raise self._translate_write_gate_error(exc) from exc

    @guarded_mutation("asset_metadata_update_unchecked")
    def _update_metadata_unchecked(
        self,
        *,
        asset_id: str,
        title: str | None,
        description: str | None,
        tags: list[str] | None,
        write_guard: AssetWriteGuard | _NoopAssetWriteGuard,
    ) -> dict:
        try:
            asset_id = (asset_id or "").strip()
            if not re.fullmatch(r"[0-9a-f]{32}", asset_id):
                raise AssetStoreError("asset_unavailable")
            clean_title = (
                self._clean_metadata_text(title, 200, "title")
                if title is not None
                else None
            )
            clean_description = (
                self._clean_metadata_text(description, 4000, "description")
                if description is not None
                else None
            )
            clean_tags = self._normalize_tags(tags) if tags is not None else None
        except Exception:
            write_guard.mark_rolled_back()
            raise
        changed = False
        mutation_started = False

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM assets WHERE asset_id = ?",
                    (asset_id,),
                ).fetchone()
                if not row:
                    raise AssetStoreError("asset_unavailable")
                current_tags = conn.execute(
                    """
                    SELECT tag_normalized, tag_display
                    FROM asset_tags
                    WHERE asset_id = ?
                    ORDER BY tag_normalized
                    """,
                    (asset_id,),
                ).fetchall()
                changes = {}
                if clean_title is not None and clean_title != row["title"]:
                    changes["title"] = clean_title
                if clean_description is not None and clean_description != row["description"]:
                    changes["description"] = clean_description
                replace_tags = False
                if clean_tags is not None:
                    old_normalized = {item["tag_normalized"] for item in current_tags}
                    new_normalized = {item[0] for item in clean_tags}
                    replace_tags = old_normalized != new_normalized

                if changes or replace_tags:
                    changed = True
                    mutation_started = True
                    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    if changes:
                        assignments = ", ".join(f"{key} = ?" for key in changes)
                        conn.execute(
                            f"UPDATE assets SET {assignments}, updated_at = ? WHERE asset_id = ?",
                            [*changes.values(), updated_at, asset_id],
                        )
                    else:
                        conn.execute(
                            "UPDATE assets SET updated_at = ? WHERE asset_id = ?",
                            (updated_at, asset_id),
                        )
                    if replace_tags:
                        conn.execute(
                            "DELETE FROM asset_tags WHERE asset_id = ?",
                            (asset_id,),
                        )
                        conn.executemany(
                            """
                            INSERT INTO asset_tags (
                                asset_id, tag_normalized, tag_display, created_at
                            ) VALUES (?, ?, ?, ?)
                            """,
                            [
                                (asset_id, normalized, display, updated_at)
                                for normalized, display in clean_tags
                            ],
                        )
                conn.commit()
                if changed:
                    write_guard.mark_changed()
                else:
                    write_guard.mark_unchanged()
            except Exception:
                conn.rollback()
                if not mutation_started:
                    write_guard.mark_rolled_back()
                raise
            finally:
                conn.close()
        result = self.get(asset_id)
        if result is None:
            raise AssetStoreError("asset_unavailable")
        return result

    @guarded_mutation("asset_delete")
    def delete(self, asset_id: str) -> dict:
        try:
            with self._asset_write_guard() as write_guard:
                return self._delete_unchecked(
                    asset_id=asset_id,
                    write_guard=write_guard,
                )
        except HostMigrationStateError as exc:
            raise self._translate_write_gate_error(exc) from exc

    @guarded_mutation("asset_delete_unchecked")
    def _delete_unchecked(
        self,
        *,
        asset_id: str,
        write_guard: AssetWriteGuard | _NoopAssetWriteGuard,
    ) -> dict:
        try:
            asset_id = (asset_id or "").strip()
            if not re.fullmatch(r"[0-9a-f]{32}", asset_id):
                raise AssetStoreError("asset_unavailable")
        except Exception:
            write_guard.mark_rolled_back()
            raise

        mutation_started = False
        with self._lock:
            conn = self._connect()
            quarantine_path: Path | None = None
            original_path: Path | None = None
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM assets WHERE asset_id = ?",
                    (asset_id,),
                ).fetchone()
                if not row:
                    raise AssetStoreError("asset_unavailable")

                original_path = (self.data_root / row["stored_relpath"]).resolve()
                try:
                    original_path.relative_to(self.assets_dir.resolve())
                except ValueError as exc:
                    raise AssetStoreError("invalid_stored_path") from exc
                if not original_path.is_file():
                    raise AssetStoreError("asset_file_unavailable")

                quarantine_path = self.temp_dir / (
                    f"delete-{asset_id}-{secrets.token_hex(8)}{original_path.suffix}"
                )
                mutation_started = True
                os.replace(original_path, quarantine_path)
                cursor = conn.execute(
                    "DELETE FROM assets WHERE asset_id = ?",
                    (asset_id,),
                )
                if cursor.rowcount != 1:
                    raise AssetStoreError("asset_delete_failed")
                conn.commit()
                write_guard.mark_changed()
            except Exception:
                conn.rollback()
                if not mutation_started:
                    write_guard.mark_rolled_back()
                if (
                    quarantine_path
                    and original_path
                    and quarantine_path.exists()
                    and not original_path.exists()
                ):
                    try:
                        os.replace(quarantine_path, original_path)
                    except OSError as restore_exc:
                        raise AssetStoreError("asset_delete_restore_failed") from restore_exc
                raise
            finally:
                conn.close()

        cleanup_pending = False
        if quarantine_path:
            try:
                quarantine_path.unlink()
            except OSError:
                cleanup_pending = True
        return {
            "asset_id": asset_id,
            "deleted": True,
            "cleanup_pending": cleanup_pending,
        }

    def search(
        self,
        query: str = "",
        tags: list[str] | None = None,
        kind: str = "",
        mime_type: str = "",
        created_from: str = "",
        created_to: str = "",
        limit: int = 20,
        offset: int = 0,
        semantic_scores: dict[str, float] | None = None,
    ) -> dict:
        if not isinstance(query, str):
            raise AssetStoreError("invalid_query")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise AssetStoreError("invalid_limit")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise AssetStoreError("invalid_offset")
        kind = (kind or "").strip().lower()
        if kind not in {"", "image", "file"}:
            raise AssetStoreError("invalid_kind")
        mime_type = (mime_type or "").strip().lower()
        if mime_type and mime_type not in SUPPORTED_MIME_TYPES:
            raise AssetStoreError("invalid_mime_type")
        filter_tags = self._normalize_tags(tags or []) if tags is not None else []
        filter_normalized = {item[0] for item in filter_tags}
        created_from_dt = self._parse_iso8601(created_from, "created_from")
        created_to_dt = self._parse_iso8601(created_to, "created_to", end_of_day=True)
        if created_from_dt and created_to_dt and created_from_dt > created_to_dt:
            raise AssetStoreError("invalid_date_range")
        normalized_query = unicodedata.normalize("NFKC", query).strip().casefold()

        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if mime_type:
            clauses.append("mime_type = ?")
            params.append(mime_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            conn.execute("BEGIN")
            rows = conn.execute(f"SELECT * FROM assets {where}", params).fetchall()
            tags_by_asset = self._tags_for_assets(conn, [row["asset_id"] for row in rows])

        matches = []
        for row in rows:
            created_at = self._row_datetime(row["created_at"])
            if created_from_dt and created_at < created_from_dt:
                continue
            if created_to_dt and created_at > created_to_dt:
                continue
            row_tags = tags_by_asset[row["asset_id"]]
            row_tag_normalized = {item["tag_normalized"] for item in row_tags}
            if filter_normalized and not filter_normalized.issubset(row_tag_normalized):
                continue

            reasons = []
            rank = 6
            semantic_score = float(
                (semantic_scores or {}).get(row["asset_id"], 0.0)
            )
            if normalized_query:
                asset_id_text = row["asset_id"].casefold()
                filename_text = unicodedata.normalize("NFKC", row["original_filename"]).casefold()
                title_text = unicodedata.normalize("NFKC", row["title"]).casefold()
                description_text = unicodedata.normalize("NFKC", row["description"]).casefold()
                if asset_id_text == normalized_query:
                    reasons.append("asset_id_exact")
                    rank = min(rank, 0)
                elif normalized_query in asset_id_text:
                    reasons.append("asset_id")
                    rank = min(rank, 5)
                if normalized_query in row_tag_normalized:
                    reasons.append("tag_exact")
                    rank = min(rank, 1)
                elif any(normalized_query in item["tag_normalized"] for item in row_tags):
                    reasons.append("tag")
                    rank = min(rank, 5)
                if title_text == normalized_query:
                    reasons.append("title_exact")
                    rank = min(rank, 2)
                elif title_text.startswith(normalized_query):
                    reasons.append("title_prefix")
                    rank = min(rank, 2)
                elif normalized_query in title_text:
                    reasons.append("title")
                    rank = min(rank, 5)
                if normalized_query in filename_text:
                    reasons.append("filename")
                    rank = min(rank, 3)
                if normalized_query in description_text:
                    reasons.append("description")
                    rank = min(rank, 5)
                if semantic_score > 0:
                    reasons.append("semantic")
                if not reasons:
                    continue

            matches.append({
                "rank": rank,
                "created_at_dt": created_at,
                "result": {
                    "asset_id": row["asset_id"],
                    "filename": row["original_filename"],
                    "title": row["title"],
                    "description": row["description"],
                    "tags": [item["tag_display"] for item in row_tags],
                    "kind": row["kind"],
                    "mime_type": row["mime_type"],
                    "width": row["width"],
                    "height": row["height"],
                    "stored_bytes": row["stored_bytes"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "match_reasons": reasons,
                },
            })
            if semantic_score > 0:
                matches[-1]["semantic_score"] = semantic_score
                matches[-1]["result"]["semantic_score"] = semantic_score
        matches.sort(key=lambda item: (
            item["rank"],
            -item.get("semantic_score", 0.0) if item["rank"] == 6 else 0.0,
            -item["created_at_dt"].timestamp(),
            item["result"]["asset_id"],
        ))
        total = len(matches)
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "results": [item["result"] for item in matches[offset:offset + limit]],
        }

    def resolve_file(self, asset_id: str) -> tuple[dict, Path] | None:
        asset = self.get(asset_id)
        if not asset:
            return None
        path = (self.data_root / asset["stored_relpath"]).resolve()
        try:
            path.relative_to(self.assets_dir.resolve())
        except ValueError as exc:
            raise AssetStoreError("invalid_stored_path") from exc
        if not path.is_file():
            return None
        return asset, path
