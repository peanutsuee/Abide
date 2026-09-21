"""Focused O5E repository identity and restore-epoch authority tests."""

from __future__ import annotations

import sqlite3

import pytest

from raw_evidence_backup_authority import (
    BackupAuthorityError,
    RawEvidenceBackupAuthority,
)
from raw_evidence_store import RawEvidenceError, RawEvidenceStore


def test_repository_id_is_stable_and_first_bind_is_one_way(tmp_path):
    live = RawEvidenceStore(tmp_path / "live")
    authority = RawEvidenceBackupAuthority.open(tmp_path / "backup", live_root=live.root)
    repository_id = authority.repository_id()
    assert live.backup_repository_id() is None
    assert live.bind_backup_repository(repository_id) == repository_id
    assert live.backup_repository_id() == repository_id
    with pytest.raises(RawEvidenceError, match="backup_repository_mismatch"):
        live.bind_backup_repository("f" * 32)


def test_pending_revocation_is_idempotent_and_monotonic(tmp_path):
    authority = RawEvidenceBackupAuthority.open(tmp_path / "backup")
    evidence_id = "1" * 32
    operation_id = "2" * 32
    first = authority.begin_revocation(
        operation_id,
        target_evidence_id=evidence_id,
        reason="user_redaction",
    )
    retry = authority.begin_revocation(
        "3" * 32,
        target_evidence_id=evidence_id,
        reason="retry",
    )
    assert first["allocated_epoch"] == 1
    assert retry["operation_id"] == operation_id
    assert authority.current_restore_epoch() == 1
    authority.apply_revocation(operation_id)
    assert authority.pending_revocations() == []


def test_corrupt_authority_fails_closed(tmp_path):
    authority = RawEvidenceBackupAuthority.open(tmp_path / "backup")
    with sqlite3.connect(authority.database_path) as conn:
        conn.execute("DROP TABLE authority")
    with pytest.raises(BackupAuthorityError, match="backup_authority_corrupt"):
        authority.current_restore_epoch()


def test_backup_root_symlink_is_rejected_when_supported(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink unavailable")
    with pytest.raises(BackupAuthorityError, match="backup_repository_path_invalid"):
        RawEvidenceBackupAuthority.open(link)
