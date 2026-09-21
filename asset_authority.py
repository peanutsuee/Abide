"""Configuration primitives for the asset-authority cutover plane.

This module deliberately does not wire authority selection into MCP,
Dashboard, or server startup.  It only provides a strict, reusable parser and
the small value types that later integration work can consume.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from typing import Mapping


class AssetAuthority(str, Enum):
    """The backend that is allowed to own production asset operations."""

    LEGACY = "legacy"
    RM = "rm"


class AssetAuthorityConfigError(ValueError):
    """Stable configuration failure for the asset subsystem."""

    def __init__(self, code: str = "asset_authority_invalid") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AssetAuthorityConfig:
    """Parsed authority configuration with an explicit default."""

    authority: AssetAuthority
    source: str


def parse_asset_authority(value: str | None) -> AssetAuthority:
    """Parse ``OMBRE_ASSET_AUTHORITY`` without changing application routing."""

    if value is None or not value.strip():
        return AssetAuthority.LEGACY
    normalized = value.strip().casefold()
    try:
        return AssetAuthority(normalized)
    except ValueError as exc:
        raise AssetAuthorityConfigError() from exc


def load_asset_authority(
    environ: Mapping[str, str] | None = None,
) -> AssetAuthorityConfig:
    """Load the selector from an environment-like mapping.

    The default is intentionally legacy.  Reading this value alone does not
    initialize Remember-Me or select any existing MCP/Dashboard route.
    """

    values = os.environ if environ is None else environ
    raw = values.get("OMBRE_ASSET_AUTHORITY")
    authority = parse_asset_authority(raw)
    source = "env" if raw is not None and raw.strip() else "default"
    return AssetAuthorityConfig(authority=authority, source=source)


__all__ = [
    "AssetAuthority",
    "AssetAuthorityConfig",
    "AssetAuthorityConfigError",
    "load_asset_authority",
    "parse_asset_authority",
]
