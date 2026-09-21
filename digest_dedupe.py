"""Pure local embedding duplicate scan used by the digest MCP tool."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import numpy as np
import frontmatter
import yaml

from utils import strip_wikilinks


def _read_frontmatter_fields(
    file_path: str,
    wanted_fields: set[str],
) -> dict[str, object] | None:
    """Read selected top-level YAML scalars without reading a bucket body."""
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            if handle.readline().strip() != "---":
                return None
            fields: dict[str, object] = {}
            for raw_line in handle:
                if raw_line.strip() in ("---", "..."):
                    return fields
                key, separator, raw_value = raw_line.partition(":")
                if (
                    not separator
                    or key != key.lstrip()
                    or key not in wanted_fields
                ):
                    continue
                try:
                    fields[key] = yaml.safe_load(raw_value)
                except yaml.YAMLError:
                    fields[key] = raw_value.strip()
    except OSError:
        return None
    return None


def _is_sealed(value: object) -> bool:
    """Fail closed when a frontmatter sealed value cannot be interpreted."""
    try:
        return int(value or 0) == 1
    except (TypeError, ValueError):
        return True


def _is_dormant(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _display_label(value: object, fallback: str, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] if text else fallback


def _read_unsealed_body(file_path: str) -> str | None:
    """Read the body exactly as Breath does after its initial sealed check passed."""
    try:
        return str(frontmatter.load(file_path).content or "")
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        return None


def _read_cached_summaries(
    cache_db_path: str,
    content_hashes: set[str],
) -> dict[str, str]:
    """Read cached dehydration summaries without creating or changing the DB."""
    database_path = Path(cache_db_path)
    if not content_hashes or not database_path.is_file():
        return {}
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(database_uri, uri=True) as conn:
            conn.execute("PRAGMA query_only = ON")
            summaries: dict[str, str] = {}
            hashes = sorted(content_hashes)
            for start in range(0, len(hashes), 900):
                chunk = hashes[start:start + 900]
                placeholders = ", ".join("?" for _ in chunk)
                rows = conn.execute(
                    "SELECT content_hash, summary FROM dehydration_cache "
                    f"WHERE content_hash IN ({placeholders})",
                    chunk,
                ).fetchall()
                for content_hash, summary in rows:
                    if isinstance(summary, str) and summary.strip():
                        summaries[str(content_hash)] = summary
            return summaries
    except (OSError, sqlite3.Error):
        return {}


def _is_sealed_for_output(file_path: str) -> bool:
    """Recheck sealed state immediately before rendering; unreadable fails closed."""
    access = _read_frontmatter_fields(file_path, {"sealed"})
    return access is None or _is_sealed(access.get("sealed", 0))


def _bucket_metadata_index(
    bucket_roots: tuple[str, ...],
    *,
    include_display: bool = True,
) -> tuple[dict[str, dict], dict[str, int]]:
    """Index access frontmatter and, when allowed, unsealed display data."""
    records: dict[str, dict] = {}
    counts = {"buckets": 0, "sealed": 0, "metadata_unreadable": 0}
    for base_dir in bucket_roots:
        if not os.path.exists(base_dir):
            continue
        for root, _, filenames in os.walk(base_dir):
            for filename in filenames:
                if not filename.endswith(".md"):
                    continue
                file_path = os.path.join(root, filename)
                fallback_id = Path(file_path).stem
                counts["buckets"] += 1
                access = _read_frontmatter_fields(file_path, {"id", "sealed"})
                if access is None:
                    counts["sealed"] += 1
                    counts["metadata_unreadable"] += 1
                    records[fallback_id] = {"sealed": True}
                    continue

                bucket_id = str(access.get("id") or fallback_id)
                if _is_sealed(access.get("sealed", 0)):
                    counts["sealed"] += 1
                    records[bucket_id] = {"sealed": True}
                    continue

                if not include_display:
                    records[bucket_id] = {"sealed": False}
                    continue

                display = _read_frontmatter_fields(
                    file_path,
                    {"name", "dormant"},
                ) or {}
                name = _display_label(display.get("name"), bucket_id)
                body = _read_unsealed_body(file_path)
                cleaned_body = strip_wikilinks(body or "")
                has_body = bool(cleaned_body.strip())
                records[bucket_id] = {
                    "sealed": False,
                    "file_path": file_path,
                    "name": name,
                    "body": cleaned_body if has_body else "",
                    "content_hash": (
                        hashlib.sha256(cleaned_body.encode("utf-8")).hexdigest()
                        if has_body
                        else ""
                    ),
                    "dormant": _is_dormant(display.get("dormant", False)),
                }
    return records, counts


def _attach_summary_sources(
    bucket_records: dict[str, dict],
    cache_db_path: str,
) -> dict[str, int]:
    """Apply cache, body, then name fallback without calling any model or API."""
    cached_summaries = _read_cached_summaries(
        cache_db_path,
        {
            record["content_hash"]
            for record in bucket_records.values()
            if not record.get("sealed", True) and record.get("content_hash")
        },
    )
    counts = {"summary": 0, "body": 0, "name": 0}
    for record in bucket_records.values():
        if record.get("sealed", True):
            continue
        content_hash = record.get("content_hash", "")
        cached_summary = cached_summaries.get(content_hash)
        if cached_summary:
            record["summary"] = _display_label(cached_summary, record["name"], limit=120)
            record["summary_source"] = "summary"
        elif record.get("body"):
            record["summary"] = _display_label(record["body"], record["name"], limit=120)
            record["summary_source"] = "body"
        else:
            record["summary"] = record["name"]
            record["summary_source"] = "name"
        counts[record["summary_source"]] += 1
    return counts


def _read_embedding_rows(db_path: str, model: str) -> tuple[list[tuple[str, str]], list[tuple[str, int]]]:
    """Read vectors through SQLite read-only mode and enumerate model counts."""
    database_path = Path(db_path)
    if not database_path.is_file():
        raise RuntimeError("embedding_database_missing")
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as conn:
        conn.execute("PRAGMA query_only = ON")
        model_counts = [
            (str(stored_model or ""), int(count))
            for stored_model, count in conn.execute(
                "SELECT model, COUNT(*) FROM embeddings GROUP BY model ORDER BY model"
            ).fetchall()
        ]
        rows = [
            (str(bucket_id), str(embedding_json))
            for bucket_id, embedding_json in conn.execute(
                "SELECT bucket_id, embedding FROM embeddings WHERE model = ?",
                (model,),
            ).fetchall()
        ]
    return rows, model_counts


def run_dedupe_scan(
    *,
    bucket_roots: tuple[str, ...],
    excluded_archive_roots: tuple[str, ...] = (),
    db_path: str,
    model: str,
    limit: int = 30,
) -> str:
    """Return a local-only duplicate report. It performs no bucket or DB mutation."""
    try:
        limit = max(0, min(int(limit), 500))
    except (TypeError, ValueError):
        return "limit 必须是整数。"

    bucket_records, bucket_counts = _bucket_metadata_index(bucket_roots)
    excluded_archive_records, excluded_archive_counts = _bucket_metadata_index(
        excluded_archive_roots,
        include_display=False,
    )
    embedding_rows, model_counts = _read_embedding_rows(db_path, model)
    summary_counts = _attach_summary_sources(
        bucket_records,
        str(Path(db_path).with_name("dehydration_cache.db")),
    )

    orphan_rows = 0
    sealed_vector_rows = 0
    invalid_vector_rows = 0
    usable_entries: list[tuple[str, np.ndarray]] = []
    for bucket_id, embedding_json in embedding_rows:
        record = bucket_records.get(bucket_id)
        if record is None:
            if bucket_id in excluded_archive_records:
                continue
            orphan_rows += 1
            continue
        if record.get("sealed", True):
            sealed_vector_rows += 1
            continue
        try:
            vector = np.asarray(json.loads(embedding_json), dtype=np.float64)
        except (TypeError, ValueError, json.JSONDecodeError):
            invalid_vector_rows += 1
            continue
        if vector.ndim != 1 or vector.size == 0 or not np.isfinite(vector).all():
            invalid_vector_rows += 1
            continue
        usable_entries.append((bucket_id, vector))

    by_dimension: dict[int, list[int]] = {}
    for index, (_, vector) in enumerate(usable_entries):
        by_dimension.setdefault(int(vector.size), []).append(index)

    pair_left: list[np.ndarray] = []
    pair_right: list[np.ndarray] = []
    pair_scores: list[np.ndarray] = []
    for indexes in by_dimension.values():
        if len(indexes) < 2:
            continue
        matrix = np.vstack([usable_entries[index][1] for index in indexes])
        norms = np.linalg.norm(matrix, axis=1)
        nonzero = norms > 0
        if nonzero.sum() < 2:
            continue
        local_indexes = np.asarray(indexes, dtype=np.int64)[nonzero]
        normalized = matrix[nonzero] / norms[nonzero, np.newaxis]
        similarity = np.clip(normalized @ normalized.T, -1.0, 1.0)
        left, right = np.triu_indices(len(local_indexes), k=1)
        pair_left.append(local_indexes[left])
        pair_right.append(local_indexes[right])
        pair_scores.append(similarity[left, right])

    scores = np.concatenate(pair_scores) if pair_scores else np.asarray([], dtype=np.float64)
    left_indexes = np.concatenate(pair_left) if pair_left else np.asarray([], dtype=np.int64)
    right_indexes = np.concatenate(pair_right) if pair_right else np.asarray([], dtype=np.int64)
    distribution = (
        ("0.95+", int(np.count_nonzero(scores >= 0.95))),
        ("0.90-0.95", int(np.count_nonzero((scores >= 0.90) & (scores < 0.95)))),
        ("0.85-0.90", int(np.count_nonzero((scores >= 0.85) & (scores < 0.90)))),
        ("0.80-0.85", int(np.count_nonzero((scores >= 0.80) & (scores < 0.85)))),
        ("0.75-0.80", int(np.count_nonzero((scores >= 0.75) & (scores < 0.80)))),
    )
    unnamed_bucket_ids = sorted(
        bucket_id
        for bucket_id, record in bucket_records.items()
        if not record.get("sealed", True) and record.get("name") == bucket_id
    )

    lines = [
        "=== digest embedding 查重（只读）===",
        f"当前模型: {model}",
        f"向量: N={len(usable_entries)}（当前模型行={len(embedding_rows)}，sealed 跳过={sealed_vector_rows}，无效跳过={invalid_vector_rows}）",
        f"桶: M={bucket_counts['buckets']}（sealed={bucket_counts['sealed']}，元数据不可读={bucket_counts['metadata_unreadable']}）",
        f"归档桶排除: {excluded_archive_counts['buckets']}",
        f"差额: K=M-N={bucket_counts['buckets'] - len(usable_entries)}",
        f"孤儿向量行: {orphan_rows}",
        f"未命名桶（name=bucket_id）: {len(unnamed_bucket_ids)}",
        "未命名桶 ID 清单:",
    ]
    lines.extend(f"- {bucket_id}" for bucket_id in unnamed_bucket_ids)
    if not unnamed_bucket_ids:
        lines.append("- 无")
    lines.extend([
        (
            "摘要来源: "
            f"缓存命中={summary_counts['summary']}，"
            f"正文回退={summary_counts['body']}，"
            f"名称回退={summary_counts['name']}"
        ),
        "embeddings 表 model 分布:",
    ])
    lines.extend(f"- {stored_model or '(empty)'}: {count}" for stored_model, count in model_counts)
    lines.append("相似度分布（同维、有效、非 sealed 向量对）:")
    lines.extend(f"- {label}: {count}" for label, count in distribution)
    lines.append(f"成对清单（按相似度降序，最多 {limit} 对）:")

    if not len(scores) or limit == 0:
        lines.append("- 无可输出的向量对。")
        return "\n".join(lines)

    ordered = np.argsort(scores)[::-1]
    rendered = 0
    for pair_index in ordered:
        if rendered >= limit:
            break
        left_id = usable_entries[int(left_indexes[pair_index])][0]
        right_id = usable_entries[int(right_indexes[pair_index])][0]
        left_record = bucket_records[left_id]
        right_record = bucket_records[right_id]
        if (
            _is_sealed_for_output(left_record["file_path"])
            or _is_sealed_for_output(right_record["file_path"])
        ):
            continue
        rendered += 1
        left_source = f" ({left_record['summary_source']})"
        right_source = f" ({right_record['summary_source']})"
        lines.append(
            f"{rendered}. {scores[pair_index]:.6f} | "
            f"{left_id} name={left_record['name']!r} summary={left_record['summary']!r}{left_source} dormant={left_record['dormant']} "
            f"<-> {right_id} name={right_record['name']!r} summary={right_record['summary']!r}{right_source} dormant={right_record['dormant']}"
        )
    if not rendered:
        lines.append("- 无可输出的向量对。")
    return "\n".join(lines)
