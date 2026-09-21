"""Focused O5E backup, authority, retention, and fencing tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from raw_evidence_backup import RawEvidenceBackupError, RawEvidenceBackupService
from raw_evidence_backup_authority import (
    BackupRetention,
    RawEvidenceBackupAuthority,
)
from raw_evidence_lifecycle import RawEvidenceLifecycle
from raw_evidence_store import RawEvidenceError, RawEvidenceStore


def _fixture(tmp_path: Path):
    live = RawEvidenceStore(tmp_path / "live")
    evidence = live.create(b"backup fixture", source_system="test", source_kind="item")
    key = X25519PrivateKey.generate()
    authority = RawEvidenceBackupAuthority.open(
        tmp_path / "backup",
        live_root=live.root,
    )
    service = RawEvidenceBackupService(live, authority, key.public_key())
    return live, evidence, key, authority, service


def test_retention_defaults_to_seven_and_is_bounded(monkeypatch):
    monkeypatch.delenv("OMBRE_RAW_EVIDENCE_BACKUP_RETENTION_DAYS", raising=False)
    assert BackupRetention.from_env().days == 7
    monkeypatch.setenv("OMBRE_RAW_EVIDENCE_BACKUP_RETENTION_DAYS", "1")
    assert BackupRetention.from_env().days == 1
    monkeypatch.setenv("OMBRE_RAW_EVIDENCE_BACKUP_RETENTION_DAYS", "30")
    assert BackupRetention.from_env().days == 30
    for value in ("0", "31", "not-a-number"):
        monkeypatch.setenv("OMBRE_RAW_EVIDENCE_BACKUP_RETENTION_DAYS", value)
        with pytest.raises(RawEvidenceError, match="backup_retention_invalid"):
            BackupRetention.from_env()


def test_backup_create_verify_is_encrypted_and_catalogued(tmp_path):
    live, _, key, authority, service = _fixture(tmp_path)
    result = service.create()
    bundle = authority.bundles_root / f"{result['backup_id']}.obrawbackup"
    assert bundle.exists()
    assert b"backup fixture" not in bundle.read_bytes()
    verified = service.verify(bundle.name, key)
    assert verified["activatable"] is True
    assert verified["registry_schema"] == 6
    assert authority.catalog_by_name(bundle.name)["status"] == "active"
    assert live.backup_claim_status()["backup_state"] == "released"


def test_claim_is_single_owner_and_stale_claim_is_recoverable(tmp_path):
    live = RawEvidenceStore(tmp_path / "live")
    live.acquire_backup_claim("1" * 32, now="2026-01-01T00:00:00+00:00", ttl_seconds=300)
    with pytest.raises(RawEvidenceError, match="backup_busy"):
        live.acquire_backup_claim("2" * 32, now="2026-01-01T00:01:00+00:00", ttl_seconds=300)
    recovered = live.acquire_backup_claim(
        "2" * 32,
        now="2026-01-01T00:05:00+00:00",
        ttl_seconds=300,
    )
    assert recovered["operation_id"] == "2" * 32


def test_redaction_bumps_epoch_and_revokes_old_backup(tmp_path, monkeypatch):
    live, evidence, key, authority, service = _fixture(tmp_path)
    monkeypatch.setenv("OMBRE_RAW_EVIDENCE_BACKUP_ROOT", str(authority.root))
    result = service.create()
    old_name = f"{result['backup_id']}.obrawbackup"
    redacted = RawEvidenceLifecycle(live).redact(evidence["evidence_id"])
    assert redacted["state"] == "tombstoned"
    assert authority.current_restore_epoch() == 1
    verification = service.verify(old_name, key)
    assert verification["activatable"] is False
    assert verification["activation_error"] == "restore_epoch_revoked"


def test_expired_bundle_prune_is_exact_and_bounded(tmp_path):
    _, _, key, authority, service = _fixture(tmp_path)
    created = datetime.now(timezone.utc) - timedelta(days=8)
    result = service.create(clock=lambda: created)
    name = f"{result['backup_id']}.obrawbackup"
    pruned = service.prune(now=(created + timedelta(days=8)).isoformat(timespec="seconds"))
    assert pruned["pruned"] == 1
    assert not (authority.bundles_root / name).exists()
    assert authority.catalog_by_name(name)["status"] == "pruned"
    # An operator prune never sweeps an unknown file.
    unknown = authority.bundles_root / "unknown.obrawbackup"
    unknown.write_bytes(b"keep")
    service.prune(now=(created + timedelta(days=9)).isoformat(timespec="seconds"))
    assert unknown.exists()
