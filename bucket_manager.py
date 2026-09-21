# ============================================================
# Module: Memory Bucket Manager (bucket_manager.py)
# 模块：记忆桶管理器
#
# CRUD operations, multi-dimensional index search, activation updates
# for memory buckets.
# 记忆桶的增删改查、多维索引搜索、激活更新。
#
# Core design:
# 核心逻辑：
#   - Each bucket = one Markdown file (YAML frontmatter + body)
#     每个记忆桶 = 一个 Markdown 文件
#   - Storage by type: permanent / dynamic / archive
#     存储按类型分目录
#   - Multi-dimensional soft index: domain + valence/arousal + fuzzy text
#     多维软索引：主题域 + 情感坐标 + 文本模糊匹配
#   - Search strategy: domain pre-filter → weighted multi-dim ranking
#     搜索策略：主题域预筛 → 多维加权精排
#   - Emotion coordinates based on Russell circumplex model:
#     情感坐标基于环形情感模型（Russell circumplex）：
#       valence (0~1): 0=negative → 1=positive
#       arousal (0~1): 0=calm → 1=excited
#
# Depended on by: server.py, decay_engine.py
# 被谁依赖：server.py, decay_engine.py
# ============================================================

import os
import math
import logging
import shutil
import sqlite3
import hashlib
import json
import tempfile
import inspect
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import frontmatter
from rapidfuzz import fuzz

from maintenance_write_gate import (
    DEFAULT_WRITE_COORDINATOR,
    guarded_async_mutation,
    guarded_mutation,
    guarded_optional_async_mutation,
)

from utils import (
    generate_bucket_id,
    sanitize_name,
    safe_path,
    now_iso,
)

logger = logging.getLogger("ombre_brain.bucket")

_IMPORT_MARKER_FIELD = "_ob_import_operations"
_IMPORT_OPERATION_STATUSES = frozenset({"planned", "applied"})
_BOOT_DELTA_PROFILES = ("talk", "code", "tg")
TODO_SAID_BY_VALUES = frozenset({"ting", "model", "system", "unknown"})
PROVENANCE_KIND_VALUES = frozenset({"unknown", "summary", "inference", "system"})


def normalize_provenance_kind(raw: Any, *, strict: bool = False) -> str:
    """Return a safe bucket-body provenance classification.

    Existing frontmatter is intentionally permissive: absent or corrupted
    values are read as ``unknown`` and never rewritten.  New explicit writes
    instead fail closed so a caller cannot silently manufacture a label.
    """
    if isinstance(raw, str) and raw in PROVENANCE_KIND_VALUES:
        return raw
    if strict:
        raise ValueError(
            "provenance_kind must be unknown, summary, inference, or system."
        )
    return "unknown"


def canonicalize_todos(raw: Any) -> list[str]:
    """Return todos in the canonical, ordered ``list[str]`` form."""
    if isinstance(raw, list):
        values = [str(item).strip() for item in raw if item is not None and str(item).strip()]
    elif isinstance(raw, dict):
        values = [
            f"{key}: {value}".strip()
            for key, value in raw.items()
            if str(value).strip()
        ]
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None and parsed != raw:
            return canonicalize_todos(parsed)
        values = [
            line.strip().lstrip("-* ").strip()
            for line in text.replace(",", "\n").splitlines()
            if line.strip().lstrip("-* ").strip()
        ]
    elif raw is None:
        values = []
    else:
        text = str(raw).strip()
        values = [text] if text else []
    return list(dict.fromkeys(values))


def _todo_provenance_record(
    raw: Any,
    *,
    strict: bool,
) -> dict[str, Any] | None:
    """Validate one optional todo-provenance sidecar record.

    ``todos`` deliberately remains a list of text strings.  This sidecar is
    therefore allowed to be missing or malformed on existing files without
    affecting normal todo reads.  Explicit new writes use ``strict=True`` and
    reject malformed values rather than silently recording invented metadata.
    """
    if not isinstance(raw, dict):
        if strict:
            raise ValueError("todo provenance entries must be objects.")
        return None
    allowed = {"text", "said_by", "said_at", "source_bucket"}
    unexpected = set(raw) - allowed
    if unexpected:
        if strict:
            raise ValueError("todo provenance entries contain unsupported fields.")
        return None
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        if strict:
            raise ValueError("todo provenance text must be a non-empty string.")
        return None
    said_by = raw.get("said_by", "unknown")
    if not isinstance(said_by, str) or said_by not in TODO_SAID_BY_VALUES:
        if strict:
            raise ValueError(
                "todo provenance said_by must be ting, model, system, or unknown."
            )
        return None
    said_at = raw.get("said_at")
    if said_at is not None:
        if not isinstance(said_at, str) or not said_at.strip():
            if strict:
                raise ValueError("todo provenance said_at must be an ISO-8601 string or null.")
            return None
        said_at = said_at.strip()
        try:
            datetime.fromisoformat(said_at)
        except (TypeError, ValueError):
            if strict:
                raise ValueError("todo provenance said_at must be an ISO-8601 string or null.")
            return None
    source_bucket = raw.get("source_bucket")
    if source_bucket is not None:
        if not isinstance(source_bucket, str):
            if strict:
                raise ValueError("todo provenance source_bucket must be a string or null.")
            return None
        source_bucket = source_bucket.strip() or None
    return {
        "text": text.strip(),
        "said_by": said_by,
        "said_at": said_at,
        "source_bucket": source_bucket,
    }


def reconcile_todo_provenance(
    todos: Any,
    raw_provenance: Any,
    *,
    strict: bool = False,
) -> list[dict[str, Any]]:
    """Return safe sidecar records for the canonical todo text list.

    First valid record wins for duplicate canonical text.  A missing sidecar
    record means unknown provenance; this helper never manufactures records
    for legacy or automatic-extraction todos.
    """
    canonical_todos = canonicalize_todos(todos)
    if raw_provenance is None:
        return []
    if not isinstance(raw_provenance, list):
        if strict:
            raise ValueError("todo_provenance must be a list.")
        return []
    allowed_texts = set(canonical_todos)
    records_by_text: dict[str, dict[str, Any]] = {}
    for raw_record in raw_provenance:
        record = _todo_provenance_record(raw_record, strict=strict)
        if record is None:
            continue
        if record["text"] not in allowed_texts:
            if strict:
                raise ValueError("todo provenance text must appear in todos.")
            continue
        records_by_text.setdefault(record["text"], record)
    return [records_by_text[text] for text in canonical_todos if text in records_by_text]


def _todo_provenance_is_known(record: dict[str, Any] | None) -> bool:
    return bool(record) and (
        record.get("said_by") != "unknown"
        or record.get("said_at") is not None
        or record.get("source_bucket") is not None
    )


