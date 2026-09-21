"""Focused O5E restore verification and new-root-only publication tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from raw_evidence_backup import RawEvidenceBackupError, RawEvidenceBackupService
from raw_evidence_backup_authority import RawEvidenceBackupAuthority
from raw_evidence_restore import RawEvidenceRestoreError, RawEvidenceRestoreService
from raw_evidence_store import RawEvidenceStore


def _fixture(tmp_path: Path):
    live = RawEvidenceStore(tmp_path / "live")
    live.create(b"restore fixture", source_system="test", source_kind="item")
    key = X25519PrivateKey.generate()
    authority = RawEvidenceBackupAuthority.open(tmp_path / "backup", live_root=live.root)
    backup = RawEvidenceBackupService(live, authority, key.public_key())
    created = backup.create()
    restore = RawEvidenceRestoreService(live, authority, key)
    return live, authority, backup, restore, f"{created['backup_id']}.obrawbackup"


def test_restore_publishes_only_to_absent_new_root(tmp_path):
    live, authority, _, restore, bundle = _fixture(tmp_path)
    live_registry = live.registry_path.read_bytes()
    target = tmp_path / "restored"
    staged = restore.stage(bundle, target)
    assert not target.exists()
    result = restore.create_root(staged["stage_root"], target)
    assert result["status"] == "created"
    assert (target / "registry.sqlite3").exists()
    assert (target / "blobs" / "sha256").exists()
    assert live.registry_path.read_bytes() == live_registry
    assert authority.catalog_by_name(bundle)["status"] == "active"


def test_restore_rejects_existing_destination_and_keeps_live_untouched(tmp_path):
    live, _, _, restore, bundle = _fixture(tmp_path)
    target = tmp_path / "existing"
    target.mkdir()
    before = live.registry_path.read_bytes()
    with pytest.raises(RawEvidenceRestoreError, match="restore_destination_exists"):
        restore.create_root_from_bundle(bundle, target)
    assert live.registry_path.read_bytes() == before


def test_wrong_key_and_tampered_bundle_fail_closed(tmp_path):
    live, authority, backup, restore, bundle = _fixture(tmp_path)
    wrong = X25519PrivateKey.generate()
    with pytest.raises(RawEvidenceBackupError):
        backup.verify(bundle, wrong)
    assert restore.verify(bundle)["activatable"] is True
    path = authority.bundles_root / bundle
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 1
    path.write_bytes(raw)
    with pytest.raises(RawEvidenceBackupError):
        backup.verify(bundle, wrong)
    assert live.registry_path.exists()
