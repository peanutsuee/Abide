"""O5B opt-in import capture and run coordination.

This module is intentionally inert until a caller explicitly requests raw
evidence capture.  It owns only capture/run metadata; it does not expose raw
evidence to memory recall, model context, Dashboard browsing, or MCP tools.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from typing import Any

from raw_evidence_store import RawEvidenceError, RawEvidenceStore


CAPTURE_MODE = "IMPORT_SNAPSHOT"
FIDELITY_LEVEL = "IMPORT_SNAPSHOT"
IMPORTER_VERSION = "ombre-import-o5b-v1"
PARSER_VERSION = "import-parsers-v1"
CHUNKER_VERSION = "chunk-turns-v1"
RAW_EVIDENCE_ROOT_ENV = "OMBRE_RAW_EVIDENCE_ROOT"
RM_ROOT_ENV = "OMBRE_RM_DATA_ROOT"


def parse_capture_option(value: str | None) -> bool:
    """Parse the additive upload option using strict 0/1 semantics."""

    if value is None:
        return False
    if value == "1":
        return True
    if value == "0":
        return False
    raise RawEvidenceError("capture_option_invalid")


def source_sha256(raw_bytes: bytes) -> str:
    if not isinstance(raw_bytes, bytes):
        raise RawEvidenceError("invalid_input")
    return hashlib.sha256(raw_bytes).hexdigest()


@dataclass(frozen=True)
class PreparedImportRun:
    run: dict[str, Any]
    source_sha256: str

    @property
    def run_id(self) -> str:
        return self.run["run_id"]


class RawEvidenceImportCoordinator:
    """Lazily construct the isolated store and coordinate one import run."""

    def __init__(self, config: dict[str, Any]):
        root = config.get("raw_evidence_root")
        if not root:
            raise RawEvidenceError("evidence_root_missing")

        forbidden_roots = [config.get("buckets_dir")]
        rm_root = os.environ.get(RM_ROOT_ENV, "")
        if rm_root:
            forbidden_roots.append(rm_root)
        self.store = RawEvidenceStore(
            root,
            forbidden_roots=[path for path in forbidden_roots if path],
        )

    def prepare_run(
        self,
        raw_bytes: bytes,
        *,
        filename: str,
        media_type: str,
        preserve_raw: bool,
        resume: bool,
        actor_id: str = "dashboard",
    ) -> PreparedImportRun:
        if not isinstance(raw_bytes, bytes):
            raise RawEvidenceError("invalid_input")
        digest = source_sha256(raw_bytes)
        existing = None
        if resume:
            existing = self.store.find_resumable_import_run(
                source_sha256=digest,
                filename=filename or "upload",
                preserve_raw=preserve_raw,
            )
        run_id = existing["run_id"] if existing else uuid.uuid4().hex
        run = self.store.create_or_get_import_run(
            run_id=run_id,
            retry_key=f"o5b-import:{run_id}",
            source_sha256=digest,
            source_size_bytes=len(raw_bytes),
            filename=filename or "upload",
            media_type=media_type or "application/octet-stream",
            source_system="dashboard",
            source_kind="import_upload",
            source_scope="dashboard_upload",
            actor_id=actor_id,
            preserve_raw=preserve_raw,
            importer_version=IMPORTER_VERSION,
            parser_version=PARSER_VERSION,
            chunker_version=CHUNKER_VERSION,
        )
        return PreparedImportRun(run=run, source_sha256=digest)

    def capture(
        self,
        prepared: PreparedImportRun,
        raw_bytes: bytes,
        *,
        filename: str,
        media_type: str,
    ) -> dict[str, Any]:
        return self.store.create_or_reuse_import_evidence(
            raw_bytes,
            run_id=prepared.run_id,
            item_key="source_snapshot",
            source_system="dashboard",
            source_kind="import_upload",
            source_scope="dashboard_upload",
            filename=filename or "upload",
            media_type=media_type or "application/octet-stream",
            privacy_class="restricted_admin",
        )

    def update_run(self, run_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.store.update_import_run(run_id, **kwargs)

    def get_item(self, run_id: str, item_key: str) -> dict[str, Any] | None:
        return self.store.get_import_item(run_id, item_key)

    def list_items(self, run_id: str, *, prefix: str | None = None) -> list[dict[str, Any]]:
        return self.store.list_import_items(run_id, prefix=prefix)

    def upsert_item(self, run_id: str, item_key: str, **kwargs: Any) -> dict[str, Any]:
        return self.store.upsert_import_item(run_id, item_key, **kwargs)

    def create_lineage_intent(
        self,
        *,
        run_id: str,
        run_item_key: str,
        operation_key: str,
        memory_id: str,
        memory_mutation_id: str,
        evidence_id: str,
        revision_id: str,
        lineage_kind: str,
    ) -> dict[str, Any]:
        return self.store.create_lineage_intent(
            run_id=run_id,
            run_item_key=run_item_key,
            operation_key=operation_key,
            memory_id=memory_id,
            memory_mutation_id=memory_mutation_id,
            evidence_id=evidence_id,
            revision_id=revision_id,
            lineage_kind=lineage_kind,
        )

    def complete_lineage(self, lineage_id: str) -> dict[str, Any]:
        return self.store.update_lineage_status(lineage_id, status="complete")

    def list_lineage(
        self,
        *,
        run_id: str | None = None,
        status: str | None = None,
        memory_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.list_lineage(
            run_id=run_id,
            status=status,
            memory_id=memory_id,
        )

    def reconcile_pending_lineage(self, bucket_manager, *, run_id: str) -> list[dict[str, Any]]:
        """Complete only lineage intents proven by the O5B atomic marker."""

        reconciled: list[dict[str, Any]] = []
        pending = self.list_lineage(run_id=run_id, status="pending")
        for lineage in pending:
            inspection = bucket_manager.inspect_import_operation(
                lineage["operation_key"]
            )
            if inspection is None:
                reconciled.append(
                    self.store.update_lineage_status(
                        lineage["lineage_id"], status="needs_reconcile"
                    )
                )
                continue
            if (
                inspection.get("result_id") != lineage["memory_id"]
                or inspection.get("memory_mutation_id")
                != lineage["memory_mutation_id"]
                or inspection.get("operation_key") != lineage["operation_key"]
            ):
                reconciled.append(
                    self.store.update_lineage_status(
                        lineage["lineage_id"], status="provenance_broken"
                    )
                )
                continue
            marker = inspection.get("marker")
            if marker is not None:
                if (
                    marker.get("operation_key") != lineage["operation_key"]
                    or marker.get("memory_mutation_id")
                    != lineage["memory_mutation_id"]
                ):
                    reconciled.append(
                        self.store.update_lineage_status(
                            lineage["lineage_id"], status="provenance_broken"
                        )
                    )
                else:
                    reconciled.append(self.complete_lineage(lineage["lineage_id"]))
            elif inspection.get("status") == "applied":
                reconciled.append(
                    self.store.update_lineage_status(
                        lineage["lineage_id"], status="provenance_broken"
                    )
                )
        return reconciled


__all__ = [
    "CAPTURE_MODE",
    "CHUNKER_VERSION",
    "FIDELITY_LEVEL",
    "IMPORTER_VERSION",
    "PARSER_VERSION",
    "PreparedImportRun",
    "RAW_EVIDENCE_ROOT_ENV",
    "RawEvidenceImportCoordinator",
    "RawEvidenceError",
    "parse_capture_option",
    "source_sha256",
]
