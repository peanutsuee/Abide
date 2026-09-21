# ============================================================
# Module: Embedding Engine (embedding_engine.py)
# ??:?????
#
# Generates embeddings via Gemini API (OpenAI-compatible),
# stores them in SQLite, and provides cosine similarity search.
# ?? Gemini API(OpenAI ??)?? embedding,
# ??? SQLite ?,??????????
#
# Depended on by: server.py, bucket_manager.py
# ????:server.py, bucket_manager.py
# ============================================================

import os
import json
import math
import sqlite3
import logging
import re
from urllib.parse import urlsplit

from openai import AsyncOpenAI

from maintenance_write_gate import (
    DEFAULT_WRITE_COORDINATOR,
    guarded_mutation,
)

logger = logging.getLogger("ombre_brain.embedding")

_ERROR_TYPE_LIMIT = 80
_REQUEST_URL_LIMIT = 500
_REDACTED_RESPONSE_BODY = "[redacted]"
_BODY_ATTRIBUTE_MISSING = object()
_BODY_EMPTY = object()
_BODY_PRESENT = object()
_BODY_UNKNOWN = object()
_ERROR_CODES = {
    "embedding_http_error",
    "embedding_provider_error",
    "embedding_search_error",
    "embedding_store_error",
    "embedding_timeout",
}
_TIMEOUT_ERROR_TYPES = {
    "APITimeoutError",
    "ConnectTimeout",
    "PoolTimeout",
    "ReadTimeout",
    "TimeoutError",
}


