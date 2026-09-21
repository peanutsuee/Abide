"""Lazy Ombre-Brain host boundary for the pinned public Remember-Me Core."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
import threading
import weakref


EXPECTED_DISTRIBUTION = "remember-me"
EXPECTED_PACKAGE_VERSION = "0.1.0.dev7"
EXPECTED_DATA_COMPATIBILITY = "ombre-brain-assets-v1"
EXPECTED_SANITIZER_ID = "remember-me-pillow-v1"
EXPECTED_PILLOW_RANGE = "Pillow>=10.4,<13"
EXPECTED_MCP_TOOLS = (
    "rm_asset_upload_link",
    "rm_asset_upload_status",
    "rm_asset_get",
    "rm_asset_update_metadata",
    "rm_asset_reindex_embeddings",
    "rm_asset_search",
    "rm_asset_download_link",
    "rm_asset_view",
    "rm_asset_inspect",
)


class RememberMeAdapterError(RuntimeError):
    """Fail-closed adapter error that does not include host data."""


@dataclass(frozen=True)
class RememberMeContract:
    distribution_name: str
    package_version: str
    data_compatibility: str
    sanitizer_id: str
    pillow_range: str
    mcp_tools: tuple[str, ...]


def inspect_remember_me_contract() -> RememberMeContract:
    """Inspect package constants without creating storage or protocol runtimes."""
    try:
        distribution = metadata.distribution(EXPECTED_DISTRIBUTION)
        distribution_name = distribution.metadata.get("Name", "")

        from remember_me.imaging.pillow_sanitizer import (
            PILLOW_VERSION_RANGE,
            SANITIZER_ID,
        )
        from remember_me.mcp.server import MCP_TOOL_NAMES
        from remember_me.metadata import (
            DATA_COMPATIBILITY_VERSION,
            PROJECT_VERSION,
        )

        installed_version = distribution.version
        if PROJECT_VERSION != installed_version:
            raise RememberMeAdapterError(
                "remember_me_contract_mismatch:package_metadata"
            )
        return RememberMeContract(
            distribution_name=distribution_name,
            package_version=installed_version,
            data_compatibility=DATA_COMPATIBILITY_VERSION,
            sanitizer_id=SANITIZER_ID,
            pillow_range=PILLOW_VERSION_RANGE,
            mcp_tools=tuple(MCP_TOOL_NAMES),
        )
    except RememberMeAdapterError:
        raise
    except Exception as exc:
        raise RememberMeAdapterError(
            "remember_me_contract_unavailable"
        ) from exc


def validate_remember_me_contract(
    contract: RememberMeContract | None = None,
) -> RememberMeContract:
    """Validate every pinned host contract field and fail closed."""
    actual = contract or inspect_remember_me_contract()
    expected = RememberMeContract(
        distribution_name=EXPECTED_DISTRIBUTION,
        package_version=EXPECTED_PACKAGE_VERSION,
        data_compatibility=EXPECTED_DATA_COMPATIBILITY,
        sanitizer_id=EXPECTED_SANITIZER_ID,
        pillow_range=EXPECTED_PILLOW_RANGE,
        mcp_tools=EXPECTED_MCP_TOOLS,
    )
    for field_name in RememberMeContract.__dataclass_fields__:
        if getattr(actual, field_name) != getattr(expected, field_name):
            raise RememberMeAdapterError(
                "remember_me_contract_mismatch:{}".format(field_name)
            )
    return actual


_RUNTIME_OWNERS: dict[Path, weakref.ReferenceType] = {}
_RUNTIME_OWNERS_LOCK = threading.Lock()


class RememberMeAdapter:
    """Own at most one explicitly created LocalRuntime."""

    def __init__(self) -> None:
        self._runtime = None
        self._data_root: Path | None = None
        self._vector_provider = None

    @property
    def runtime_created(self) -> bool:
        return self._runtime is not None

    def create_runtime(self, data_root: Path, vector_provider=None):
        if not isinstance(data_root, Path):
            raise RememberMeAdapterError("remember_me_data_root_must_be_path")
        normalized_root = data_root.expanduser().resolve()
        if self._runtime is not None:
            if (
                normalized_root == self._data_root
                and (
                    vector_provider is None
                    or vector_provider is self._vector_provider
                )
            ):
                return self._runtime
            raise RememberMeAdapterError("remember_me_runtime_already_created")

        validate_remember_me_contract()
        with _RUNTIME_OWNERS_LOCK:
            owner_ref = _RUNTIME_OWNERS.get(normalized_root)
            owner = owner_ref() if owner_ref is not None else None
            if owner is not None and owner is not self:
                raise RememberMeAdapterError(
                    "remember_me_data_root_already_owned"
                )

            from remember_me.factory import create_local_runtime

            try:
                runtime = create_local_runtime(
                    normalized_root,
                    vector_provider=vector_provider,
                )
            except Exception as exc:
                raise RememberMeAdapterError(
                    "remember_me_runtime_creation_failed"
                ) from exc
            self._runtime = runtime
            self._data_root = normalized_root
            self._vector_provider = vector_provider
            _RUNTIME_OWNERS[normalized_root] = weakref.ref(self)
            return runtime


__all__ = [
    "EXPECTED_DATA_COMPATIBILITY",
    "EXPECTED_DISTRIBUTION",
    "EXPECTED_MCP_TOOLS",
    "EXPECTED_PACKAGE_VERSION",
    "EXPECTED_PILLOW_RANGE",
    "EXPECTED_SANITIZER_ID",
    "RememberMeAdapter",
    "RememberMeAdapterError",
    "RememberMeContract",
    "inspect_remember_me_contract",
    "validate_remember_me_contract",
]
