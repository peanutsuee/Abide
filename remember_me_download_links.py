"""Injectable Ombre-Brain download Ticket creation for Remember-Me assets."""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
import re
import secrets
import threading
import time
from typing import Any, Protocol
import unicodedata
from urllib.parse import urlparse

_DOWNLOAD_PATH_PREFIX = "/rm/asset-download/"
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{40,128}")
_SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._ -]+")
_STORED_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "application/octet-stream": ".bin",
}
_MAX_TOKEN_ATTEMPTS = 32


class RememberMeDownloadLinkError(RuntimeError):
    """Stable download-link error that carries no host or asset details."""

    def __init__(self, code: str) -> None:
        self.code = (
            "download_store_full"
            if code == "download_store_full"
            else "download_unavailable"
        )
        super().__init__(self.code)


class RememberMeDownloadLinkCollaborator(Protocol):
    """OB-owned Ticket and URL creation seam."""

    def create_download_link(
        self,
        asset: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Return the existing OB download-link payload."""


class RememberMeObDownloadLinkCollaborator:
    """Create legacy-compatible OB download Tickets from public metadata."""

    def __init__(
        self,
        *,
        token_store: MutableMapping[str, dict[str, Any]] | None = None,
        lock: Any = None,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] | None = None,
        ticket_source_store: MutableMapping[str, str] | None = None,
        public_base_url: str | Callable[[], str] = "",
        ttl_seconds: int = 300,
        max_tokens: int = 100,
    ) -> None:
        if (
            not callable(clock)
            or (token_factory is not None and not callable(token_factory))
            or isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or ttl_seconds <= 0
            or isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or max_tokens <= 0
            or (
                ticket_source_store is not None
                and not isinstance(ticket_source_store, MutableMapping)
            )
        ):
            raise RememberMeDownloadLinkError("download_unavailable")
        self._token_store = token_store if token_store is not None else {}
        self._ticket_source_store = ticket_source_store
        self._lock = lock if lock is not None else threading.Lock()
        self._clock = clock
        self._token_factory = token_factory or (
            lambda: secrets.token_urlsafe(32)
        )
        self._public_base_url = public_base_url
        self._ttl_seconds = ttl_seconds
        self._max_tokens = max_tokens

    def create_download_link(
        self,
        asset: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Create one bounded Ticket and the current OB public payload."""
        try:
            fields = _download_fields(asset)
            current = float(self._clock())
            filename = safe_download_filename(asset)
            base_url = _valid_public_base_url(self._public_base_url)
            with self._lock:
                self._cleanup_expired(current)
                if len(self._token_store) >= self._max_tokens:
                    raise RememberMeDownloadLinkError(
                        "download_store_full"
                    )
                token = self._new_token()
                try:
                    self._token_store[token] = {
                        "asset_id": fields["asset_id"],
                        "expires_at": current + self._ttl_seconds,
                        "get_count": 0,
                    }
                    if self._ticket_source_store is not None:
                        self._ticket_source_store[token] = "remember_me"
                except Exception:
                    self._token_store.pop(token, None)
                    if self._ticket_source_store is not None:
                        self._ticket_source_store.pop(token, None)
                    raise
            download_path = f"{_DOWNLOAD_PATH_PREFIX}{token}"
            return {
                "ok": True,
                "asset_id": fields["asset_id"],
                "filename": filename,
                "mime_type": fields["mime_type"],
                "stored_bytes": fields["stored_bytes"],
                "stored_sha256": fields["stored_sha256"],
                "download_path": download_path,
                "download_url": (
                    f"{base_url}{download_path}" if base_url else ""
                ),
                "expires_in_seconds": self._ttl_seconds,
            }
        except RememberMeDownloadLinkError:
            raise
        except Exception as exc:
            raise RememberMeDownloadLinkError(
                "download_unavailable"
            ) from exc

    def _cleanup_expired(self, current: float) -> None:
        expired = [
            token
            for token, item in self._token_store.items()
            if item["expires_at"] <= current
        ]
        for token in expired:
            self._token_store.pop(token, None)
            if self._ticket_source_store is not None:
                self._ticket_source_store.pop(token, None)

    def _new_token(self) -> str:
        for _ in range(_MAX_TOKEN_ATTEMPTS):
            token = self._token_factory()
            if (
                isinstance(token, str)
                and _TOKEN_PATTERN.fullmatch(token) is not None
                and token not in self._token_store
            ):
                return token
        raise RememberMeDownloadLinkError("download_unavailable")


def safe_download_filename(asset: Mapping[str, Any]) -> str:
    """Reproduce the current OB filename sanitizing and truncation rules."""
    mime_type = asset.get("mime_type")
    extension = _STORED_EXTENSIONS.get(mime_type)
    if extension is None:
        raise RememberMeDownloadLinkError("download_unavailable")
    raw_name = asset.get("filename", "")
    name = _SAFE_FILENAME_PATTERN.sub(
        "_",
        raw_name if isinstance(raw_name, str) else "",
    ).strip(" .")
    if not name:
        asset_id = asset.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id:
            raise RememberMeDownloadLinkError("download_unavailable")
        name = f"remember-me-{asset_id}{extension}"
    elif not name.lower().endswith(extension.lower()):
        name += extension
    return name[:180]


def _download_fields(asset: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(asset, Mapping):
        raise RememberMeDownloadLinkError("download_unavailable")
    fields = {
        "asset_id": asset.get("asset_id"),
        "mime_type": asset.get("mime_type"),
        "stored_bytes": asset.get("stored_bytes"),
        "stored_sha256": asset.get("stored_sha256"),
    }
    if (
        not isinstance(fields["asset_id"], str)
        or not fields["asset_id"]
        or fields["mime_type"] not in _STORED_EXTENSIONS
        or not isinstance(fields["stored_bytes"], int)
        or isinstance(fields["stored_bytes"], bool)
        or fields["stored_bytes"] < 0
        or not isinstance(fields["stored_sha256"], str)
        or not fields["stored_sha256"]
    ):
        raise RememberMeDownloadLinkError("download_unavailable")
    return fields


def _valid_public_base_url(
    value: str | Callable[[], str],
) -> str:
    raw = value() if callable(value) else value
    if not isinstance(raw, str):
        return ""
    if (
        not raw
        or any(
            character.isspace()
            or ord(character) < 32
            or ord(character) == 127
            or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in raw
        )
        or "\\" in raw
    ):
        return ""
    try:
        parsed = urlparse(raw)
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.netloc.endswith(":")
        or "\\" in parsed.netloc
        or not hostname
        or "\\" in hostname
        or username is not None
        or password is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port is not None
        and not 1 <= port <= 65535
    ):
        return ""
    return raw.rstrip("/")


__all__ = [
    "RememberMeDownloadLinkCollaborator",
    "RememberMeDownloadLinkError",
    "RememberMeObDownloadLinkCollaborator",
    "safe_download_filename",
]
