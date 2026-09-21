from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timezone

from maintenance_write_gate import (
    DEFAULT_WRITE_COORDINATOR,
    guarded_mutation,
)


logger = logging.getLogger("ombre_brain.asset_embedding")

ASSET_SEMANTIC_THRESHOLD = 0.42


class AssetEmbeddingIndex:
    def __init__(self, asset_store, embedding_engine):
        self.asset_store = asset_store
        self.embedding_engine = embedding_engine
        self.write_coordinator = getattr(
            asset_store,
            "write_coordinator",
            DEFAULT_WRITE_COORDINATOR,
        )
        self.db_path = asset_store.db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS asset_embeddings (
                    asset_id TEXT PRIMARY KEY,
                    embedding TEXT NOT NULL,
                    model TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (asset_id) REFERENCES assets(asset_id)
                        ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_asset_embeddings_model "
                "ON asset_embeddings(model)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_asset_embeddings_content_hash "
                "ON asset_embeddings(content_hash)"
            )
            conn.execute(
                """
                DELETE FROM asset_embeddings
                WHERE asset_id NOT IN (SELECT asset_id FROM assets)
                """
            )

    @staticmethod
    def build_index_text(asset: dict) -> str:
        title = str(asset.get("title", "") or "").strip()
        description = str(asset.get("description", "") or "").strip()
        tags = [
            str(tag).strip()
            for tag in asset.get("tags", [])
            if str(tag).strip()
        ]
        if not title and not description and not tags:
            return ""
        return "\n".join(
            [
                f"Title: {title}",
                f"Description: {description}",
                f"Tags: {', '.join(tags)}",
                f"Filename: {asset.get('original_filename', '')}",
                f"Kind: {asset.get('kind', '')}",
                f"MIME type: {asset.get('mime_type', '')}",
            ]
        )

    @staticmethod
    def _content_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _existing(self, asset_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM asset_embeddings WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
        return dict(row) if row else None

    @guarded_mutation("asset_embedding_delete")
    def delete(self, asset_id: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", asset_id or ""):
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM asset_embeddings WHERE asset_id = ?",
                (asset_id,),
            )

    def is_current(self, asset: dict) -> bool:
        text = self.build_index_text(asset)
        if not text:
            return self._existing(asset["asset_id"]) is None
        existing = self._existing(asset["asset_id"])
        return bool(
            existing
            and existing["model"] == self.embedding_engine.model
            and existing["content_hash"] == self._content_hash(text)
        )

    async def index_asset(self, asset: dict) -> str:
        asset_id = asset["asset_id"]
        text = self.build_index_text(asset)
        if not text:
            self.delete(asset_id)
            return "skipped"

        model = self.embedding_engine.model
        content_hash = self._content_hash(text)
        existing = self._existing(asset_id)
        if (
            existing
            and existing["model"] == model
            and existing["content_hash"] == content_hash
        ):
            return "skipped"
        if existing:
            self.delete(asset_id)
        if not self.embedding_engine.enabled:
            return "failed"

        try:
            embedding = await self.embedding_engine._generate_embedding(text)
        except Exception as exc:
            logger.warning(
                "Asset embedding generation failed asset_id=%s error=%s",
                asset_id,
                type(exc).__name__,
            )
            return "failed"
        if not embedding:
            logger.warning(
                "Asset embedding generation returned no vector asset_id=%s",
                asset_id,
            )
            return "failed"

        current = self.asset_store.get(asset_id)
        if not current:
            self.delete(asset_id)
            return "failed"
        current_text = self.build_index_text(current)
        if (
            self.embedding_engine.model != model
            or self._content_hash(current_text) != content_hash
        ):
            return "failed"

        with self.write_coordinator.writer_scope("asset_embedding_store"):
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO asset_embeddings (
                        asset_id, embedding, model, content_hash, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(asset_id) DO UPDATE SET
                        embedding = excluded.embedding,
                        model = excluded.model,
                        content_hash = excluded.content_hash,
                        updated_at = excluded.updated_at
                    """,
                    (
                        asset_id,
                        json.dumps(embedding),
                        model,
                        content_hash,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
        return "indexed"

    async def search(
        self,
        query: str,
        top_k: int = 100,
        threshold: float = ASSET_SEMANTIC_THRESHOLD,
    ) -> dict[str, float]:
        if not self.embedding_engine.enabled or not query.strip():
            return {}
        try:
            query_embedding = await self.embedding_engine._generate_embedding(query)
        except Exception as exc:
            logger.warning(
                "Asset semantic query failed error=%s",
                type(exc).__name__,
            )
            return {}
        if not query_embedding:
            return {}

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ae.asset_id, ae.embedding
                FROM asset_embeddings ae
                JOIN assets a ON a.asset_id = ae.asset_id
                WHERE ae.model = ?
                """,
                (self.embedding_engine.model,),
            ).fetchall()

        results = []
        for row in rows:
            try:
                stored = json.loads(row["embedding"])
                score = self.embedding_engine._cosine_similarity(
                    query_embedding,
                    stored,
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if score >= threshold:
                results.append((row["asset_id"], score))
        results.sort(key=lambda item: (-item[1], item[0]))
        return {
            asset_id: round(score, 6)
            for asset_id, score in results[:top_k]
        }

    async def reindex(
        self,
        asset_id: str = "",
        limit: int = 100,
    ) -> dict:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("invalid_limit")
        if asset_id:
            asset = self.asset_store.get(asset_id)
            if not asset:
                self.delete(asset_id)
                raise ValueError("asset_unavailable")
            assets = [asset]
        else:
            assets = self.asset_store.list_for_embedding(limit)

        counts = {
            "scanned": 0,
            "indexed": 0,
            "skipped": 0,
            "failed": 0,
        }
        for asset in assets:
            counts["scanned"] += 1
            try:
                status = await self.index_asset(asset)
            except Exception as exc:
                logger.warning(
                    "Asset embedding reindex failed asset_id=%s error=%s",
                    asset["asset_id"],
                    type(exc).__name__,
                )
                status = "failed"
            counts[status] += 1
        return counts
