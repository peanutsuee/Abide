"""Default-off Remember-Me host runtime bootstrap for Ombre-Brain."""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class RememberMeHostRuntimeError(RuntimeError):
    """Stable host bootstrap error that exposes no path or exception detail."""


@dataclass(frozen=True)
class RememberMeHostBundle:
    host_adapter: Any
    core_adapter: Any
    download_links: Any
    presenter: Any


def create_remember_me_host_bundle(
    *,
    data_root: Path,
    token_store: MutableMapping[str, dict[str, Any]],
    ticket_source_store: MutableMapping[str, str],
    download_lock: Any,
    public_base_url: str | Callable[[], str],
    ttl_seconds: int,
    max_tokens: int,
    vector_provider: Any = None,
) -> RememberMeHostBundle:
    try:
        _validate_inputs(
            data_root=data_root,
            token_store=token_store,
            ticket_source_store=ticket_source_store,
            download_lock=download_lock,
            public_base_url=public_base_url,
            ttl_seconds=ttl_seconds,
            max_tokens=max_tokens,
        )

        from remember_me_adapter import RememberMeAdapter
        from remember_me_core_adapter import RememberMeCoreAdapter
        from remember_me_download_links import (
            RememberMeObDownloadLinkCollaborator,
        )
        from remember_me_mcp_presenter import (
            RememberMeMcpCompatibilityPresenter,
        )

        host_adapter = RememberMeAdapter()
        core_adapter = RememberMeCoreAdapter.from_host_adapter(
            host_adapter,
            data_root,
            vector_provider=vector_provider,
        )
        download_links = RememberMeObDownloadLinkCollaborator(
            token_store=token_store,
            ticket_source_store=ticket_source_store,
            lock=download_lock,
            public_base_url=public_base_url,
            ttl_seconds=ttl_seconds,
            max_tokens=max_tokens,
        )
        presenter = RememberMeMcpCompatibilityPresenter(
            core_adapter,
            download_links,
        )
        return RememberMeHostBundle(
            host_adapter=host_adapter,
            core_adapter=core_adapter,
            download_links=download_links,
            presenter=presenter,
        )
    except RememberMeHostRuntimeError:
        raise
    except Exception:
        raise RememberMeHostRuntimeError(
            "remember_me_host_bootstrap_failed"
        ) from None


def _validate_inputs(
    *,
    data_root: Path,
    token_store: MutableMapping[str, dict[str, Any]],
    ticket_source_store: MutableMapping[str, str],
    download_lock: Any,
    public_base_url: str | Callable[[], str],
    ttl_seconds: int,
    max_tokens: int,
) -> None:
    if not isinstance(data_root, Path) or not data_root.is_absolute():
        _fail()
    if not isinstance(token_store, MutableMapping):
        _fail()
    if not isinstance(ticket_source_store, MutableMapping):
        _fail()
    if not all(
        callable(getattr(download_lock, name, None))
        for name in ("__enter__", "__exit__")
    ):
        _fail()
    if not isinstance(public_base_url, str) and not callable(public_base_url):
        _fail()
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or ttl_seconds <= 0
        or isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or max_tokens <= 0
    ):
        _fail()


def _fail() -> None:
    raise RememberMeHostRuntimeError(
        "remember_me_host_bootstrap_failed"
    ) from None


__all__ = [
    "RememberMeHostBundle",
    "RememberMeHostRuntimeError",
    "create_remember_me_host_bundle",
]
