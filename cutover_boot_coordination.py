"""Read-only bridge between D2 coordination and the server boot path.

The public server must not import the operator transition controller.  This
module exposes only the narrow, redacted RM_PREPARED proof that the runtime
needs to recognize a controlled restart.  It performs no writes and never
reads or returns lease capability material.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from asset_authority import AssetAuthority
from asset_cutover_state import (
    CutoverSnapshot,
    CutoverState,
    ExpiredRmAcceptanceRecoveryBootProof,
    MigrationIdentity,
    RmPreparedBootCoordination,
)


D2_TRANSITION_SCHEMA_VERSION = 1
RM_PREPARED_PHASE = "RM_PREPARED"
RM_FROZEN_ACCEPTANCE_PHASE = "RM_FROZEN_ACCEPTANCE"
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:@/-]{1,160}\Z")


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    enum_value = getattr(value, "value", None)
    return enum_value if isinstance(enum_value, str) else None


def _identity_from_record(value: Any) -> MigrationIdentity | None:
    if not isinstance(value, Mapping):
        return None
    migration_key = value.get("migration_key")
    migration_version = value.get("migration_version")
    source_identity = value.get("source_identity")
    source_generation = value.get("source_generation")
    target_identity = value.get("target_identity")
    if (
        not isinstance(migration_key, str)
        or not migration_key
        or type(migration_version) is not int
        or migration_version < 1
        or not isinstance(source_identity, str)
        or not source_identity
        or type(source_generation) is not int
        or source_generation < 0
        or not isinstance(target_identity, str)
        or not target_identity
    ):
        return None
    return MigrationIdentity(
        migration_key=migration_key,
        migration_version=migration_version,
        source_identity=source_identity,
        source_generation=source_generation,
        target_identity=target_identity,
    )


def is_rm_prepared_coordination_record_active(
    record: Mapping[str, Any] | None,
    snapshot: CutoverSnapshot,
) -> bool:
    """Return whether one record is exactly current for RM_PREPARED boot."""

    if not isinstance(record, Mapping):
        return False
    if (
        snapshot.state is not CutoverState.FROZEN_READY_FOR_RM_SWITCH
        or snapshot.authority is not AssetAuthority.LEGACY
        or snapshot.freeze_status != "active"
        or not snapshot.lease_id
        or snapshot.migration_identity is None
    ):
        return False
    if (
        record.get("phase") != RM_PREPARED_PHASE
        or _text(record.get("expected_authority")) != AssetAuthority.RM.value
        or _text(record.get("authority_before")) != AssetAuthority.LEGACY.value
        or _text(record.get("authority_after")) != AssetAuthority.RM.value
        or _text(record.get("state_before")) != CutoverState.FROZEN_READY_FOR_RM_SWITCH.value
        or _text(record.get("state_after")) != CutoverState.FROZEN_RM_ACCEPTANCE.value
        or record.get("lease_id") != snapshot.lease_id
    ):
        return False
    return _identity_from_record(record.get("migration_identity")) == snapshot.migration_identity


def _proof_from_record(record: Mapping[str, Any], snapshot: CutoverSnapshot) -> RmPreparedBootCoordination | None:
    if not is_rm_prepared_coordination_record_active(record, snapshot):
        return None
    try:
        return RmPreparedBootCoordination(
            phase=RM_PREPARED_PHASE,
            expected_authority=AssetAuthority.RM,
            authority_before=AssetAuthority.LEGACY,
            authority_after=AssetAuthority.RM,
            state_before=CutoverState.FROZEN_READY_FOR_RM_SWITCH,
            state_after=CutoverState.FROZEN_RM_ACCEPTANCE,
            lease_id=str(record["lease_id"]),
            migration_identity=snapshot.migration_identity,
        )
    except (KeyError, TypeError, ValueError):
        return None


def read_rm_prepared_boot_coordination(
    state_db: str | Path,
    snapshot: CutoverSnapshot,
) -> RmPreparedBootCoordination | None:
    """Read a validated RM_PREPARED proof without mutating the state DB."""

    if not isinstance(snapshot, CutoverSnapshot):
        return None
    try:
        path = Path(state_db).expanduser().resolve(strict=False)
        if not path.is_file():
            return None
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    try:
        row = connection.execute(
            "SELECT schema_version, payload_json FROM d2_transition_record WHERE singleton = 1"
        ).fetchone()
        if row is None or int(row["schema_version"]) != D2_TRANSITION_SCHEMA_VERSION:
            return None
        payload = json.loads(row["payload_json"])
        return _proof_from_record(payload, snapshot) if isinstance(payload, Mapping) else None
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return None
    finally:
        connection.close()


def _safe_identity(value: Any) -> bool:
    return isinstance(value, str) and _SAFE_ID.fullmatch(value) is not None


def _read_expired_rm_acceptance_proof(
    record: Mapping[str, Any],
    snapshot: CutoverSnapshot,
    freeze: sqlite3.Row,
) -> ExpiredRmAcceptanceRecoveryBootProof | None:
    if (
        snapshot.state is not CutoverState.FROZEN_RM_ACCEPTANCE
        or snapshot.authority is not AssetAuthority.RM
        or snapshot.freeze_status != "expired"
        or not snapshot.rm_available
        or not snapshot.lease_id
        or snapshot.migration_identity is None
        or not isinstance(record, Mapping)
    ):
        return None
    required = {
        "phase",
        "transition_identity",
        "expected_authority",
        "authority_before",
        "authority_after",
        "state_before",
        "state_after",
        "lease_id",
        "migration_identity",
        "readiness",
        "acceptance",
        "legacy_acceptance",
        "failures",
        "warnings",
        "freeze_released_at",
    }
    if not required.issubset(record):
        return None
    if (
        record.get("phase") != RM_FROZEN_ACCEPTANCE_PHASE
        or _text(record.get("expected_authority")) != AssetAuthority.RM.value
        or _text(record.get("authority_before")) != AssetAuthority.LEGACY.value
        or _text(record.get("authority_after")) != AssetAuthority.RM.value
        or _text(record.get("state_before")) != CutoverState.FROZEN_READY_FOR_RM_SWITCH.value
        or _text(record.get("state_after")) != CutoverState.FROZEN_RM_ACCEPTANCE.value
        or record.get("lease_id") != snapshot.lease_id
        or record.get("lease_id") != str(freeze["lease_id"])
        or type(freeze["generation"]) is not int
        or freeze["generation"] < 0
        or record.get("freeze_released_at") is not None
        or not _safe_identity(record.get("transition_identity"))
    ):
        return None
    if not isinstance(record.get("acceptance"), Mapping) or not isinstance(
        record.get("legacy_acceptance"), Mapping
    ):
        return None
    if not isinstance(record.get("failures"), list) or not all(
        isinstance(value, str) for value in record["failures"]
    ):
        return None
    if not isinstance(record.get("warnings"), list) or not all(
        isinstance(value, str) for value in record["warnings"]
    ):
        return None
    readiness = record.get("readiness")
    if not isinstance(readiness, Mapping):
        return None
    hard_gates = readiness.get("hard_gates")
    if (
        readiness.get("status") != "PASS"
        or readiness.get("READY_FOR_AUTHORITY_SWITCH") != "YES"
        or not _safe_identity(readiness.get("evidence_identity"))
        or not isinstance(hard_gates, Mapping)
        or not hard_gates
        or any(value != "PASS" for value in hard_gates.values())
    ):
        return None
    identity = _identity_from_record(record.get("migration_identity"))
    if identity is None or identity != snapshot.migration_identity:
        return None
    effective_lease_generation = int(freeze["generation"])
    if "lease_generation" in record:
        if (
            type(record["lease_generation"]) is not int
            or record["lease_generation"] < 0
            or record["lease_generation"] != effective_lease_generation
        ):
            return None
        effective_lease_generation = int(record["lease_generation"])
    try:
        return ExpiredRmAcceptanceRecoveryBootProof(
            phase=RM_FROZEN_ACCEPTANCE_PHASE,
            expected_authority=AssetAuthority.RM,
            state=CutoverState.FROZEN_RM_ACCEPTANCE,
            lease_id=str(record["lease_id"]),
            lease_generation=effective_lease_generation,
            transition_identity=str(record["transition_identity"]),
            migration_identity=identity,
        )
    except (KeyError, TypeError, ValueError):
        return None


def read_expired_rm_acceptance_recovery_boot_proof(
    state_db: str | Path,
    snapshot: CutoverSnapshot,
) -> ExpiredRmAcceptanceRecoveryBootProof | None:
    """Read a complete expired-D2 proof without writes or capability access."""

    if not isinstance(snapshot, CutoverSnapshot):
        return None
    try:
        path = Path(state_db).expanduser().resolve(strict=False)
        if not path.is_file():
            return None
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    try:
        row = connection.execute(
            "SELECT schema_version, payload_json FROM d2_transition_record WHERE singleton = 1"
        ).fetchone()
        freeze = connection.execute(
            "SELECT lease_id, generation FROM cutover_freeze WHERE singleton = 1"
        ).fetchone()
        if row is None or freeze is None or int(row["schema_version"]) != D2_TRANSITION_SCHEMA_VERSION:
            return None
        payload = json.loads(row["payload_json"])
        return (
            _read_expired_rm_acceptance_proof(payload, snapshot, freeze)
            if isinstance(payload, Mapping)
            else None
        )
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return None
    finally:
        connection.close()


__all__ = [
    "D2_TRANSITION_SCHEMA_VERSION",
    "ExpiredRmAcceptanceRecoveryBootProof",
    "RM_FROZEN_ACCEPTANCE_PHASE",
    "RM_PREPARED_PHASE",
    "is_rm_prepared_coordination_record_active",
    "read_expired_rm_acceptance_recovery_boot_proof",
    "read_rm_prepared_boot_coordination",
]