def merge_todo_provenance(
    target_todos: Any,
    target_provenance: Any,
    source_todos: Any,
    source_provenance: Any,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Merge todo text and sidecars without arbitrarily choosing attribution."""
    target = canonicalize_todos(target_todos)
    source = canonicalize_todos(source_todos)
    merged_todos = list(dict.fromkeys(target + source))
    target_records = {
        record["text"]: record
        for record in reconcile_todo_provenance(target, target_provenance)
    }
    source_records = {
        record["text"]: record
        for record in reconcile_todo_provenance(source, source_provenance)
    }
    merged_records = []
    for text in merged_todos:
        target_record = target_records.get(text)
        source_record = source_records.get(text)
        if target_record is None:
            chosen = source_record
        elif source_record is None or target_record == source_record:
            chosen = target_record
        elif _todo_provenance_is_known(target_record) and not _todo_provenance_is_known(source_record):
            chosen = target_record
        elif _todo_provenance_is_known(source_record) and not _todo_provenance_is_known(target_record):
            chosen = source_record
        else:
            # Both sides claim incompatible known provenance.  Preserve the
            # todo text but deliberately discard unsupported attribution.
            chosen = {
                "text": text,
                "said_by": "unknown",
                "said_at": None,
                "source_bucket": None,
            }
        if chosen is not None:
            merged_records.append(dict(chosen))
    return merged_todos, merged_records


class BucketIdempotencyError(RuntimeError):
    """Content-free failure for the O5B memory idempotency seam."""

    def __init__(self, code: str = "idempotency_conflict") -> None:
        self.code = code
        super().__init__(code)


def _date_only(value: str | None = None) -> str:
    """Return YYYY-MM-DD, defaulting to today when parsing fails."""
    if value:
        try:
            return datetime.fromisoformat(str(value)).date().isoformat()
        except (ValueError, TypeError):
            pass
    return datetime.now().date().isoformat()


def _is_sealed_bucket(bucket: Any) -> bool:
    """Return True for either a loaded bucket dict or a frontmatter Post."""
    metadata = bucket.get("metadata") if isinstance(bucket.get("metadata"), dict) else bucket
    try:
        return int(metadata.get("sealed", 0) or 0) == 1
    except (TypeError, ValueError):
        return False


class BucketManager:
    """
    Memory bucket manager — entry point for all bucket CRUD operations.
    Buckets are stored as Markdown files with YAML frontmatter for metadata
    and body for content. Natively compatible with Obsidian browsing/editing.
    记忆桶管理器 —— 所有桶的 CRUD 操作入口。
    桶以 Markdown 文件存储，YAML frontmatter 存元数据，正文存内容。
    天然兼容 Obsidian 直接浏览和编辑。
    """

    def __init__(
        self,
        config: dict,
        embedding_engine=None,
        write_coordinator=None,
    ):
        self.write_coordinator = write_coordinator or DEFAULT_WRITE_COORDINATOR
        # --- Read storage paths from config / 从配置中读取存储路径 ---
        self.base_dir = config["buckets_dir"]
        self.permanent_dir = os.path.join(self.base_dir, "permanent")
        self.dynamic_dir = os.path.join(self.base_dir, "dynamic")
        self.archive_dir = os.path.join(self.base_dir, "archive")
        self.feel_dir = os.path.join(self.base_dir, "feel")
        self.history_db_path = os.path.join(self.base_dir, "bucket_history.sqlite3")
        self.fuzzy_threshold = config.get("matching", {}).get("fuzzy_threshold", 50)
        self.max_results = config.get("matching", {}).get("max_results", 5)

        # --- Wikilink config / 双链配置 ---
        wikilink_cfg = config.get("wikilink", {})
        self.wikilink_enabled = wikilink_cfg.get("enabled", True)
        self.wikilink_use_tags = wikilink_cfg.get("use_tags", False)
        self.wikilink_use_domain = wikilink_cfg.get("use_domain", True)
        self.wikilink_use_auto_keywords = wikilink_cfg.get("use_auto_keywords", True)
        self.wikilink_auto_top_k = wikilink_cfg.get("auto_top_k", 8)
        self.wikilink_min_len = wikilink_cfg.get("min_keyword_len", 2)
        self.wikilink_exclude_keywords = set(wikilink_cfg.get("exclude_keywords", []))
        self.wikilink_stopwords = {
            "的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
            "都", "一个", "上", "也", "很", "到", "说", "要", "去",
            "你", "会", "着", "没有", "看", "好", "自己", "这", "他", "她",
            "我们", "你们", "他们", "然后", "今天", "昨天", "明天", "一下",
            "the", "and", "for", "are", "but", "not", "you", "all", "can",
            "had", "her", "was", "one", "our", "out", "has", "have", "with",
            "this", "that", "from", "they", "been", "said", "will", "each",
        }
        self.wikilink_stopwords |= {w.lower() for w in self.wikilink_exclude_keywords}

        # --- Search scoring weights / 检索权重配置 ---
        scoring = config.get("scoring_weights", {})
        self.w_topic = scoring.get("topic_relevance", 4.0)
        self.w_emotion = scoring.get("emotion_resonance", 2.0)
        self.w_time = scoring.get("time_proximity", 1.5)
        self.w_importance = scoring.get("importance", 1.0)
        self.content_weight = scoring.get("content_weight", 1.0)  # body×1, per spec

        # --- Optional embedding engine for pre-filtering / 可选 embedding 引擎，用于预筛候选集 ---
        self.embedding_engine = embedding_engine
        self._init_history_db()

    async def _delete_ordinary_embedding(self, bucket_id: str) -> None:
        """Delete the local ordinary vector, regardless of provider enablement."""
        if self.embedding_engine is None:
            return
        delete_embedding = getattr(self.embedding_engine, "delete_embedding", None)
        if delete_embedding is None:
            return
        result = delete_embedding(bucket_id)
        if inspect.isawaitable(result):
            await result

    async def _refresh_ordinary_embedding_best_effort(
        self,
        bucket_id: str,
        content: str,
    ) -> None:
        """Refresh an unsealed vector without making provider failure fatal."""
        if not self.embedding_engine or not getattr(self.embedding_engine, "enabled", False):
            return
        try:
            await self.embedding_engine.generate_and_store(bucket_id, content)
        except Exception as exc:
            logger.warning(f"Embedding refresh failed for {bucket_id}: {exc}")

    def _init_history_db(self) -> None:
        """Create the write-ahead bucket history table if needed."""
        os.makedirs(self.base_dir, exist_ok=True)
        with sqlite3.connect(self.history_db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bucket_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bucket_id TEXT NOT NULL,
                    old_content TEXT NOT NULL,
                    changed_at TEXT NOT NULL,
                    change_type TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bucket_history_bucket_id "
                "ON bucket_history(bucket_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS letters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    sealed INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(letters)").fetchall()
            }
            if "sealed" not in columns:
                conn.execute(
                    "ALTER TABLE letters ADD COLUMN sealed INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_letters_created_at "
                "ON letters(created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    note_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    author TEXT NOT NULL,
                    via TEXT NOT NULL,
                    text TEXT NOT NULL,
                    sealed INTEGER NOT NULL DEFAULT 0,
                    open_at TEXT,
                    boot_delivered_at TEXT,
                    read_at TEXT,
                    skipped_at TEXT,
                    skipped_reason TEXT,
                    dismissed_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notes_boot_delivery "
                "ON notes(sealed, open_at, boot_delivered_at, skipped_at, note_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notes_created_at "
                "ON notes(created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS boot_delta_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bucket_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_boot_delta_events_bucket_id "
                "ON boot_delta_events(bucket_id, id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS boot_delta_checkpoint (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    last_event_id INTEGER NOT NULL,
                    completed_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS boot_delta_profile_checkpoints (
                    profile TEXT PRIMARY KEY,
                    last_event_id INTEGER NOT NULL,
                    completed_at TEXT NOT NULL
                )
                """
            )
            profile_count = conn.execute(
                "SELECT COUNT(*) FROM boot_delta_profile_checkpoints"
            ).fetchone()[0]
            legacy_checkpoint = conn.execute(
                """
                SELECT last_event_id, completed_at
                FROM boot_delta_checkpoint
                WHERE singleton = 1
                """
            ).fetchone()
            if profile_count == 0 and legacy_checkpoint is not None:
                conn.executemany(
                    """
                    INSERT INTO boot_delta_profile_checkpoints
                        (profile, last_event_id, completed_at)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (profile, int(legacy_checkpoint[0]), legacy_checkpoint[1])
                        for profile in _BOOT_DELTA_PROFILES
                    ],
                )

    def _ensure_import_operation_table(self) -> None:
        """Create the lazy O5B operation journal only when capture is used."""

        with sqlite3.connect(self.history_db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ob_import_operations (
                    operation_key TEXT PRIMARY KEY,
                    operation_kind TEXT NOT NULL CHECK (operation_kind IN ('create', 'update')),
                    target_bucket_id TEXT,
                    payload_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    result_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('planned', 'applied')),
                    memory_mutation_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(ob_import_operations)"
                ).fetchall()
            }
            if "memory_mutation_id" not in columns:
                conn.execute(
                    "ALTER TABLE ob_import_operations "
                    "ADD COLUMN memory_mutation_id TEXT"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ob_import_operations_target "
                "ON ob_import_operations(target_bucket_id)"
            )

    @staticmethod
    def _canonical_import_payload(payload: dict[str, Any]) -> tuple[str, str]:
        if not isinstance(payload, dict):
            raise BucketIdempotencyError("operation_payload_invalid")
        try:
            serialized = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise BucketIdempotencyError("operation_payload_invalid") from exc
        return serialized, hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _operation_result_id(operation_key: str) -> str:
        return hashlib.sha256(
            f"ombre-brain:o5b:bucket:{operation_key}".encode("utf-8")
        ).hexdigest()[:32]

    def _get_import_operation(self, operation_key: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM ob_import_operations WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["payload"] = json.loads(result.pop("payload_json"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BucketIdempotencyError("operation_payload_invalid") from exc
        return result

    def _ensure_import_operation(
        self,
        operation_key: str,
        *,
        operation_kind: str | None = None,
        target_bucket_id: str | None = None,
        payload: dict[str, Any] | None = None,
        payload_digest: str | None = None,
        memory_mutation_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(operation_key, str) or not operation_key or len(operation_key) > 128:
            raise BucketIdempotencyError("operation_key_invalid")
        if operation_kind is not None and operation_kind not in {"create", "update"}:
            raise BucketIdempotencyError("operation_kind_invalid")
        if target_bucket_id is not None and not isinstance(target_bucket_id, str):
            raise BucketIdempotencyError("target_bucket_invalid")
        if memory_mutation_id is not None and (
            not isinstance(memory_mutation_id, str)
            or len(memory_mutation_id) != 64
            or any(char not in "0123456789abcdef" for char in memory_mutation_id)
        ):
            raise BucketIdempotencyError("memory_mutation_invalid")
        if payload is not None:
            serialized, computed_digest = self._canonical_import_payload(payload)
            if payload_digest is not None and payload_digest != computed_digest:
                raise BucketIdempotencyError("operation_payload_conflict")
            payload_digest = computed_digest
        else:
            serialized = None

        self._ensure_import_operation_table()
        now = now_iso()
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM ob_import_operations WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if row is None:
                if operation_kind is None or serialized is None or payload_digest is None:
                    conn.rollback()
                    raise BucketIdempotencyError("operation_plan_missing")
                result_id = (
                    self._operation_result_id(operation_key)
                    if operation_kind == "create"
                    else target_bucket_id
                )
                if not result_id:
                    conn.rollback()
                    raise BucketIdempotencyError("target_bucket_invalid")
                conn.execute(
                    """
                    INSERT INTO ob_import_operations (
                        operation_key, operation_kind, target_bucket_id,
                        payload_json, payload_digest, result_id, status,
                        memory_mutation_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?)
                    """,
                    (
                        operation_key,
                        operation_kind,
                        target_bucket_id,
                        serialized,
                        payload_digest,
                        result_id,
                        memory_mutation_id,
                        now,
                        now,
                    ),
                )
            else:
                if operation_kind is not None and row["operation_kind"] != operation_kind:
                    conn.rollback()
                    raise BucketIdempotencyError("operation_payload_conflict")
                if target_bucket_id is not None and row["target_bucket_id"] != target_bucket_id:
                    conn.rollback()
                    raise BucketIdempotencyError("operation_payload_conflict")
                if payload_digest is not None and row["payload_digest"] != payload_digest:
                    conn.rollback()
                    raise BucketIdempotencyError("operation_payload_conflict")
                if memory_mutation_id is not None:
                    existing_mutation_id = row["memory_mutation_id"]
                    if (
                        existing_mutation_id is not None
                        and existing_mutation_id != memory_mutation_id
                    ):
                        conn.rollback()
                        raise BucketIdempotencyError("memory_mutation_conflict")
                    if existing_mutation_id is None:
                        conn.execute(
                            """
                            UPDATE ob_import_operations
                            SET memory_mutation_id = ?, updated_at = ?
                            WHERE operation_key = ?
                            """,
                            (memory_mutation_id, now, operation_key),
                        )
            conn.commit()
        result = self._get_import_operation(operation_key)
        if result is None:
            raise BucketIdempotencyError("operation_not_found")
        return result

    def _mark_import_operation_applied(self, operation_key: str) -> None:
        with sqlite3.connect(self.history_db_path) as conn:
            conn.execute(
                """
                UPDATE ob_import_operations SET status = 'applied', updated_at = ?
                WHERE operation_key = ?
                """,
                (now_iso(), operation_key),
            )

    @staticmethod
    def _operation_marker(post: frontmatter.Post, operation_key: str) -> dict[str, Any] | None:
        markers = post.get(_IMPORT_MARKER_FIELD, [])
        if markers in (None, ""):
            return None
        if not isinstance(markers, list):
            raise BucketIdempotencyError("operation_marker_invalid")
        for marker in markers:
            if not isinstance(marker, dict):
                raise BucketIdempotencyError("operation_marker_invalid")
            if marker.get("operation_key") == operation_key:
                return marker
        return None

    @classmethod
    def _append_operation_marker(
        cls,
        post: frontmatter.Post,
        *,
        operation_key: str,
        payload_digest: str,
        operation_kind: str,
        memory_mutation_id: str | None = None,
    ) -> bool:
        existing = cls._operation_marker(post, operation_key)
        if existing is not None:
            if existing.get("payload_digest") != payload_digest:
                raise BucketIdempotencyError("operation_payload_conflict")
            if (
                memory_mutation_id is not None
                and existing.get("memory_mutation_id") != memory_mutation_id
            ):
                raise BucketIdempotencyError("memory_mutation_conflict")
            return False
        markers = post.get(_IMPORT_MARKER_FIELD, [])
        if markers in (None, ""):
            markers = []
        if not isinstance(markers, list):
            raise BucketIdempotencyError("operation_marker_invalid")
        marker = {
            "operation_key": operation_key,
            "payload_digest": payload_digest,
            "operation_kind": operation_kind,
        }
        if memory_mutation_id is not None:
            marker["memory_mutation_id"] = memory_mutation_id
        markers.append(marker)
        post[_IMPORT_MARKER_FIELD] = markers
        return True

    @staticmethod
    def _write_post_atomic(file_path: str, post: frontmatter.Post) -> None:
        """Atomically publish an O5B-marked memory file."""

        parent = os.path.dirname(file_path)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{os.path.basename(file_path)}.",
            suffix=".tmp",
            dir=parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(frontmatter.dumps(post))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, file_path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @guarded_mutation("bucket_import_idempotency_plan")
    def plan_import_operation(
        self,
        operation_key: str,
        *,
        operation_kind: str,
        target_bucket_id: str | None = None,
        payload: dict[str, Any],
        memory_mutation_id: str | None = None,
    ) -> dict[str, Any]:
        """Durably plan an O5B operation before any memory file mutation."""

        return self._ensure_import_operation(
            operation_key,
            operation_kind=operation_kind,
            target_bucket_id=target_bucket_id,
            payload=payload,
            memory_mutation_id=memory_mutation_id,
        )

    @guarded_async_mutation("bucket_import_idempotency_apply")
    async def apply_import_operation(
        self,
        operation_key: str,
        *,
        operation_kind: str | None = None,
        target_bucket_id: str | None = None,
        payload: dict[str, Any] | None = None,
        payload_digest: str | None = None,
        memory_mutation_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply or replay one durable import memory operation."""

        operation = self._ensure_import_operation(
            operation_key,
            operation_kind=operation_kind,
            target_bucket_id=target_bucket_id,
            payload=payload,
            payload_digest=payload_digest,
            memory_mutation_id=memory_mutation_id,
        )
        stored_payload = operation["payload"]
        if operation["operation_kind"] == "create":
            bucket_id = await self.create(
                **stored_payload,
                _o5b_operation_key=operation_key,
                _o5b_payload_digest=operation["payload_digest"],
                _o5c_memory_mutation_id=operation.get("memory_mutation_id"),
            )
            return {"operation_key": operation_key, "result_id": bucket_id, "kind": "create"}

        bucket_id = operation["target_bucket_id"]
        if not bucket_id or not isinstance(stored_payload, dict):
            raise BucketIdempotencyError("operation_payload_invalid")
        update_kwargs = stored_payload.get("kwargs")
        if not isinstance(update_kwargs, dict):
            raise BucketIdempotencyError("operation_payload_invalid")
        applied = await self.update(
            bucket_id,
            **update_kwargs,
            _o5b_operation_key=operation_key,
            _o5b_payload_digest=operation["payload_digest"],
            _o5c_memory_mutation_id=operation.get("memory_mutation_id"),
        )
        if not applied:
            raise BucketIdempotencyError("target_bucket_missing")
        return {"operation_key": operation_key, "result_id": bucket_id, "kind": "update"}

    def inspect_import_operation(self, operation_key: str) -> dict[str, Any] | None:
        """Inspect an O5B operation and its hidden atomic marker read-only."""

        operation = self._get_import_operation(operation_key)
        if operation is None:
            return None
        result_id = operation.get("result_id")
        file_path = self._find_bucket_file(result_id) if result_id else None
        marker = None
        if file_path:
            try:
                post = frontmatter.load(file_path)
                marker = self._operation_marker(post, operation_key)
            except Exception as exc:
                raise BucketIdempotencyError("operation_marker_invalid") from exc
        return {
            **operation,
            "memory_exists": file_path is not None,
            "memory_path": file_path,
            "marker": marker,
        }

    @guarded_mutation("bucket_history_write")
    def record_history(self, bucket_id: str, old_content: str, change_type: str) -> None:
        """Persist the old content before a destructive content change."""
        with sqlite3.connect(self.history_db_path) as conn:
            conn.execute(
                """
                INSERT INTO bucket_history
                    (bucket_id, old_content, changed_at, change_type)
                VALUES (?, ?, ?, ?)
                """,
                (bucket_id, old_content or "", now_iso(), change_type),
            )

    @guarded_mutation("boot_delta_event_write")
    def _record_boot_delta_event(
        self,
        bucket_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Record a compact business change for the next successful boot."""
        serialized = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        with sqlite3.connect(self.history_db_path) as conn:
            conn.execute(
                """
                INSERT INTO boot_delta_events
                    (bucket_id, event_type, payload_json, occurred_at)
                VALUES (?, ?, ?, ?)
                """,
                (bucket_id, event_type, serialized, now_iso()),
            )

    def get_boot_delta_checkpoint(self, profile: str = "talk") -> dict[str, Any] | None:
        """Return one profile's last successful boot checkpoint."""
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT last_event_id, completed_at
                FROM boot_delta_profile_checkpoints
                WHERE profile = ?
                """,
                (profile,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_boot_delta_high_water(self) -> int:
        """Return the current boot-delta event high-water mark without writing."""
        with sqlite3.connect(self.history_db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM boot_delta_events"
            ).fetchone()
        return int(row[0] or 0)

    def get_boot_delta_events(
        self,
        after_event_id: int,
        through_event_id: int,
    ) -> list[dict[str, Any]]:
        """Read one stable boot-delta event range, oldest first."""
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, bucket_id, event_type, payload_json, occurred_at
                FROM boot_delta_events
                WHERE id > ? AND id <= ?
                ORDER BY id ASC
                """,
                (int(after_event_id), int(through_event_id)),
            ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            try:
                payload = json.loads(event.pop("payload_json"))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            event["payload"] = payload if isinstance(payload, dict) else {}
            events.append(event)
        return events

    @guarded_mutation("boot_delta_checkpoint_write")
    def advance_boot_delta_checkpoint(
        self,
        expected_event_id: int | None,
        next_event_id: int,
        profile: str = "talk",
    ) -> bool:
        """CAS-advance one profile checkpoint after its boot body completes."""
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT last_event_id
                FROM boot_delta_profile_checkpoints
                WHERE profile = ?
                """,
                (profile,),
            ).fetchone()
            current = int(row["last_event_id"]) if row is not None else None
            if current != expected_event_id:
                conn.rollback()
                return False
            if row is None:
                existing_count = conn.execute(
                    "SELECT COUNT(*) FROM boot_delta_profile_checkpoints"
                ).fetchone()[0]
                if existing_count == 0:
                    conn.executemany(
                        """
                        INSERT INTO boot_delta_profile_checkpoints
                            (profile, last_event_id, completed_at)
                        VALUES (?, ?, ?)
                        """,
                        [
                            (item, int(next_event_id), now_iso())
                            for item in _BOOT_DELTA_PROFILES
                        ],
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO boot_delta_profile_checkpoints
                            (profile, last_event_id, completed_at)
                        VALUES (?, ?, ?)
                        """,
                        (profile, int(next_event_id), now_iso()),
                    )
            else:
                conn.execute(
                    """
                    UPDATE boot_delta_profile_checkpoints
                    SET last_event_id = ?, completed_at = ?
                    WHERE profile = ?
                    """,
                    (int(next_event_id), now_iso(), profile),
                )
            conn.commit()
        return True

    def get_history(self, bucket_id: str, limit: int = 20) -> list[dict]:
        """Return recent write-ahead snapshots for manual recovery."""
        limit = max(1, min(int(limit or 20), 100))
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT bucket_id, old_content, changed_at, change_type
                FROM bucket_history
                WHERE bucket_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (bucket_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_history_for_bucket_ids(
        self, bucket_ids: Iterable[str]
    ) -> dict[str, list[dict]]:
        """Read ordered content snapshots for an in-memory historical corpus.

        This intentionally exposes only existing write-ahead rows.  It does
        not create tables, materialize snapshots, or otherwise mutate history.
        """
        ids = list(dict.fromkeys(
            str(bucket_id).strip() for bucket_id in bucket_ids if str(bucket_id).strip()
        ))
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT id, bucket_id, old_content, changed_at, change_type
                FROM bucket_history
                WHERE bucket_id IN ({placeholders})
                ORDER BY bucket_id ASC, changed_at ASC, id ASC
                """,
                ids,
            ).fetchall()
        history: dict[str, list[dict]] = {bucket_id: [] for bucket_id in ids}
        for row in rows:
            history.setdefault(str(row["bucket_id"]), []).append(dict(row))
        return history

    @guarded_mutation("bucket_letter_write")
    def record_letter(self, content: str, session_id: str, sealed: bool = False) -> None:
        """Persist an inter-window handoff letter outside normal memory buckets."""
        if not content or not content.strip():
            return
        with sqlite3.connect(self.history_db_path) as conn:
            conn.execute(
                """
                INSERT INTO letters (content, created_at, session_id, sealed)
                VALUES (?, ?, ?, ?)
                """,
                (content.strip(), now_iso(), session_id or "", 1 if sealed else 0),
            )

    def get_letters(self, limit: int = 1, include_sealed: bool = False) -> list[dict]:
        """Return latest handoff letters, newest first."""
        limit = max(1, min(int(limit or 1), 50))
        where = "" if include_sealed else "WHERE sealed = 0"
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT id, content, created_at, session_id, sealed
                FROM letters
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_letter(self, letter_id: int, include_sealed: bool = False) -> Optional[dict]:
        """Return one handoff letter by exact id, respecting sealed visibility."""
        try:
            letter_id = int(letter_id)
        except (TypeError, ValueError):
            return None
        if letter_id < 1:
            return None
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT id, content, created_at, session_id, sealed
                FROM letters
                WHERE id = ? AND (? OR sealed = 0)
                """,
                (letter_id, bool(include_sealed)),
            ).fetchone()
        return dict(row) if row else None

    @guarded_mutation("bucket_letter_seal")
    def seal_letter(self, letter_id: int, sealed: bool = True) -> bool:
        """Hide or unhide one handoff letter by id."""
        with sqlite3.connect(self.history_db_path) as conn:
            cur = conn.execute(
                "UPDATE letters SET sealed = ? WHERE id = ?",
                (1 if sealed else 0, int(letter_id)),
            )
            return cur.rowcount > 0

    @guarded_mutation("bucket_note_write")
    def record_note(
        self,
        text: str,
        *,
        author: str = "婷",
        via: str = "mcp",
        sealed: bool = False,
        open_at: str = "",
    ) -> int | None:
        """Persist one optional Ting note outside ordinary memory buckets."""
        if not isinstance(text, str) or not text.strip():
            return None
        if not isinstance(author, str) or not author.strip():
            return None
        if not isinstance(via, str) or not via.strip():
            return None
        normalized_open_at = open_at.strip() if isinstance(open_at, str) else ""
        with sqlite3.connect(self.history_db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO notes (created_at, author, via, text, sealed, open_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    now_iso(),
                    author.strip(),
                    via.strip(),
                    text,
                    1 if sealed else 0,
                    normalized_open_at or None,
                ),
            )
            return int(cur.lastrowid)

    @staticmethod
    def _note_visible_where(include_sealed: bool = False) -> str:
        sealed_clause = "" if include_sealed else "AND sealed = 0"
        return (
            "(open_at IS NULL OR open_at = '' OR open_at <= ?) "
            f"{sealed_clause}"
        )

    def list_notes(
        self,
        limit: int = 20,
        include_sealed: bool = False,
        *,
        available_at: str | None = None,
    ) -> list[dict]:
        """List visible Ting-note history, newest first, without exposing future notes."""
        limit = max(1, min(int(limit or 20), 100))
        available_at = available_at or now_iso()
        where = self._note_visible_where(include_sealed)
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT note_id, created_at, author, via, text, sealed, open_at,
                       boot_delivered_at, read_at, skipped_at, skipped_reason,
                       dismissed_at
                FROM notes
                WHERE {where}
                ORDER BY note_id DESC
                LIMIT ?
                """,
                (available_at, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_note(
        self,
        note_id: int,
        include_sealed: bool = False,
        *,
        available_at: str | None = None,
    ) -> Optional[dict]:
        """Read one visible Ting note while preserving sealed/future existence hiding."""
        try:
            note_id = int(note_id)
        except (TypeError, ValueError):
            return None
        if note_id < 1:
            return None
        available_at = available_at or now_iso()
        where = self._note_visible_where(include_sealed)
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"""
                SELECT note_id, created_at, author, via, text, sealed, open_at,
                       boot_delivered_at, read_at, skipped_at, skipped_reason,
                       dismissed_at
                FROM notes
                WHERE note_id = ? AND {where}
                """,
                (note_id, available_at),
            ).fetchone()
        return dict(row) if row else None

    def get_latest_visible_note(self, *, available_at: str | None = None) -> Optional[dict]:
        """Return the latest normally visible historical note for boot status text."""
        notes = self.list_notes(
            limit=1,
            include_sealed=False,
            available_at=available_at,
        )
        return notes[0] if notes else None

    def get_latest_note_delivery_candidate(
        self,
        *,
        available_at: str | None = None,
    ) -> Optional[dict]:
        """Return the newest visible note still eligible for one-time boot delivery."""
        available_at = available_at or now_iso()
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT note_id, created_at, author, via, text, sealed, open_at,
                       boot_delivered_at, read_at, skipped_at, skipped_reason,
                       dismissed_at
                FROM notes
                WHERE sealed = 0
                  AND dismissed_at IS NULL
                  AND (open_at IS NULL OR open_at = '' OR open_at <= ?)
                  AND boot_delivered_at IS NULL
                  AND skipped_at IS NULL
                ORDER BY note_id DESC
                LIMIT 1
                """,
                (available_at,),
            ).fetchone()
        return dict(row) if row else None

    @guarded_mutation("bucket_note_boot_delivery")
    def mark_note_boot_delivered(
        self,
        note_id: int,
        *,
        delivered_at: str | None = None,
    ) -> bool:
        """Deliver one newest eligible note and skip only older eligible predecessors."""
        try:
            note_id = int(note_id)
        except (TypeError, ValueError):
            return False
        delivered_at = delivered_at or now_iso()
        with sqlite3.connect(self.history_db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            candidate = conn.execute(
                """
                SELECT note_id
                FROM notes
                WHERE sealed = 0
                  AND dismissed_at IS NULL
                  AND (open_at IS NULL OR open_at = '' OR open_at <= ?)
                  AND boot_delivered_at IS NULL
                  AND skipped_at IS NULL
                ORDER BY note_id DESC
                LIMIT 1
                """,
                (delivered_at,),
            ).fetchone()
            if candidate is None or int(candidate["note_id"]) != note_id:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE notes SET boot_delivered_at = ? WHERE note_id = ?",
                (delivered_at, note_id),
            )
            conn.execute(
                """
                UPDATE notes
                SET skipped_at = ?,
                    skipped_reason = 'superseded_by_newer_eligible_note'
                WHERE note_id < ?
                  AND sealed = 0
                  AND dismissed_at IS NULL
                  AND (open_at IS NULL OR open_at = '' OR open_at <= ?)
                  AND boot_delivered_at IS NULL
                  AND skipped_at IS NULL
                """,
                (delivered_at, note_id, delivered_at),
            )
            conn.commit()
        return True

    @guarded_mutation("bucket_note_read")
    def mark_note_read(self, note_id: int, *, read_at: str | None = None) -> bool:
        """Record a successful full get_note read and suppress later automatic delivery."""
        try:
            note_id = int(note_id)
        except (TypeError, ValueError):
            return False
        read_at = read_at or now_iso()
        with sqlite3.connect(self.history_db_path) as conn:
            cur = conn.execute(
                """
                UPDATE notes
                SET read_at = COALESCE(read_at, ?),
                    boot_delivered_at = COALESCE(boot_delivered_at, ?)
                WHERE note_id = ?
                """,
                (read_at, read_at, note_id),
            )
            return cur.rowcount > 0

    # ---------------------------------------------------------
    # Create a new bucket
    # 创建新桶
    # Write content and metadata into a .md file
    # 将内容和元数据写入一个 .md 文件
    # ---------------------------------------------------------
    @guarded_async_mutation("bucket_create")
    async def create(
        self,
        content: str,
        tags: list[str] = None,
        importance: int = 5,
        domain: list[str] = None,
        valence: float = 0.5,
        arousal: float = 0.3,
        bucket_type: str = "dynamic",
        provenance_kind: str | None = None,
        name: str = None,
        pinned: bool = False,
        protected: bool = False,
        sealed: bool = False,
        topics: list[str] = None,
        todos: list[str] = None,
        todo_provenance: list[dict[str, Any]] | None = None,
        _o5b_operation_key: str | None = None,
        _o5b_payload_digest: str | None = None,
        _o5c_memory_mutation_id: str | None = None,
    ) -> str:
        """
        Create a new memory bucket, return bucket ID.
        创建一个新的记忆桶，返回桶 ID。

        pinned/protected=True: bucket won't be merged, decayed, or have importance changed.
        Importance is locked to 10 for pinned/protected buckets.
        pinned/protected 桶不参与合并与衰减，importance 强制锁定为 10。
        """
        canonical_todos = canonicalize_todos(todos)
        normalized_provenance_kind = (
            normalize_provenance_kind(provenance_kind, strict=True)
            if provenance_kind is not None
            else "unknown"
        )
        canonical_todo_provenance = reconcile_todo_provenance(
            canonical_todos,
            todo_provenance,
            strict=todo_provenance is not None,
        )
        operation = None
        if _o5b_operation_key is not None:
            operation_payload = {
                "content": content,
                "tags": tags or [],
                "importance": importance,
                "domain": domain,
                "valence": valence,
                "arousal": arousal,
                "name": name,
            }
            if todos is not None:
                operation_payload["todos"] = canonical_todos
            if provenance_kind is not None:
                operation_payload["provenance_kind"] = normalized_provenance_kind
            if todo_provenance is not None:
                operation_payload["todo_provenance"] = canonical_todo_provenance
            operation = self._ensure_import_operation(
                _o5b_operation_key,
                operation_kind="create",
                payload=operation_payload,
                payload_digest=_o5b_payload_digest,
                memory_mutation_id=_o5c_memory_mutation_id,
            )
            if operation["operation_kind"] != "create":
                raise BucketIdempotencyError("operation_kind_conflict")
            bucket_id = operation["result_id"]
        else:
            bucket_id = generate_bucket_id()
        tags = tags or []
        todos = canonical_todos
        todo_provenance = reconcile_todo_provenance(
            todos,
            canonical_todo_provenance,
        )
        bucket_name = sanitize_name(name) if name else bucket_id
        # feel buckets are allowed to have empty domain; others default to ["未分类"]
        if bucket_type == "feel":
            domain = domain if domain is not None else []
        else:
            domain = domain or ["未分类"]
        linked_content = content  # wikilink injection disabled; LLM adds [[]] via prompt

        # --- Pinned/protected buckets: lock importance to 10 ---
        # --- 钉选/保护桶：importance 强制锁定为 10 ---
        if pinned or protected:
            importance = 10

        # --- Build YAML frontmatter metadata / 构建元数据 ---
        today = _date_only()
        metadata = {
            "id": bucket_id,
            "name": bucket_name,
            "tags": tags,
            "domain": domain,
            "valence": max(0.0, min(1.0, valence)),
            "arousal": max(0.0, min(1.0, arousal)),
            "importance": max(1, min(10, importance)),
            "type": bucket_type,
            "created": now_iso(),
            "last_active": now_iso(),
            "created_at": today,
            "updated_at": today,
            "emotion_history": "[]",
            "related_buckets": "",
            "source_bucket": "",
            "trigger_date": "",
            "trigger_last_seen": "",
            "dormant": False,
            "sealed": 1 if sealed else 0,
            "activation_count": 0,
            "todos": todos,
        }
        if provenance_kind is not None:
            metadata["provenance_kind"] = normalized_provenance_kind
        if todo_provenance:
            metadata["todo_provenance"] = todo_provenance
        if pinned:
            metadata["pinned"] = True
        if protected:
            metadata["protected"] = True
        if topics is not None:
            metadata["topics"] = topics

        # --- Assemble Markdown file (frontmatter + body) ---
        # --- 组装 Markdown 文件 ---
        post = frontmatter.Post(linked_content, **metadata)
        if operation is not None:
            existing_path = self._find_bucket_file(bucket_id)
            if existing_path:
                try:
                    existing_post = frontmatter.load(existing_path)
                except Exception as exc:
                    raise BucketIdempotencyError("operation_marker_invalid") from exc
                marker = self._operation_marker(existing_post, _o5b_operation_key)
                if (
                    marker is None
                    or marker.get("payload_digest") != operation["payload_digest"]
                    or (
                        operation.get("memory_mutation_id") is not None
                        and marker.get("memory_mutation_id")
                        != operation["memory_mutation_id"]
                    )
                ):
                    raise BucketIdempotencyError("idempotency_conflict")
                self._mark_import_operation_applied(_o5b_operation_key)
                return bucket_id
            if operation["status"] == "applied":
                raise BucketIdempotencyError("target_bucket_missing")
            self._append_operation_marker(
                post,
                operation_key=_o5b_operation_key,
                payload_digest=operation["payload_digest"],
                operation_kind="create",
                memory_mutation_id=operation.get("memory_mutation_id"),
            )

        # --- Choose directory by type + primary domain ---
        # --- 按类型 + 主题域选择存储目录 ---
        if bucket_type == "permanent" or pinned:
            type_dir = self.permanent_dir
            if pinned and bucket_type != "permanent":
                metadata["type"] = "permanent"
        elif bucket_type == "feel":
            type_dir = self.feel_dir
        else:
            type_dir = self.dynamic_dir
        if bucket_type == "feel":
            primary_domain = "沉淀物"  # feel subfolder name
        else:
            primary_domain = sanitize_name(domain[0]) if domain else "未分类"
        target_dir = os.path.join(type_dir, primary_domain)
        os.makedirs(target_dir, exist_ok=True)

        # --- Filename: readable_name_bucketID.md (Obsidian friendly) ---
        # --- 文件名：可读名称_桶ID.md ---
        if bucket_name and bucket_name != bucket_id:
            filename = f"{bucket_name}_{bucket_id}.md"
        else:
            filename = f"{bucket_id}.md"
        file_path = safe_path(target_dir, filename)

        if sealed:
            try:
                await self._delete_ordinary_embedding(bucket_id)
            except Exception as exc:
                logger.error(
                    "Refusing sealed bucket create because ordinary-vector cleanup "
                    "failed for %s: %s",
                    bucket_id,
                    exc,
                )
                raise RuntimeError("sealed_embedding_cleanup_failed") from exc

        try:
            if operation is not None:
                self._write_post_atomic(file_path, post)
                self._mark_import_operation_applied(_o5b_operation_key)
            else:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(frontmatter.dumps(post))
        except OSError as e:
            logger.error(f"Failed to write bucket file / 写入桶文件失败: {file_path}: {e}")
            raise

        logger.info(
            f"Created bucket / 创建记忆桶: {bucket_id} ({bucket_name}) → {primary_domain}/"
            + (" [PINNED]" if pinned else "") + (" [PROTECTED]" if protected else "")
        )
        self._record_boot_delta_event(bucket_id, "created")
        if not sealed:
            await self._refresh_ordinary_embedding_best_effort(bucket_id, content)
        return bucket_id

    # ---------------------------------------------------------
    # Read bucket content
    # 读取桶内容
    # Returns {"id", "metadata", "content", "path"} or None
    # ---------------------------------------------------------
    async def get(self, bucket_id: str) -> Optional[dict]:
        """
        Read a single bucket by ID.
        根据 ID 读取单个桶。
        """
        if not bucket_id or not isinstance(bucket_id, str):
            return None
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return None
        return self._load_bucket(file_path)

    # ---------------------------------------------------------
    # Move bucket between directories
    # 在目录间移动桶文件
    # ---------------------------------------------------------
    @guarded_mutation("bucket_move")
    def _move_bucket(self, file_path: str, target_type_dir: str, domain: list[str] = None) -> str:
        """
        Move a bucket file to a new type directory, preserving domain subfolder.
        Returns new file path.
        """
        primary_domain = sanitize_name(domain[0]) if domain else "未分类"
        target_dir = os.path.join(target_type_dir, primary_domain)
        os.makedirs(target_dir, exist_ok=True)
        filename = os.path.basename(file_path)
        new_path = safe_path(target_dir, filename)
        if os.path.normpath(file_path) != os.path.normpath(new_path):
            os.rename(file_path, new_path)
            logger.info(f"Moved bucket / 移动记忆桶: {filename} → {target_dir}/")
        return new_path

    # ---------------------------------------------------------
    # Update bucket
    # 更新桶
    # Supports: content, tags, importance, valence, arousal, name, resolved
    # ---------------------------------------------------------
    @guarded_async_mutation("bucket_update")
    async def update(self, bucket_id: str, **kwargs) -> bool:
        """
        Update bucket content or metadata fields.
        更新桶的内容或元数据字段。
        """
        history_change_type = kwargs.pop("_history_change_type", "replace")
        o5b_operation_key = kwargs.pop("_o5b_operation_key", None)
        o5b_payload_digest = kwargs.pop("_o5b_payload_digest", None)
        o5c_memory_mutation_id = kwargs.pop("_o5c_memory_mutation_id", None)
        operation = None
        if o5b_operation_key is not None:
            operation = self._ensure_import_operation(
                o5b_operation_key,
                operation_kind="update",
                target_bucket_id=bucket_id,
                payload={"kwargs": dict(kwargs)},
                payload_digest=o5b_payload_digest,
                memory_mutation_id=o5c_memory_mutation_id,
            )
            if operation["operation_kind"] != "update":
                raise BucketIdempotencyError("operation_kind_conflict")
            bucket_id = operation["target_bucket_id"]
            stored_kwargs = operation["payload"].get("kwargs")
            if not isinstance(stored_kwargs, dict):
                raise BucketIdempotencyError("operation_payload_invalid")
            kwargs = stored_kwargs
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        try:
            post = frontmatter.load(file_path)
        except Exception as e:
            logger.warning(f"Failed to load bucket for update / 加载桶失败: {file_path}: {e}")
            return False

        previous_content = str(post.content or "")
        previous_todos = canonicalize_todos(post.get("todos"))
        previous_todo_provenance = reconcile_todo_provenance(
            previous_todos,
            post.get("todo_provenance"),
        )
        previous_superseded_by = post.get("superseded_by")
        explicit_provenance_kind = (
            normalize_provenance_kind(kwargs["provenance_kind"], strict=True)
            if "provenance_kind" in kwargs
            else None
        )

        if operation is not None:
            marker = self._operation_marker(post, o5b_operation_key)
            if marker is not None:
                if marker.get("payload_digest") != operation["payload_digest"]:
                    raise BucketIdempotencyError("operation_payload_conflict")
                if (
                    operation.get("memory_mutation_id") is not None
                    and marker.get("memory_mutation_id")
                    != operation["memory_mutation_id"]
                ):
                    raise BucketIdempotencyError("memory_mutation_conflict")
                self._mark_import_operation_applied(o5b_operation_key)
                return True
            if operation["status"] == "applied":
                raise BucketIdempotencyError("operation_marker_missing")
            self._append_operation_marker(
                post,
                operation_key=o5b_operation_key,
                payload_digest=operation["payload_digest"],
                operation_kind="update",
                memory_mutation_id=operation.get("memory_mutation_id"),
            )

        previous_sealed = _is_sealed_bucket(post)
        next_sealed = (
            int(kwargs["sealed"]) == 1
            if "sealed" in kwargs
            else previous_sealed
        )
        content_changed = "content" in kwargs
        cleanup_before_write = next_sealed and (not previous_sealed or content_changed)
        if cleanup_before_write:
            try:
                await self._delete_ordinary_embedding(bucket_id)
            except Exception as exc:
                logger.error(
                    "Refusing sealed bucket update because ordinary-vector cleanup "
                    "failed for %s: %s",
                    bucket_id,
                    exc,
                )
                return False

        # --- Pinned/protected buckets: lock importance to 10, ignore importance changes ---
        # --- 钉选/保护桶：importance 不可修改，强制保持 10 ---
        is_pinned = post.get("pinned", False) or post.get("protected", False)
        if is_pinned:
            kwargs.pop("importance", None)  # silently ignore importance update

        # --- Update only fields that were passed in / 只改传入的字段 ---
        if "content" in kwargs:
            try:
                self.record_history(bucket_id, post.content, history_change_type)
            except Exception as e:
                logger.error(
                    f"Refusing content update because history capture failed "
                    f"for {bucket_id}: {e}"
                )
                return False
            post.content = kwargs["content"]  # wikilink injection disabled; LLM adds [[]] via prompt
        # A body rewrite invalidates any previous provenance claim unless the
        # caller deliberately provides a replacement classification.
        if explicit_provenance_kind is not None:
            post["provenance_kind"] = explicit_provenance_kind
        elif content_changed:
            post["provenance_kind"] = "unknown"
        if "tags" in kwargs:
            post["tags"] = kwargs["tags"]
        if "todos" in kwargs or "todo_provenance" in kwargs:
            next_todos = (
                canonicalize_todos(kwargs["todos"])
                if "todos" in kwargs
                else previous_todos
            )
            raw_provenance = (
                kwargs["todo_provenance"]
                if "todo_provenance" in kwargs
                else previous_todo_provenance
            )
            try:
                next_todo_provenance = reconcile_todo_provenance(
                    next_todos,
                    raw_provenance,
                    strict="todo_provenance" in kwargs,
                )
            except ValueError as exc:
                logger.warning("Refusing invalid todo provenance update for %s: %s", bucket_id, exc)
                return False
            post["todos"] = next_todos
            if next_todo_provenance:
                post["todo_provenance"] = next_todo_provenance
            else:
                post.metadata.pop("todo_provenance", None)
        if "importance" in kwargs:
            post["importance"] = max(1, min(10, int(kwargs["importance"])))
        if "domain" in kwargs:
            post["domain"] = kwargs["domain"]
        if "valence" in kwargs:
            post["valence"] = max(0.0, min(1.0, float(kwargs["valence"])))
        if "arousal" in kwargs:
            post["arousal"] = max(0.0, min(1.0, float(kwargs["arousal"])))
        if "name" in kwargs:
            post["name"] = sanitize_name(kwargs["name"])
        if "resolved" in kwargs:
            post["resolved"] = bool(kwargs["resolved"])
        if "pinned" in kwargs:
            post["pinned"] = bool(kwargs["pinned"])
            if kwargs["pinned"]:
                post["importance"] = 10  # pinned → lock importance to 10
        if "digested" in kwargs:
            post["digested"] = bool(kwargs["digested"])
        if "model_valence" in kwargs:
            post["model_valence"] = max(0.0, min(1.0, float(kwargs["model_valence"])))
        if "emotion_history" in kwargs:
            post["emotion_history"] = kwargs["emotion_history"]
        if "related_buckets" in kwargs:
            post["related_buckets"] = kwargs["related_buckets"]
        if "source_bucket" in kwargs:
            post["source_bucket"] = kwargs["source_bucket"]
        if "trigger_date" in kwargs:
            post["trigger_date"] = kwargs["trigger_date"]
        if "trigger_last_seen" in kwargs:
            post["trigger_last_seen"] = kwargs["trigger_last_seen"]
        if "superseded_by" in kwargs:
            if kwargs["superseded_by"] is None:
                post.metadata.pop("superseded_by", None)
            else:
                post["superseded_by"] = str(kwargs["superseded_by"])
        if "superseded_at" in kwargs:
            if kwargs["superseded_at"] is None:
                post.metadata.pop("superseded_at", None)
            else:
                post["superseded_at"] = str(kwargs["superseded_at"])
        if "supersedes" in kwargs:
            if kwargs["supersedes"] is None:
                post.metadata.pop("supersedes", None)
            else:
                post["supersedes"] = list(
                    dict.fromkeys(
                        str(value).strip()
                        for value in kwargs["supersedes"]
                        if str(value).strip()
                    )
                )
        if "dormant" in kwargs:
            post["dormant"] = bool(kwargs["dormant"])
        if "sealed" in kwargs:
            post["sealed"] = 1 if int(kwargs["sealed"]) == 1 else 0

        # --- Auto-refresh activation time / 自动刷新激活时间 ---
        post["last_active"] = now_iso()
        post["updated_at"] = _date_only()

        try:
            if operation is not None or content_changed:
                self._write_post_atomic(file_path, post)
                if operation is not None:
                    self._mark_import_operation_applied(o5b_operation_key)
            else:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(frontmatter.dumps(post))
        except OSError as e:
            logger.error(f"Failed to write bucket update / 写入桶更新失败: {file_path}: {e}")
            return False

        # --- Auto-move: pinned → permanent/ ---
        # --- 自动移动：钉选 → permanent/ ---
        # NOTE: resolved buckets are NOT auto-archived here.
        # They stay in dynamic/ and decay naturally until score < threshold.
        # 注意：resolved 桶不在此自动归档，留在 dynamic/ 随衰减引擎自然归档。
        domain = post.get("domain", ["未分类"])
        if kwargs.get("pinned") and post.get("type") != "permanent":
            post["type"] = "permanent"
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
            self._move_bucket(file_path, self.permanent_dir, domain)

        if (
            not next_sealed
            and (previous_sealed or content_changed)
        ):
            await self._refresh_ordinary_embedding_best_effort(
                bucket_id,
                post.content,
            )

        if content_changed and str(post.content or "") != previous_content:
            self._record_boot_delta_event(bucket_id, "content_updated")
        current_todos = canonicalize_todos(post.get("todos"))
        # Boot deltas intentionally track todo text only.  Provenance-only
        # sidecar changes are metadata refinements and do not create a new
        # boot-delta event in Phase 5.10.
        if current_todos != previous_todos:
            self._record_boot_delta_event(
                bucket_id,
                "todos_updated",
                {
                    "closed_count": len(set(previous_todos) - set(current_todos)),
                    "opened_count": len(set(current_todos) - set(previous_todos)),
                },
            )
        current_superseded_by = post.get("superseded_by")
        if (
            current_superseded_by != previous_superseded_by
            and str(current_superseded_by or "").strip()
        ):
            self._record_boot_delta_event(
                bucket_id,
                "superseded",
                {"mode": str(current_superseded_by).strip()},
            )

        logger.info(f"Updated bucket / 更新记忆桶: {bucket_id}")
        return True

    @guarded_async_mutation("bucket_tg_summary_refresh")
    async def refresh_tg_summary(
        self,
        bucket_id: str,
        summary: str,
        source_sha256: str,
    ) -> tuple[str, str]:
        """Store a TG summary only when the source body still has the expected hash."""
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return "missing", ""
        try:
            post = frontmatter.load(file_path)
        except Exception as exc:
            logger.warning(
                "Failed to load bucket for TG summary refresh %s: %s",
                bucket_id,
                exc,
            )
            return "invalid", ""
        if _is_sealed_bucket(post):
            return "sealed", ""

        current_source_sha256 = hashlib.sha256(
            str(post.content or "").encode("utf-8")
        ).hexdigest()
        if current_source_sha256 != source_sha256:
            return "source_hash_mismatch", current_source_sha256

        post["tg_summary"] = summary
        post["tg_summary_source_hash"] = current_source_sha256
        post["tg_summary_updated_at"] = now_iso()
        post["last_active"] = now_iso()
        post["updated_at"] = _date_only()
        try:
            self._write_post_atomic(file_path, post)
        except OSError as exc:
            logger.error(
                "Failed to write TG summary refresh for %s: %s", bucket_id, exc
            )
            return "write_failed", current_source_sha256
        logger.info("Refreshed TG summary for bucket %s", bucket_id)
        return "updated", current_source_sha256

    # ---------------------------------------------------------
    # Wikilink injection — DISABLED
    # 自动添加 Obsidian 双链 — 已禁用
    # Now handled by LLM prompts (Gemini adds [[]] for proper nouns)
    # 现在由 LLM prompt 处理（Gemini 对人名/地名/专有名词加 [[]]）
    # ---------------------------------------------------------
    # def _apply_wikilinks(self, content, tags, domain, name): ...
    # def _collect_wikilink_keywords(self, content, tags, domain, name): ...
    # def _normalize_keywords(self, keywords): ...
    # def _extract_auto_keywords(self, content): ...

    # ---------------------------------------------------------
    # Delete bucket
    # 删除桶
    # ---------------------------------------------------------
    @guarded_async_mutation("bucket_delete")
    async def delete(
        self,
        bucket_id: str,
        *,
        _allow_sealed: bool = False,
        _dashboard_override: bool = False,
    ) -> bool:
        """
        Delete a memory bucket file.
        删除指定的记忆桶文件。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            try:
                await self._delete_ordinary_embedding(bucket_id)
            except Exception as exc:
                logger.error(
                    "Orphan ordinary-vector cleanup failed for missing bucket %s: %s",
                    bucket_id,
                    exc,
                )
            return False

        try:
            post = frontmatter.load(file_path)
            if not _dashboard_override and (
                (not _allow_sealed and _is_sealed_bucket(post))
                or post.get("pinned")
                or post.get("protected")
            ):
                logger.warning(
                    "Refusing destructive delete of protected bucket %s",
                    bucket_id,
                )
                return False
            self.record_history(bucket_id, post.content, "delete")
        except Exception as exc:
            logger.error(f"Failed to snapshot bucket {bucket_id}: {exc}")
            return False

        try:
            await self._delete_ordinary_embedding(bucket_id)
        except Exception as exc:
            logger.error(
                "Refusing to delete bucket %s because ordinary-vector cleanup failed: %s",
                bucket_id,
                exc,
            )
            return False

        try:
            os.remove(file_path)
        except OSError as exc:
            logger.error(
                "Bucket file removal failed after vector cleanup / 删除桶文件失败: %s: %s",
                file_path,
                exc,
            )
            return False

        logger.info(f"Deleted bucket / 删除记忆桶: {bucket_id}")
        return True

    # ---------------------------------------------------------
    # Touch bucket (refresh activation time + increment count)
    # 触碰桶（刷新激活时间 + 累加激活次数）
    # Called on every recall hit; affects decay score.
    # 每次检索命中时调用，影响衰减得分。
    # ---------------------------------------------------------
    @guarded_optional_async_mutation("bucket_touch")
    async def touch(
        self,
        bucket_id: str,
        ripple_ids: set[str] | None = None,
        wake_dormant: bool = False,
    ) -> None:
        """
        Update a bucket's last activation time and count. Wake it only when requested.
        Also triggers time ripple: nearby memories get a slight activation boost.
        更新桶的最后激活时间和激活次数；仅在显式请求时解除休眠。
        同时触发时间涟漪：时间上相邻的记忆轻微唤醒。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return

        try:
            post = frontmatter.load(file_path)
            post["last_active"] = now_iso()
            post["activation_count"] = post.get("activation_count", 0) + 1
            if wake_dormant:
                post["dormant"] = False

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))

            # --- Time ripple: boost nearby memories within ±48h ---
            # --- 时间涟漪：±48小时内的记忆轻微唤醒 ---
            current_time = datetime.fromisoformat(str(post.get("created", post.get("last_active", ""))))
            await self._time_ripple(
                bucket_id,
                current_time,
                allowed_ids=ripple_ids,
            )
        except Exception as e:
            logger.warning(f"Failed to touch bucket / 触碰桶失败: {bucket_id}: {e}")
            raise

    @guarded_async_mutation("bucket_dormant")
    async def set_dormant(self, bucket_id: str, dormant: bool = True) -> bool:
        """Set dormant without refreshing last_active or updated_at."""
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False
        try:
            post = frontmatter.load(file_path)
            post["dormant"] = bool(dormant)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
            return True
        except Exception as e:
            logger.warning(f"Failed to set dormant for {bucket_id}: {e}")
            return False

    @guarded_optional_async_mutation("bucket_time_ripple")
    async def _time_ripple(
        self,
        source_id: str,
        reference_time: datetime,
        hours: float = 48.0,
        allowed_ids: set[str] | None = None,
    ) -> None:
        """
        Slightly boost activation_count of buckets created/activated near the reference time.
        轻微提升时间相邻桶的激活次数（+0.3），不改 last_active 避免递归唤醒。
        Max 5 buckets rippled per touch to bound I/O.
        """
        all_buckets = await self.list_all(include_archive=False)

        rippled = 0
        max_ripple = 5
        for bucket in all_buckets:
            if rippled >= max_ripple:
                break
            if bucket["id"] == source_id:
                continue
            if allowed_ids is not None and bucket["id"] not in allowed_ids:
                continue
            meta = bucket.get("metadata", {})
            # Skip pinned/permanent/feel
            if meta.get("pinned") or meta.get("protected") or meta.get("type") in ("permanent", "feel"):
                continue

            created_str = meta.get("created", meta.get("last_active", ""))
            try:
                created = datetime.fromisoformat(str(created_str))
                delta_hours = abs((reference_time - created).total_seconds()) / 3600
            except (ValueError, TypeError):
                continue

            if delta_hours <= hours:
                # Boost activation_count by 0.3 (fractional), don't change last_active
                file_path = self._find_bucket_file(bucket["id"])
                if not file_path:
                    continue
                try:
                    post = frontmatter.load(file_path)
                    current_count = post.get("activation_count", 1)
                    # Store as float for fractional increments; calculate_score handles it
                    post["activation_count"] = round(current_count + 0.3, 1)
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(frontmatter.dumps(post))
                    rippled += 1
                except Exception:
                    logger.warning("Failed to persist time ripple for %s", bucket["id"])
                    raise

    # ---------------------------------------------------------
    # Multi-dimensional search (core feature)
    # 多维搜索（核心功能）
    #
    # Strategy: domain pre-filter → weighted multi-dim ranking
    # 策略：主题域预筛 → 多维加权精排
    #
    # Ranking formula:
    #   total = topic(×w_topic) + emotion(×w_emotion)
    #           + time(×w_time) + importance(×w_importance)
    #
    # Per-dimension scores (normalized to 0~1):
    #   topic     = rapidfuzz weighted match (name/tags/domain/body)
    #   emotion   = 1 - Euclidean distance (query v/a vs bucket v/a)
    #   time      = e^(-0.02 × days) (recent memories first)
    #   importance = importance / 10
    # ---------------------------------------------------------
    async def search(
        self,
        query: str,
        limit: int = None,
        domain_filter: list[str] = None,
        query_valence: float = None,
        query_arousal: float = None,
        include_dormant: bool = False,
        include_sealed: bool = False,
        candidate_buckets: list[dict] = None,
        trace: dict | None = None,
        include_semantic: bool = True,
    ) -> list[dict]:
        """
        Multi-dimensional indexed search for memory buckets.
        多维索引搜索记忆桶。

        domain_filter: pre-filter by domain (None = search all)
        query_valence/arousal: emotion coordinates for resonance scoring
        """
        if trace is not None:
            trace.clear()
            trace.update({
                "query": query,
                "candidate_source": (
                    "candidate_buckets"
                    if candidate_buckets is not None
                    else "active_buckets"
                ),
                "semantic": {
                    "enabled": bool(
                        self.embedding_engine
                        and getattr(self.embedding_engine, "enabled", False)
                    ),
                    "status": "not_run",
                },
                "candidates": [],
                "ranking": [],
            })

        if not query or not query.strip():
            if trace is not None:
                trace["status"] = "empty_query"
            return []

        limit = limit or self.max_results
        all_buckets = (
            list(candidate_buckets)
            if candidate_buckets is not None
            else await self.list_all(include_archive=False)
        )

        if not all_buckets:
            if trace is not None:
                trace["status"] = "no_candidates"
            return []

        trace_entries = {}
        if trace is not None:
            for bucket in all_buckets:
                bid = str(bucket.get("id", ""))
                trace_entries[bid] = {
                    "id": bid,
                    "candidate_origin": trace["candidate_source"],
                    "eligible": True,
                    "admitted": False,
                    "entered_ranking": False,
                }

        # --- Layer 1: domain pre-filter (fast scope reduction) ---
        # --- 第一层：主题域预筛（快速缩小范围）---
        if candidate_buckets is not None:
            candidates = all_buckets
        elif domain_filter:
            filter_set = {d.lower() for d in domain_filter}
            candidates = [
                b for b in all_buckets
                if {d.lower() for d in b["metadata"].get("domain", [])} & filter_set
            ]
            # Fall back to full search if pre-filter yields nothing
            # 预筛为空则回退全量搜索
            if not candidates:
                candidates = all_buckets
        else:
            candidates = all_buckets

        if trace is not None:
            candidate_ids = {str(bucket.get("id", "")) for bucket in candidates}
            for bid, entry in trace_entries.items():
                if bid not in candidate_ids:
                    entry["eligible"] = False
                    entry["exclusion_reasons"] = ["domain_filter"]

        if not include_dormant:
            before_dormant = candidates
            candidates = [
                b for b in candidates
                if not b.get("metadata", {}).get("dormant", False)
            ]
            if trace is not None:
                retained_ids = {str(bucket.get("id", "")) for bucket in candidates}
                for bucket in before_dormant:
                    bid = str(bucket.get("id", ""))
                    if bid not in retained_ids:
                        entry = trace_entries.get(bid)
                        if entry is not None:
                            entry["eligible"] = False
                            entry.setdefault("exclusion_reasons", []).append("dormant")

        if not include_sealed:
            before_sealed = candidates
            candidates = [
                bucket for bucket in candidates
                if not _is_sealed_bucket(bucket)
            ]
            if trace is not None:
                retained_ids = {str(bucket.get("id", "")) for bucket in candidates}
                for bucket in before_sealed:
                    bid = str(bucket.get("id", ""))
                    if bid not in retained_ids:
                        entry = trace_entries.get(bid)
                        if entry is not None:
                            entry["eligible"] = False
                            entry.setdefault("exclusion_reasons", []).append("sealed")

        # --- Layer 1.5: semantic recall for hybrid keyword/vector ranking ---
        # --- 第1.5层：语义召回，与关键词分数混合排序 ---
        vector_scores = {}
        semantic_before_error = ""
        if include_semantic and self.embedding_engine and self.embedding_engine.enabled:
            semantic_before_error = str(
                getattr(self.embedding_engine, "last_error", "") or ""
            )
            try:
                if candidate_buckets is None:
                    candidate_ids = None
                    if not include_sealed:
                        candidate_ids = {
                            str(bucket["id"])
                            for bucket in all_buckets
                            if not _is_sealed_bucket(bucket)
                        }
                    vector_results = await self.embedding_engine.search_similar(
                        query,
                        top_k=50,
                        **(
                            {"candidate_ids": candidate_ids}
                            if candidate_ids is not None
                            else {}
                        ),
                    )
                else:
                    candidate_ids = {str(bucket["id"]) for bucket in candidates}
                    vector_results = await self.embedding_engine.search_similar(
                        query,
                        top_k=50,
                        candidate_ids=candidate_ids,
                    )
                vector_scores = dict(vector_results)
            except Exception as e:
                logger.warning(f"Embedding pre-filter failed, using fuzzy only / embedding 预筛失败: {e}")

        if trace is not None:
            semantic_after_error = str(
                getattr(self.embedding_engine, "last_error", "") or ""
            ) if self.embedding_engine else ""
            if not include_semantic:
                trace["semantic"] = {
                    "enabled": False,
                    "status": "disabled_for_historical_corpus",
                }
            elif not self.embedding_engine or not self.embedding_engine.enabled:
                trace["semantic"] = {"enabled": False, "status": "disabled"}
            elif semantic_after_error and semantic_after_error != semantic_before_error:
                trace["semantic"] = {
                    "enabled": True,
                    "status": "provider_error",
                    "error_code": semantic_after_error,
                }
            elif vector_scores:
                trace["semantic"] = {
                    "enabled": True,
                    "status": "available",
                    "matched_count": len(vector_scores),
                }
            else:
                trace["semantic"] = {
                    "enabled": True,
                    "status": "unavailable_or_empty_index",
                }

        # --- Layer 2: weighted multi-dim ranking ---
        # --- 第二层：多维加权精排 ---
        scored = []
        for bucket in candidates:
            meta = bucket.get("metadata", {})

            try:
                # Dim 1: topic relevance (fuzzy text, 0~1)
                topic_score = self._calc_topic_score(query, bucket)
                exact_score = self._calc_exact_match_score(query, bucket)
                semantic_score = max(
                    0.0,
                    min(1.0, float(vector_scores.get(bucket["id"], 0.0))),
                )

                # Dim 2: emotion resonance (coordinate distance, 0~1)
                emotion_score = self._calc_emotion_score(
                    query_valence, query_arousal, meta
                )

                # Dim 3: time proximity (exponential decay, 0~1)
                time_score = self._calc_time_score(meta)

                # Dim 4: importance (direct normalization)
                importance_score = max(1, min(10, int(meta.get("importance", 5)))) / 10.0

                # --- Weighted sum / 加权求和 ---
                total = (
                    topic_score * self.w_topic
                    + emotion_score * self.w_emotion
                    + time_score * self.w_time
                    + importance_score * self.w_importance
                )
                # Normalize to 0~100 for readability
                weight_sum = self.w_topic + self.w_emotion + self.w_time + self.w_importance
                normalized = (total / weight_sum) * 100 if weight_sum > 0 else 0

                trace_entry = (
                    trace_entries.get(str(bucket.get("id", "")))
                    if trace is not None
                    else None
                )
                if trace_entry is not None:
                    trace_entry["scores"] = {
                        "fuzzy_lexical": round(topic_score, 4),
                        "exact_match": round(exact_score, 4),
                        "emotion": round(emotion_score, 4),
                        "time": round(time_score, 4),
                        "importance": round(importance_score, 4),
                        "semantic": round(semantic_score, 4),
                    }
                    trace_entry["pre_penalty_score"] = round(normalized, 2)
                    trace_entry["semantic_threshold"] = semantic_score >= 0.42
                    trace_entry["threshold"] = self.fuzzy_threshold

                # Threshold check uses raw (pre-penalty) score so resolved buckets
                # 阈值用原始分数判定，确保 resolved 桶在关键词命中时仍可被搜出
                # remain reachable by keyword (penalty applied only to ranking).
                if normalized >= self.fuzzy_threshold or semantic_score >= 0.42:
                    # Resolved buckets get ranking penalty (but still reachable by keyword)
                    # 已解决的桶仅在排序时降权
                    hybrid_score = normalized
                    if semantic_score:
                        hybrid_score = (
                            normalized * 0.65 + semantic_score * 100 * 0.35
                        )
                    if meta.get("resolved", False):
                        hybrid_score *= 0.3
                    superseded_factor = (
                        0.1 if str(meta.get("superseded_by", "") or "").strip() else 1.0
                    )
                    hybrid_score *= superseded_factor
                    if trace_entry is not None:
                        trace_entry["admitted"] = True
                        trace_entry["resolved"] = bool(meta.get("resolved", False))
                        trace_entry["ranking_penalty"] = (
                            0.3 if meta.get("resolved", False) else 1.0
                        )
                        trace_entry["superseded_factor"] = superseded_factor
                        trace_entry["final_ranking_score"] = round(hybrid_score, 2)
                        trace_entry["match_tier"] = (
                            3 if exact_score >= 0.95
                            else 2 if exact_score > 0
                            else 1
                        )
                    bucket["score"] = round(hybrid_score, 2)
                    bucket["semantic_score"] = round(semantic_score, 4)
                    bucket["vector_match"] = semantic_score >= 0.42 and not exact_score
                    if exact_score >= 0.95:
                        bucket["match_tier"] = 3
                    elif exact_score > 0:
                        bucket["match_tier"] = 2
                    else:
                        bucket["match_tier"] = 1
                    scored.append(bucket)
                elif trace_entry is not None:
                    trace_entry["exclusion_reasons"] = ["threshold"]
            except Exception as e:
                logger.warning(
                    f"Scoring failed for bucket {bucket.get('id', '?')} / "
                    f"桶评分失败: {e}"
                )
                continue

        scored.sort(
            key=lambda x: (x.get("match_tier", 0), x["score"]),
            reverse=True,
        )
        if trace is not None:
            for rank, bucket in enumerate(scored, start=1):
                trace_entry = trace_entries.get(str(bucket.get("id", "")))
                if trace_entry is not None:
                    trace_entry["entered_ranking"] = True
                    trace_entry["rank"] = rank
            trace["status"] = "ok"
            trace["candidate_count"] = len(all_buckets)
            trace["eligible_count"] = len(candidates)
            trace["admitted_count"] = len(scored)
            trace["candidates"] = list(trace_entries.values())
            trace["ranking"] = [str(bucket.get("id", "")) for bucket in scored[:limit]]
        return scored[:limit]

    # ---------------------------------------------------------
    # Topic relevance sub-score:
    # name(×3) + domain(×2.5) + tags(×2) + body(×1)
    # 文本相关性子分：桶名(×3) + 主题域(×2.5) + 标签(×2) + 正文(×1)
    # ---------------------------------------------------------
    def _calc_topic_score(self, query: str, bucket: dict) -> float:
        """
        Calculate text dimension relevance score (0~1).
        计算文本维度的相关性得分。
        """
        exact_score = self._calc_exact_match_score(query, bucket)
        if exact_score:
            return exact_score

        meta = bucket.get("metadata", {})
        query_text = self._normalize_search_text(query)
        name_score = fuzz.partial_ratio(
            query_text, self._normalize_search_text(meta.get("name", ""))
        ) / 100
        domains = meta.get("domain", [])
        if isinstance(domains, str):
            domains = [domains]
        domain_score = max(
            (
                fuzz.partial_ratio(query_text, self._normalize_search_text(domain)) / 100
                for domain in domains
            ),
            default=0,
        )
        keyword_score = max(
            (
                fuzz.ratio(query_text, self._normalize_search_text(keyword)) / 100
                for keyword in self._metadata_keywords(meta)
            ),
            default=0,
        )
        body = " ".join([
            str(meta.get("summary", "")),
            str(bucket.get("content", "")[:2000]),
        ])
        content_score = fuzz.partial_ratio(
            query_text, self._normalize_search_text(body)
        ) / 100

        fuzzy = (
            name_score * 0.30
            + domain_score * 0.20
            + keyword_score * 0.30
            + content_score * 0.20
        )
        return min(0.69, fuzzy)

    @staticmethod
    def _normalize_search_text(value) -> str:
        """Normalize without tokenizing, preserving one-character Chinese names."""
        return "".join(str(value or "").casefold().split())

    def _metadata_keywords(self, meta: dict) -> list[str]:
        keywords = meta.get("keywords", [])
        tags = meta.get("tags", [])
        if isinstance(keywords, str):
            keywords = [part.strip() for part in keywords.split(",") if part.strip()]
        if isinstance(tags, str):
            tags = [part.strip() for part in tags.split(",") if part.strip()]
        return [
            str(item)
            for item in list(keywords or []) + list(tags or [])
            if str(item).strip()
        ]

    def _calc_exact_match_score(self, query: str, bucket: dict) -> float:
        """Exact keywords rank highest; content/summary matches rank second."""
        query_text = self._normalize_search_text(query)
        if not query_text:
            return 0.0
        meta = bucket.get("metadata", {})
        keywords = [
            self._normalize_search_text(item)
            for item in self._metadata_keywords(meta)
        ]
        if query_text in keywords:
            return 1.0
        if any(query_text in keyword for keyword in keywords):
            return 0.95

        searchable = " ".join([
            str(meta.get("name", "")),
            str(meta.get("summary", "")),
            str(bucket.get("content", "")),
        ])
        if query_text in self._normalize_search_text(searchable):
            return 0.85
        return 0.0

    # ---------------------------------------------------------
    # Emotion resonance sub-score:
    # Based on Russell circumplex Euclidean distance
    # 情感共鸣子分：基于环形情感模型的欧氏距离
    # No emotion in query → neutral 0.5 (doesn't affect ranking)
    # ---------------------------------------------------------
    def _calc_emotion_score(
        self, q_valence: float, q_arousal: float, meta: dict
    ) -> float:
        """
        Calculate emotion resonance score (0~1, closer = higher).
        计算情感共鸣度（0~1，越近越高）。
        """
        if q_valence is None or q_arousal is None:
            return 0.5  # No emotion coordinates → neutral / 无情感坐标时给中性分

        try:
            b_valence = float(meta.get("valence", 0.5))
            b_arousal = float(meta.get("arousal", 0.3))
        except (ValueError, TypeError):
            return 0.5

        # Euclidean distance, max sqrt(2) ≈ 1.414
        dist = math.sqrt((q_valence - b_valence) ** 2 + (q_arousal - b_arousal) ** 2)
        return max(0.0, 1.0 - dist / 1.414)

    # ---------------------------------------------------------
    # Time proximity sub-score:
    # More recent activation → higher score
    # 时间亲近子分：距上次激活越近分越高
    # ---------------------------------------------------------
    def _calc_time_score(self, meta: dict) -> float:
        """
        Calculate time proximity score (0~1, more recent = higher).
        计算时间亲近度。
        """
        last_active_str = meta.get("last_active", meta.get("created", ""))
        try:
            last_active = datetime.fromisoformat(str(last_active_str))
            days = max(0.0, (datetime.now() - last_active).total_seconds() / 86400)
        except (ValueError, TypeError):
            days = 30
        return math.exp(-0.02 * days)

    # ---------------------------------------------------------
    # List all buckets
    # 列出所有桶
    # ---------------------------------------------------------
    async def list_all(self, include_archive: bool = False) -> list[dict]:
        """
        Recursively walk directories (including domain subdirs), list all buckets.
        递归遍历目录（含域子目录），列出所有记忆桶。
        """
        buckets = []

        dirs = [self.permanent_dir, self.dynamic_dir, self.feel_dir]
        if include_archive:
            dirs.append(self.archive_dir)

        for dir_path in dirs:
            if not os.path.exists(dir_path):
                continue
            for root, _, files in os.walk(dir_path):
                for filename in files:
                    if not filename.endswith(".md"):
                        continue
                    file_path = os.path.join(root, filename)
                    bucket = self._load_bucket(file_path)
                    if bucket:
                        buckets.append(bucket)

        return buckets

    # ---------------------------------------------------------
    # Statistics (counts per category + total size)
    # 统计信息（各分类桶数量 + 总体积）
    # ---------------------------------------------------------
    async def get_stats(self) -> dict:
        """
        Return memory bucket statistics (including domain subdirs).
        返回记忆桶的统计数据。
        """
        stats = {
            "permanent_count": 0,
            "dynamic_count": 0,
            "archive_count": 0,
            "feel_count": 0,
            "total_size_kb": 0.0,
            "domains": {},
        }

        for subdir, key in [
            (self.permanent_dir, "permanent_count"),
            (self.dynamic_dir, "dynamic_count"),
            (self.archive_dir, "archive_count"),
            (self.feel_dir, "feel_count"),
        ]:
            if not os.path.exists(subdir):
                continue
            for root, _, files in os.walk(subdir):
                for f in files:
                    if f.endswith(".md"):
                        stats[key] += 1
                        fpath = os.path.join(root, f)
                        try:
                            stats["total_size_kb"] += os.path.getsize(fpath) / 1024
                        except OSError:
                            pass
                        # Per-domain counts / 每个域的桶数量
                        domain_name = os.path.basename(root)
                        if domain_name != os.path.basename(subdir):
                            stats["domains"][domain_name] = stats["domains"].get(domain_name, 0) + 1

        return stats

    # ---------------------------------------------------------
    # Archive bucket (move from permanent/dynamic into archive)
    # 归档桶（从 permanent/dynamic 移入 archive）
    # Called by decay engine to simulate "forgetting"
    # 由衰减引擎调用，模拟"遗忘"
    # ---------------------------------------------------------
    @guarded_async_mutation("bucket_archive")
    async def archive(self, bucket_id: str) -> bool:
        """
        Move a bucket into the archive directory (preserving domain subdirs).
        将指定桶移入归档目录（保留域子目录结构）。
        """
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        try:
            # Read once, get domain info and update type / 一次性读取
            post = frontmatter.load(file_path)
            domain = post.get("domain", ["未分类"])
            primary_domain = sanitize_name(domain[0]) if domain else "未分类"
            archive_subdir = os.path.join(self.archive_dir, primary_domain)
            os.makedirs(archive_subdir, exist_ok=True)

            dest = safe_path(archive_subdir, os.path.basename(file_path))

            # Update type marker then move file / 更新类型标记后移动文件
            post["type"] = "archived"
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))

            # Use shutil.move for cross-filesystem safety
            # 使用 shutil.move 保证跨文件系统安全
            shutil.move(file_path, str(dest))
        except Exception as e:
            logger.error(
                f"Failed to archive bucket / 归档桶失败: {bucket_id}: {e}"
            )
            return False

        logger.info(f"Archived bucket / 归档记忆桶: {bucket_id} → archive/{primary_domain}/")
        return True

    # ---------------------------------------------------------
    # Internal: find bucket file across all three directories
    # 内部：在三个目录中查找桶文件
    # ---------------------------------------------------------
    def _find_bucket_file(self, bucket_id: str) -> Optional[str]:
        """
        Recursively search permanent/dynamic/archive for a bucket file
        matching the given ID.
        在 permanent/dynamic/archive 中递归查找指定 ID 的桶文件。
        """
        if not bucket_id:
            return None
        for dir_path in [self.permanent_dir, self.dynamic_dir, self.archive_dir, self.feel_dir]:
            if not os.path.exists(dir_path):
                continue
            for root, _, files in os.walk(dir_path):
                for fname in files:
                    if not fname.endswith(".md"):
                        continue
                    # Match by exact ID segment in filename
                    # 通过文件名中的 ID 片段精确匹配
                    name_part = fname[:-3]  # remove .md
                    if name_part == bucket_id or name_part.endswith(f"_{bucket_id}"):
                        return os.path.join(root, fname)
        return None

    # ---------------------------------------------------------
    # Internal: load bucket data from .md file
    # 内部：从 .md 文件加载桶数据
    # ---------------------------------------------------------
    def _load_bucket(self, file_path: str) -> Optional[dict]:
        """
        Parse a Markdown file and return structured bucket data.
        解析 Markdown 文件，返回桶的结构化数据。
        """
        try:
            post = frontmatter.load(file_path)
            metadata = dict(post.metadata)
            # O5B operation markers are durable write-control metadata only;
            # never expose them through ordinary memory reads/search results.
            metadata.pop(_IMPORT_MARKER_FIELD, None)
            metadata.setdefault("created_at", _date_only(metadata.get("created")))
            metadata.setdefault(
                "updated_at",
                _date_only(metadata.get("updated_at") or metadata.get("last_active") or metadata.get("created")),
            )
            metadata.setdefault("dormant", False)
            metadata.setdefault("sealed", 0)
            metadata.setdefault("source_bucket", "")
            metadata.setdefault("trigger_date", "")
            metadata.setdefault("trigger_last_seen", "")
            metadata["provenance_kind"] = normalize_provenance_kind(
                metadata.get("provenance_kind")
            )
            metadata["sealed"] = 1 if int(metadata.get("sealed", 0) or 0) == 1 else 0
            return {
                "id": post.get("id", Path(file_path).stem),
                "metadata": metadata,
                "content": post.content,
                "path": file_path,
            }
        except Exception as e:
            logger.warning(
                f"Failed to load bucket file / 加载桶文件失败: {file_path}: {e}"
            )
            return None
