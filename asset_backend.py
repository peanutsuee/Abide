"""Authority-selected asset backends for Dashboard and MCP integration.

This module is deliberately an adapter layer, not a storage framework.  The
legacy adapter delegates to ``AssetStore`` and the RM adapter delegates to the
already-pinned Remember-Me Core host boundary.  Authority is resolved by the
runtime registry from the Implementation A control plane; backend presence
alone never selects RM.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from asset_authority import AssetAuthority, AssetAuthorityConfig, load_asset_authority
from asset_cutover_state import (
    CutoverStateError,
    CutoverStateStore,
    validate_cutover_boot,
)
from asset_mutation_gate import AssetMutationGate
from asset_storage_layout import validate_asset_storage_layout
from asset_store import AssetStore, AssetStoreError
from cutover_boot_coordination import (
    read_expired_rm_acceptance_recovery_boot_proof,
    read_rm_prepared_boot_coordination,
)


class AssetBackendError(RuntimeError):
    """Stable, path-free backend and authority failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BackendAssetResource:
    """One resolved asset, represented by a safe path or in-memory bytes."""

    asset: dict
    path: Path | None = None
    content: bytes | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.asset, dict):
            raise ValueError("asset_resource_invalid")
        if (self.path is None) == (self.content is None):
            raise ValueError("asset_resource_body_invalid")
        if self.path is not None and not isinstance(self.path, Path):
            raise ValueError("asset_resource_path_invalid")
        if self.content is not None and not isinstance(self.content, bytes):
            raise ValueError("asset_resource_content_invalid")


class RuntimeMutationGate:
    """Use the persistent gate when initialized; preserve no-state legacy boot."""

    def __init__(self, state_store: CutoverStateStore | None) -> None:
        self.state_store = state_store
        self._gate = AssetMutationGate(state_store) if state_store else None

    def assert_public_mutation_allowed(self) -> None:
        if self._gate is None:
            return
        try:
            self._gate.assert_public_mutation_allowed()
        except CutoverStateError as exc:
            if exc.code == "rm_authority_unavailable":
                raise AssetBackendError("rm_authority_unavailable") from exc
            if exc.code == "asset_mutation_frozen":
                raise AssetBackendError("asset_write_frozen") from exc
            raise AssetBackendError("asset_write_gate_unavailable") from exc

    def public_mutations_allowed(self) -> bool:
        try:
            self.assert_public_mutation_allowed()
        except AssetBackendError:
            return False
        return True


def _backend_error_code(exc: BaseException, fallback: str = "asset_unavailable") -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code else fallback


def _asset_public_metadata(asset: dict, deduplicated: bool | None = None) -> dict:
    filename = asset.get("original_filename", asset.get("filename", ""))
    payload = {
        "asset_id": asset["asset_id"],
        "source_sha256": asset["source_sha256"],
        "stored_sha256": asset["stored_sha256"],
        "decoded_bytes": asset["decoded_bytes"],
        "stored_bytes": asset["stored_bytes"],
        "mime_type": asset["mime_type"],
        "filename": filename,
        "kind": asset["kind"],
        "width": asset["width"],
        "height": asset["height"],
        "created_at": asset["created_at"],
        "title": asset.get("title", ""),
        "description": asset.get("description", ""),
        "tags": list(asset.get("tags", [])),
        "updated_at": asset.get("updated_at", asset["created_at"]),
    }
    if deduplicated is not None:
        payload["deduplicated"] = deduplicated
    return payload


def _json_error(code: str) -> str:
    return json.dumps({"ok": False, "error": code}, ensure_ascii=False, sort_keys=True)