class EmbeddingEngine:
    """
    Embedding generation + SQLite vector storage + cosine search.
    ???? + SQLite ???? + ?????
    """

    def __init__(self, config: dict, write_coordinator=None):
        self.write_coordinator = write_coordinator or DEFAULT_WRITE_COORDINATOR
        dehy_cfg = config.get("dehydration", {})
        embed_cfg = config.get("embedding", {})

        if embed_cfg.get("independent"):
            self.api_key = str(embed_cfg.get("api_key") or "").strip()
        else:
            self.api_key = (
                embed_cfg.get("api_key") or dehy_cfg.get("api_key") or ""
            ).strip()
        self.base_url = (
            (embed_cfg.get("base_url") or "").strip()
            or (dehy_cfg.get("base_url") or "").strip()
            or "https://generativelanguage.googleapis.com/v1beta/openai/"
        )
        self.model = embed_cfg.get("model", "gemini-embedding-001")
        self.enabled = bool(self.api_key) and embed_cfg.get("enabled", True)
        self.last_error = ""
        self.last_error_details = {}

        # --- SQLite path: buckets_dir/embeddings.db ---
        db_path = os.path.join(config["buckets_dir"], "embeddings.db")
        self.db_path = db_path

        # --- Initialize client ---
        if self.enabled:
            self.client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=30.0,
            )
        else:
            self.client = None

        # --- Initialize SQLite ---
        self._init_db()

    def _init_db(self):
        """Create embeddings table if not exists."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS embeddings (
                bucket_id TEXT PRIMARY KEY,
                embedding TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
        """)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(embeddings)").fetchall()
        }
        if "model" not in columns:
            conn.execute(
                "ALTER TABLE embeddings ADD COLUMN model TEXT NOT NULL DEFAULT ''"
            )
        conn.commit()
        conn.close()

    async def generate_and_store(self, bucket_id: str, content: str) -> bool:
        """
        Generate embedding for content and store in SQLite.
        ????? embedding ??? SQLite?
        Returns True on success, False on failure.
        """
        if not self.enabled or not content or not content.strip():
            return False

        try:
            embedding = await self._generate_embedding(content)
            if not embedding:
                return False
            self._store_embedding(bucket_id, embedding)
            self.last_error = ""
            self.last_error_details = {}
            return True
        except Exception as e:
            self._capture_error(e, error_code="embedding_store_error")
            logger.warning(
                "Embedding store failed [%s:%s]",
                self.last_error,
                self.last_error_details.get("error_type", "Exception"),
            )
            return False

    async def embed_text(self, text: str) -> list[float]:
        """Generate one embedding through the existing Host provider path."""
        return await self._generate_embedding(text)

    async def _generate_embedding(self, text: str) -> list[float]:
        """Call API to generate embedding vector."""
        # Truncate to avoid token limits
        truncated = text[:2000]
        try:
            response = await self.client.embeddings.create(
                model=self.model,
                input=truncated,
            )
            if response.data and len(response.data) > 0:
                return response.data[0].embedding
            return []
        except Exception as e:
            self._capture_error(e)
            logger.warning(
                "Embedding API call failed [%s:%s]",
                self.last_error,
                self.last_error_details.get("error_type", "Exception"),
            )
            return []

    def _capture_error(
        self,
        error: Exception,
        *,
        error_code: str | None = None,
    ) -> None:
        """Keep bounded diagnostics without retaining upstream content."""
        response = _safe_getattr(error, "response")
        request = _safe_getattr(error, "request")
        if request is None and response is not None:
            request = _safe_getattr(response, "request")

        error_type = _safe_error_type(error)
        selected_code = (
            error_code
            if error_code in _ERROR_CODES
            else _embedding_error_code(
                error,
                response=response,
                error_type=error_type,
            )
        )
        self.last_error = selected_code
        self.last_error_details = {
            "request_url": _sanitize_request_url(
                _safe_getattr(request, "url")
            ),
            "status_code": _sanitize_status_code(
                _safe_getattr(response, "status_code")
            ),
            "response_body": _redacted_response_body(response),
            "error_type": error_type,
        }

    @guarded_mutation("bucket_embedding_store")
    def _store_embedding(self, bucket_id: str, embedding: list[float]):
        """Store embedding in SQLite."""
        from utils import now_iso
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT OR REPLACE INTO embeddings
                (bucket_id, embedding, model, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (bucket_id, json.dumps(embedding), self.model, now_iso()),
        )
        conn.commit()
        conn.close()

    @guarded_mutation("bucket_embedding_delete")
    def delete_embedding(self, bucket_id: str):
        """Remove embedding when bucket is deleted."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM embeddings WHERE bucket_id = ?", (bucket_id,))
        conn.commit()
        conn.close()

    async def get_embedding(self, bucket_id: str) -> list[float] | None:
        """Retrieve stored embedding for a bucket. Returns None if not found."""
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            """
            SELECT embedding FROM embeddings
            WHERE bucket_id = ? AND model = ?
            """,
            (bucket_id, self.model),
        ).fetchone()
        conn.close()
        if row:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                return None
        return None

    async def search_similar(
        self,
        query: str,
        top_k: int = 10,
        candidate_ids: set[str] | list[str] | None = None,
    ) -> list[tuple[str, float]]:
        """
        Search for buckets similar to query text.
        Returns list of (bucket_id, similarity_score) sorted by score desc.
        ?????????????? (bucket_id, ?????) ???
        """
        if not self.enabled:
            return []

        if candidate_ids is not None:
            candidate_ids = {str(bucket_id) for bucket_id in candidate_ids}
            if not candidate_ids:
                return []

        try:
            query_embedding = await self._generate_embedding(query)
            if not query_embedding:
                return []
        except Exception as e:
            self._capture_error(e, error_code="embedding_search_error")
            logger.warning(
                "Embedding search failed [%s:%s]",
                self.last_error,
                self.last_error_details.get("error_type", "Exception"),
            )
            return []

        # Load embeddings from SQLite. A candidate restriction is used by
        # structured breath filters so a valid filtered candidate cannot be
        # displaced by unrelated global top-N vectors.
        conn = sqlite3.connect(self.db_path)
        if candidate_ids is None:
            rows = conn.execute(
                "SELECT bucket_id, embedding FROM embeddings WHERE model = ?",
                (self.model,),
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in candidate_ids)
            rows = conn.execute(
                "SELECT bucket_id, embedding FROM embeddings "
                f"WHERE model = ? AND bucket_id IN ({placeholders})",
                (self.model, *sorted(candidate_ids)),
            ).fetchall()
        conn.close()

        if not rows:
            return []

        # Calculate cosine similarity
        results = []
        for bucket_id, emb_json in rows:
            try:
                stored_embedding = json.loads(emb_json)
                sim = self._cosine_similarity(query_embedding, stored_embedding)
                results.append((bucket_id, sim))
            except (json.JSONDecodeError, Exception):
                continue

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        if len(a) != len(b) or not a:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


def _safe_getattr(value, name: str):
    if value is None:
        return None
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _safe_error_type(error: Exception) -> str:
    try:
        error_class = type(error)
        candidate = error_class.__name__
        if type(candidate) is not str:
            return "Exception"
        normalized = "".join(
            char
            if char.isascii() and (char.isalnum() or char == "_")
            else "_"
            for char in candidate[:_ERROR_TYPE_LIMIT]
        )
        return normalized or "Exception"
    except Exception:
        return "Exception"


def _embedding_error_code(
    error: Exception,
    *,
    response,
    error_type: str,
) -> str:
    if response is not None:
        return "embedding_http_error"
    if isinstance(error, TimeoutError) or error_type in _TIMEOUT_ERROR_TYPES:
        return "embedding_timeout"
    return "embedding_provider_error"


def _sanitize_request_url(value) -> str:
    try:
        if type(value) is not str:
            return ""
        raw = value
        if not raw or len(raw) > _REQUEST_URL_LIMIT:
            return ""
        if any(ord(char) < 32 or ord(char) == 127 for char in raw):
            return ""
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        hostname = parsed.hostname
        if ":" in hostname:
            if not re.fullmatch(r"[0-9A-Fa-f:.]+", hostname):
                return ""
        elif not re.fullmatch(r"[A-Za-z0-9.-]+", hostname):
            return ""
        port = parsed.port
        if (scheme == "https" and port == 443) or (
            scheme == "http" and port == 80
        ):
            port = None
        host = "[{}]".format(hostname) if ":" in hostname else hostname
        netloc = "{}:{}".format(host, port) if port is not None else host
        sanitized = "{}://{}".format(scheme, netloc)
        if len(sanitized) > _REQUEST_URL_LIMIT:
            return ""
        return sanitized
    except Exception:
        return ""


def _sanitize_status_code(value):
    if type(value) is int and 100 <= value <= 599:
        return value
    return None


def _redacted_response_body(response) -> str:
    if response is None:
        return ""

    text_state = _classify_body_attribute(response, "text")
    content_state = _classify_body_attribute(response, "content")
    if text_state is _BODY_PRESENT or content_state is _BODY_PRESENT:
        return _REDACTED_RESPONSE_BODY
    if text_state is _BODY_UNKNOWN or content_state is _BODY_UNKNOWN:
        return _REDACTED_RESPONSE_BODY
    if text_state is _BODY_EMPTY and content_state is _BODY_EMPTY:
        return ""
    return _REDACTED_RESPONSE_BODY


def _classify_body_attribute(response, name: str):
    try:
        body = getattr(response, name, _BODY_ATTRIBUTE_MISSING)
    except Exception:
        return _BODY_UNKNOWN
    if body is _BODY_ATTRIBUTE_MISSING:
        return _BODY_UNKNOWN
    return _classify_body_value(body)


def _classify_body_value(body):
    if body is None:
        return _BODY_EMPTY
    if type(body) in {str, bytes, bytearray, memoryview}:
        return _BODY_PRESENT if len(body) else _BODY_EMPTY
    return _BODY_PRESENT
