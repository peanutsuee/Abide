"""Host-owned async vector provider for the Remember-Me Core."""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_PROVIDER_NAMESPACE = "ob"
_DEFAULT_BACKEND = "openai-compatible"
_FINGERPRINT_LENGTH = 16


class RememberMeVectorProviderAdapter:
    """Expose the long-lived OB EmbeddingEngine as an RM vector provider."""

    def __init__(
        self,
        embedding_engine: Any,
        *,
        backend: str = _DEFAULT_BACKEND,
    ) -> None:
        if embedding_engine is None or not callable(
            getattr(embedding_engine, "embed_text", None)
        ):
            raise ValueError("embedding_engine_unavailable")
        self._embedding_engine = embedding_engine
        self._backend = _normalized_backend(backend)

    @property
    def enabled(self) -> bool:
        return self._embedding_engine.enabled is True

    @property
    def model_id(self) -> str:
        endpoint = _normalized_endpoint(
            getattr(self._embedding_engine, "base_url", "")
        )
        fingerprint = hashlib.sha256(
            endpoint.encode("utf-8")
        ).hexdigest()[:_FINGERPRINT_LENGTH]
        model = str(
            getattr(self._embedding_engine, "model", "") or ""
        ).strip() or "unset"
        return "{}:{}:{}".format(
            "{}-{}".format(_PROVIDER_NAMESPACE, self._backend),
            fingerprint,
            model,
        )

    async def embed(self, text: str) -> list[float]:
        return await self._embedding_engine.embed_text(text)

    @property
    def last_error(self) -> str:
        return str(getattr(self._embedding_engine, "last_error", "") or "")

    @property
    def last_error_details(self) -> dict[str, Any]:
        details = getattr(self._embedding_engine, "last_error_details", {})
        return dict(details) if isinstance(details, dict) else {}

    def clear_error_diagnostics(self) -> None:
        self._embedding_engine.last_error = ""
        self._embedding_engine.last_error_details = {}


def _normalized_backend(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    if normalized and all(
        char.isascii() and (char.isalnum() or char == "-")
        for char in normalized
    ):
        return normalized
    return "unknown"


def _normalized_endpoint(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        if not parsed.scheme or not parsed.hostname:
            raise ValueError("invalid_endpoint")
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname.lower()
        port = parsed.port
        if (scheme == "https" and port == 443) or (
            scheme == "http" and port == 80
        ):
            port = None
        host = "[{}]".format(hostname) if ":" in hostname else hostname
        netloc = "{}:{}".format(host, port) if port is not None else host
        path = parsed.path.rstrip("/")
        return urlunsplit((scheme, netloc, path, "", ""))
    except (TypeError, ValueError):
        return "opaque:{}".format(
            hashlib.sha256(raw.encode("utf-8")).hexdigest()
        )


__all__ = ["RememberMeVectorProviderAdapter"]