class LegacyAssetBackend:
    """Focused adapter over the existing durable ``AssetStore``."""

    authority = AssetAuthority.LEGACY
    name = "legacy"

    def __init__(
        self,
        store: AssetStore,
        mutation_gate: RuntimeMutationGate,
        *,
        embedding_index: Any = None,
    ) -> None:
        self.store = store
        self.mutation_gate = mutation_gate
        self.embedding_index = embedding_index

    def assert_public_mutation_allowed(self) -> None:
        self.mutation_gate.assert_public_mutation_allowed()

    def create_temp_path(self, suffix: str = ".upload") -> Path:
        self.assert_public_mutation_allowed()
        return self.store.create_temp_path(suffix)

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        return AssetStore.sanitize_filename(filename)

    def clean_metadata_text(self, value: str, max_chars: int, field: str) -> str:
        return self.store._clean_metadata_text(value, max_chars, field)

    def normalize_tags(self, tags: list[str]) -> list[str]:
        return [display for _, display in self.store._normalize_tags(tags)]

    def persist_upload(
        self,
        source_path: str | Path,
        source_sha256: str,
        decoded_bytes: int,
        original_filename: str,
        mime_type: str,
        *,
        require_image: bool = False,
        title: str = "",
        description: str = "",
        tags: list[str] | None = None,
    ) -> dict:
        self.assert_public_mutation_allowed()
        return self.store.persist_upload(
            source_path,
            source_sha256,
            decoded_bytes,
            original_filename,
            mime_type,
            require_image=require_image,
        )

    def get(self, asset_id: str) -> dict | None:
        return self.store.get(asset_id)

    def update_metadata(
        self,
        asset_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        self.assert_public_mutation_allowed()
        return self.store.update_metadata(
            asset_id,
            title=title,
            description=description,
            tags=tags,
        )

    def delete(self, asset_id: str) -> dict:
        self.assert_public_mutation_allowed()
        return self.store.delete(asset_id)

    def search(self, **kwargs) -> dict:
        return self.store.search(**kwargs)

    def resolve(self, asset_id: str) -> BackendAssetResource | None:
        resolved = self.store.resolve_file(asset_id)
        if not resolved:
            return None
        asset, path = resolved
        return BackendAssetResource(asset=asset, path=path)

    async def reindex(self, asset_id: str = "", limit: int = 100) -> dict:
        self.assert_public_mutation_allowed()
        if self.embedding_index is None:
            raise AssetBackendError("asset_unavailable")
        return await self.embedding_index.reindex(
            asset_id=(asset_id or "").strip(),
            limit=limit,
        )


class RememberMeAssetBackend:
    """Adapter over the existing RM Core and MCP compatibility presenter."""

    authority = AssetAuthority.RM
    name = "rm"

    def __init__(
        self,
        bundle_provider: Callable[[], Any],
        mutation_gate: RuntimeMutationGate,
    ) -> None:
        self._bundle_provider = bundle_provider
        self.mutation_gate = mutation_gate

    def _bundle(self) -> Any:
        bundle = self._bundle_provider()
        if bundle is None:
            raise AssetBackendError("rm_authority_unavailable")
        if not hasattr(bundle, "core_adapter"):
            raise AssetBackendError("rm_authority_unavailable")
        return bundle

    def _core(self) -> Any:
        core = getattr(self._bundle(), "core_adapter", None)
        if core is None:
            raise AssetBackendError("rm_authority_unavailable")
        return core

    def _presenter(self) -> Any:
        presenter = getattr(self._bundle(), "presenter", None)
        if presenter is None:
            raise AssetBackendError("rm_authority_unavailable")
        return presenter

    def assert_public_mutation_allowed(self) -> None:
        self.mutation_gate.assert_public_mutation_allowed()

    def create_temp_path(self, suffix: str = ".upload") -> Path:
        self.assert_public_mutation_allowed()
        fd, raw_path = tempfile.mkstemp(prefix="ombre-rm-dashboard-", suffix=suffix)
        os.close(fd)
        return Path(raw_path)

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        return AssetStore.sanitize_filename(filename)

    @staticmethod
    def clean_metadata_text(value: str, max_chars: int, field: str) -> str:
        return AssetStore._clean_metadata_text(value, max_chars, field)

    @staticmethod
    def normalize_tags(tags: list[str]) -> list[str]:
        return [display for _, display in AssetStore._normalize_tags(tags)]

    def persist_upload(
        self,
        source_path: str | Path,
        source_sha256: str,
        decoded_bytes: int,
        original_filename: str,
        mime_type: str,
        *,
        require_image: bool = False,
        title: str = "",
        description: str = "",
        tags: list[str] | None = None,
    ) -> dict:
        self.assert_public_mutation_allowed()
        path = Path(source_path)
        try:
            content = path.read_bytes()
            if len(content) != decoded_bytes or not hmac.compare_digest(
                hashlib.sha256(content).hexdigest(), source_sha256
            ):
                raise AssetBackendError("source_hash_mismatch")
            result = self._core().ingest_image(
                content,
                decoded_bytes,
                original_filename,
                mime_type,
                title=title,
                description=description,
                tags=tuple(tags or ()),
            )
            if not isinstance(result, dict):
                raise AssetBackendError("asset_unavailable")
            return result
        except AssetBackendError:
            raise
        except Exception as exc:
            raise AssetBackendError(_backend_error_code(exc)) from exc
        finally:
            path.unlink(missing_ok=True)

    def ingest_public_metadata(
        self,
        content: bytes,
        expected_bytes: int,
        filename: str,
        mime_type: str,
        *,
        title: str = "",
        description: str = "",
        tags: list[str] | None = None,
    ) -> dict:
        self.assert_public_mutation_allowed()
        try:
            result = self._core().ingest_ob_public_metadata(
                content,
                expected_bytes,
                filename,
                mime_type,
                title=title,
                description=description,
                tags=tuple(tags or ()),
            )
            if not isinstance(result, dict):
                raise AssetBackendError("asset_unavailable")
            return result
        except AssetBackendError:
            raise
        except Exception as exc:
            raise AssetBackendError(_backend_error_code(exc)) from exc

    def get(self, asset_id: str) -> dict | None:
        try:
            return self._core().get(asset_id)
        except Exception as exc:
            raise AssetBackendError(_backend_error_code(exc)) from exc

    def update_metadata(
        self,
        asset_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        self.assert_public_mutation_allowed()
        try:
            return self._core().update_metadata(
                asset_id,
                title=title,
                description=description,
                tags=tags,
            )
        except Exception as exc:
            raise AssetBackendError(_backend_error_code(exc)) from exc

    def delete(self, asset_id: str) -> dict:
        self.assert_public_mutation_allowed()
        try:
            return self._core().delete(asset_id)
        except Exception as exc:
            raise AssetBackendError(_backend_error_code(exc)) from exc

    def search(self, **kwargs) -> dict:
        try:
            return asyncio.run(self._core().search(**kwargs))
        except Exception as exc:
            raise AssetBackendError(_backend_error_code(exc, "search_unavailable")) from exc

    def resolve(self, asset_id: str) -> BackendAssetResource | None:
        try:
            result = self._core().resolve_blob(asset_id)
            if not result:
                return None
            asset, content = result
            return BackendAssetResource(asset=asset, content=bytes(content))
        except Exception as exc:
            code = _backend_error_code(exc)
            if code in {"asset_not_found", "asset_unavailable"}:
                return None
            raise AssetBackendError(code) from exc

    async def reindex(self, asset_id: str = "", limit: int = 100) -> Any:
        self.assert_public_mutation_allowed()
        try:
            return await self._core().reindex_embeddings(
                asset_id=(asset_id or "").strip(),
                limit=limit,
            )
        except Exception as exc:
            raise AssetBackendError(_backend_error_code(exc)) from exc

    def mcp_get(self, asset_id: str) -> str:
        return self._presenter().rm_asset_get(asset_id)

    def mcp_update_metadata(self, asset_id: str, **kwargs) -> str:
        self.assert_public_mutation_allowed()
        return self._presenter().rm_asset_update_metadata(asset_id, **kwargs)

    async def mcp_search(self, **kwargs) -> str:
        return await self._presenter().rm_asset_search(**kwargs)

    async def mcp_reindex(self, **kwargs) -> str:
        self.assert_public_mutation_allowed()
        return await self._presenter().rm_asset_reindex_embeddings(**kwargs)

    def mcp_download_link(self, asset_id: str) -> str:
        return self._presenter().rm_asset_download_link(asset_id)

    def mcp_view(self, asset_id: str) -> Any:
        return self._presenter().rm_asset_view(asset_id)

    def mcp_inspect(self, asset_id: str) -> Any:
        return self._presenter().rm_asset_inspect(asset_id)


class RuntimeAssetBackendRegistry:
    """Resolve one authority-selected backend from config, state, and runtime."""

    def __init__(
        self,
        *,
        config: AssetAuthorityConfig,
        legacy_backend: LegacyAssetBackend,
        rm_backend: RememberMeAssetBackend,
        bundle_provider: Callable[[], Any],
        state_store: CutoverStateStore | None,
    ) -> None:
        self.config = config
        self.legacy_backend = legacy_backend
        self.rm_backend = rm_backend
        self._bundle_provider = bundle_provider
        self.state_store = state_store

    @classmethod
    def from_runtime(
        cls,
        *,
        legacy_store: AssetStore,
        bundle_provider: Callable[[], Any],
        embedding_index: Any = None,
        authority_environ: dict[str, str] | None = None,
    ) -> "RuntimeAssetBackendRegistry":
        config = load_asset_authority(authority_environ)
        legacy_root = legacy_store.data_root
        raw_rm_root = (
            os.environ.get("OMBRE_RM_DATA_ROOT", "")
            if authority_environ is None
            else authority_environ.get("OMBRE_RM_DATA_ROOT", "")
        ).strip()
        if not raw_rm_root and config.authority is AssetAuthority.RM:
            raise AssetBackendError("rm_authority_unavailable")
        if raw_rm_root and config.authority is AssetAuthority.LEGACY and bundle_provider() is None:
            # Preserve v1.4.0 default-off behavior: an unused RM setting is
            # not initialized or validated until RM is actually bootstrapped.
            raw_rm_root = ""
        rm_root = Path(raw_rm_root) if raw_rm_root else legacy_root / "remember-me"
        state_root = legacy_root / "state"
        try:
            validate_asset_storage_layout(legacy_root, rm_root, state_root)
        except Exception as exc:
            raise AssetBackendError("asset_storage_layout_invalid") from exc

        state_db_path = state_root / "migration.sqlite3"
        state_store = None
        if state_db_path.is_file():
            try:
                state_store = CutoverStateStore(state_db_path)
            except CutoverStateError as exc:
                raise AssetBackendError("asset_authority_unavailable") from exc

        gate = RuntimeMutationGate(state_store)
        legacy_backend = LegacyAssetBackend(
            legacy_store,
            gate,
            embedding_index=embedding_index,
        )
        rm_backend = RememberMeAssetBackend(bundle_provider, gate)
        registry = cls(
            config=config,
            legacy_backend=legacy_backend,
            rm_backend=rm_backend,
            bundle_provider=bundle_provider,
            state_store=state_store,
        )
        registry._validate_boot()
        return registry

    def _validate_boot(self):
        snapshot = self.state_store.get_snapshot() if self.state_store else None
        coordination = None
        expired_rm_recovery = None
        if snapshot is not None and self.config.authority is AssetAuthority.RM:
            coordination = read_rm_prepared_boot_coordination(
                self.state_store.db_path,
                snapshot,
            )
            expired_rm_recovery = read_expired_rm_acceptance_recovery_boot_proof(
                self.state_store.db_path,
                snapshot,
            )
        try:
            return validate_cutover_boot(
                self.config.authority,
                snapshot,
                rm_available=self._rm_runtime_available(),
                coordination=coordination,
                expired_rm_recovery=expired_rm_recovery,
            )
        except CutoverStateError as exc:
            raise AssetBackendError("asset_authority_unavailable") from exc

    def _rm_runtime_available(self) -> bool:
        try:
            bundle = self._bundle_provider()
        except Exception:
            return False
        return bool(
            bundle is not None
            and getattr(bundle, "core_adapter", None) is not None
            and getattr(bundle, "presenter", None) is not None
        )

    @property
    def authority(self) -> AssetAuthority:
        return self._validate_boot().authority

    @property
    def snapshot(self):
        return self.state_store.get_snapshot() if self.state_store else None

    def assert_public_mutation_allowed(self) -> None:
        self._validate_boot()
        backend = self.selected_backend()
        backend.assert_public_mutation_allowed()

    def selected_backend(self) -> LegacyAssetBackend | RememberMeAssetBackend:
        authority = self._validate_boot().authority
        if authority is AssetAuthority.LEGACY:
            return self.legacy_backend
        if not self._rm_runtime_available():
            raise AssetBackendError("rm_authority_unavailable")
        return self.rm_backend

    def backend_for(self, authority: AssetAuthority) -> LegacyAssetBackend | RememberMeAssetBackend:
        if authority is AssetAuthority.LEGACY:
            return self.legacy_backend
        return self.rm_backend


__all__ = [
    "AssetBackendError",
    "BackendAssetResource",
    "LegacyAssetBackend",
    "RememberMeAssetBackend",
    "RuntimeAssetBackendRegistry",
    "RuntimeMutationGate",
]
