import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tarfile
import threading

import pytest

import offline_backup_bundle as bundle
from offline_backup_bundle import (
    BUNDLE_SUFFIX,
    MAGIC,
    BackupBundleError,
    capture_bundle,
    generate_test_keypair,
    inspect_bundle,
    load_backup_workspace,
    main,
    prepare_backup_workspace,
    restore_bundle,
    verify_bundle,
)


BASE_SHA = "81ceffb262f6a2af9c38dc6e32c6cc981ef11458"


def _workspace(tmp_path):
    return prepare_backup_workspace(tmp_path / "backup-workspace")


def _capture(workspace, public_key, **kwargs):
    return capture_bundle(
        workspace.root,
        public_key,
        ob_commit_sha=BASE_SHA,
        chunk_size=4096,
        **kwargs,
    )


def _snapshot(root):
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def _content_snapshot(root):
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def _sqlite(path, *, wal=False):
    connection = sqlite3.connect(path)
    if wal:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    connection.execute("PRAGMA user_version=7")
    connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value BLOB)")
    connection.execute("INSERT INTO items(value) VALUES (?)", (b"synthetic-row",))
    connection.commit()
    return connection


def _read_sqlite_values(path):
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return connection.execute("SELECT value FROM items ORDER BY id").fetchall()
    finally:
        connection.close()


def _mutate_bundle(path, operation):
    raw = bytearray(path.read_bytes())
    header_length = struct.unpack(">I", raw[len(MAGIC):len(MAGIC) + 4])[0]
    header_start = len(MAGIC) + 4
    payload_start = header_start + header_length
    operation(raw, header_start, header_length, payload_start)
    path.write_bytes(raw)


def _encrypted_malicious_archive(workspace, public_key, members):
    bundle_id = "a" * 32
    archive_path = workspace.temp_root / "malicious.tar"
    destination = workspace.bundles_root / f"{bundle_id}{BUNDLE_SUFFIX}"
    with tarfile.open(archive_path, "w", format=tarfile.PAX_FORMAT) as archive:
        for info, payload in members:
            if info.isreg():
                import io

                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            else:
                archive.addfile(info)
    bundle._encrypt_archive(
        archive_path,
        destination,
        public_key,
        capture_workspace_id=workspace.workspace_id,
        bundle_id=bundle_id,
        created_at="2026-08-01T00:00:00.000000+00:00",
        chunk_size=4096,
    )
    archive_path.unlink()
    return destination.name


def _regular_info(name):
    info = tarfile.TarInfo(name)
    info.mode = 0o600
    info.mtime = 0
    return info


def _recompute_manifest_digest(manifest):
    manifest = dict(manifest)
    manifest.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = hashlib.sha256(
        bundle._canonical_json_bytes(manifest)
    ).hexdigest()
    return manifest


def test_prepare_creates_fixed_identity_bound_workspace(tmp_path):
    workspace = _workspace(tmp_path)
    assert {path.name for path in workspace.root.iterdir()} == {
        "workspace-manifest.json",
        ".ombre-stage8h-g1b-backup",
        "source",
        "bundles",
        "restored",
        "reports",
        "temp",
    }
    assert load_backup_workspace(workspace.root) == workspace
    marker = json.loads((workspace.root / bundle.WORKSPACE_MARKER).read_text())
    marker["nonce"] = "0" * 64
    (workspace.root / bundle.WORKSPACE_MARKER).write_text(json.dumps(marker))
    with pytest.raises(BackupBundleError, match="workspace_invalid"):
        load_backup_workspace(workspace.root)


def test_workspace_rejects_nonworkspace_repository_and_nonempty_prepare(tmp_path):
    with pytest.raises(BackupBundleError, match="workspace_invalid"):
        load_backup_workspace(tmp_path)
    with pytest.raises(BackupBundleError, match="workspace_invalid"):
        prepare_backup_workspace(Path(bundle.__file__).resolve().parent)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "file").write_bytes(b"x")
    with pytest.raises(BackupBundleError, match="workspace_invalid"):
        prepare_backup_workspace(occupied)


def test_workspace_rejects_reparse_escape(tmp_path):
    workspace = _workspace(tmp_path)
    source = workspace.source_root
    source.rmdir()
    try:
        source.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(BackupBundleError, match="workspace_invalid"):
        load_backup_workspace(workspace.root)


def test_empty_source_capture_verify_restore_and_inspect_are_safe(tmp_path):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    result = _capture(workspace, public_key)
    bundle_path = workspace.bundles_root / result.bundle_name
    before = _snapshot(workspace.root)
    inspection = inspect_bundle(workspace.root, result.bundle_name)
    after = _snapshot(workspace.root)
    assert before == after
    assert inspection == {
        "status": "success",
        "authenticated": False,
        "metadata_trust": "unverified_header",
        "container_version": 1,
        "bundle_format_version": 1,
        "bundle_id": result.bundle_id,
        "capture_workspace_id": workspace.workspace_id,
        "created_at": inspection["created_at"],
        "encryption_profile": bundle.ENCRYPTION_PROFILE,
        "recipient_key_fingerprint": inspection["recipient_key_fingerprint"],
    }
    assert bundle_path.read_bytes().startswith(MAGIC)
    verified = verify_bundle(workspace.root, result.bundle_name, private_key)
    assert verified["entry_count"] == 0
    assert verified["authenticated"] is True
    assert verified["metadata_trust"] == "authenticated_bundle"
    assert not any(workspace.restored_root.iterdir())
    restored = restore_bundle(workspace.root, result.bundle_name, private_key)
    assert restored["entry_count"] == 0
    assert restored["authenticated"] is True
    assert restored["metadata_trust"] == "authenticated_bundle"
    assert (workspace.restored_root / result.bundle_id).is_dir()


def test_regular_bucket_blob_large_file_and_sorting_round_trip(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    expected = {
        "permanent/bucket.md": "synthetic bucket \u96ea\n".encode(),
        "assets/blobs/image.bin": bytes(range(256)) * 17,
        "support/z-large.bin": b"large" * 20000,
        "support/a-small.txt": b"small",
    }
    for relative, payload in expected.items():
        path = workspace.source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path):
        if path.is_relative_to(workspace.source_root):
            raise AssertionError("source files must be streamed")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    result = _capture(workspace, public_key)
    restore_bundle(workspace.root, result.bundle_name, private_key)
    root = workspace.restored_root / result.bundle_id
    assert {
        relative: (root / relative).read_bytes()
        for relative in expected
    } == expected


def test_sqlite_delete_journal_snapshot_round_trip(tmp_path):
    workspace = _workspace(tmp_path)
    database = workspace.source_root / "assets.sqlite3"
    connection = _sqlite(database)
    connection.close()
    before = _snapshot(workspace.source_root)
    private_key, public_key = generate_test_keypair()
    result = _capture(workspace, public_key)
    assert _snapshot(workspace.source_root) == before
    restore_bundle(workspace.root, result.bundle_name, private_key)
    restored = workspace.restored_root / result.bundle_id / "assets.sqlite3"
    assert _read_sqlite_values(restored) == [(b"synthetic-row",)]


def test_sqlite_wal_committed_rows_are_snapshotted_without_sidecars(tmp_path):
    workspace = _workspace(tmp_path)
    database = workspace.source_root / "remember-me" / "repository.sqlite3"
    database.parent.mkdir()
    writer = _sqlite(database, wal=True)
    writer.execute("INSERT INTO items(value) VALUES (?)", (b"wal-row",))
    writer.commit()
    assert Path(str(database) + "-wal").exists()
    before = _snapshot(workspace.source_root)
    private_key, public_key = generate_test_keypair()
    try:
        result = _capture(workspace, public_key)
        after = _snapshot(workspace.source_root)
    finally:
        writer.close()
    for relative in (
        "remember-me/repository.sqlite3",
        "remember-me/repository.sqlite3-wal",
    ):
        assert before[relative] == after[relative]
    assert before["remember-me/repository.sqlite3-shm"][:2] == after[
        "remember-me/repository.sqlite3-shm"
    ][:2]
    restore_bundle(workspace.root, result.bundle_name, private_key)
    restored_root = workspace.restored_root / result.bundle_id
    assert _read_sqlite_values(restored_root / "remember-me/repository.sqlite3") == [
        (b"synthetic-row",),
        (b"wal-row",),
    ]
    assert not list(restored_root.rglob("*-wal"))
    assert not list(restored_root.rglob("*-shm"))
    assert not list(restored_root.rglob("*-journal"))


def test_explicit_exclusions_do_not_enter_restored_tree(tmp_path):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    excluded = [
        ".dashboard_auth.json",
        ".backup_state.json",
        "private.pem",
        "writer.lock",
        "scratch.tmp",
    ]
    for name in excluded:
        (workspace.source_root / name).write_bytes(b"never archive")
    (workspace.source_root / "kept.txt").write_bytes(b"kept")
    result = _capture(workspace, public_key)
    assert result.exclusion_count == len(excluded)
    restore_bundle(workspace.root, result.bundle_name, private_key)
    restored = workspace.restored_root / result.bundle_id
    assert (restored / "kept.txt").read_bytes() == b"kept"
    assert all(not (restored / name).exists() for name in excluded)


def test_hardlinks_and_unsupported_source_types_fail_closed(tmp_path):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    del private_key
    source = workspace.source_root / "one.bin"
    source.write_bytes(b"content")
    try:
        os.link(source, workspace.source_root / "two.bin")
    except OSError:
        pytest.skip("hardlink creation is unavailable")
    with pytest.raises(BackupBundleError, match="source_unsupported"):
        _capture(workspace, public_key)
    assert not list(workspace.bundles_root.iterdir())


def test_regular_file_change_during_confirmation_fails_closed(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    source = workspace.source_root / "changing.bin"
    source.write_bytes(b"before")
    _, public_key = generate_test_keypair()
    original = bundle._hash_file
    changed = False

    def mutate_then_hash(path, *, chunk_size):
        nonlocal changed
        if path == source and not changed:
            changed = True
            path.write_bytes(b"after-value")
        return original(path, chunk_size=chunk_size)

    monkeypatch.setattr(bundle, "_hash_file", mutate_then_hash)
    with pytest.raises(BackupBundleError, match="source_changed"):
        _capture(workspace, public_key)
    assert not list(workspace.bundles_root.iterdir())


def test_sqlite_backup_failure_leaves_no_formal_bundle(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    database = workspace.source_root / "assets.sqlite3"
    _sqlite(database).close()
    _, public_key = generate_test_keypair()

    def fail(*args, **kwargs):
        raise sqlite3.DatabaseError("synthetic internal detail")

    monkeypatch.setattr(bundle, "_snapshot_sqlite", fail)
    with pytest.raises(BackupBundleError, match="internal_error"):
        _capture(workspace, public_key)
    assert not list(workspace.bundles_root.iterdir())


def test_atomic_bundle_publish_failure_leaves_no_formal_bundle(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    (workspace.source_root / "file").write_bytes(b"payload")
    _, public_key = generate_test_keypair()
    original = bundle.os.link

    def fail_bundle_publish(source, destination):
        if str(destination).endswith(BUNDLE_SUFFIX):
            raise OSError("synthetic path detail")
        return original(source, destination)

    monkeypatch.setattr(bundle.os, "link", fail_bundle_publish)
    with pytest.raises(BackupBundleError, match="bundle_invalid"):
        _capture(workspace, public_key)
    assert not list(workspace.bundles_root.iterdir())
    assert not list(workspace.temp_root.iterdir())


def test_bundle_fsync_failure_removes_just_published_bundle(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    (workspace.source_root / "file").write_bytes(b"payload")
    _, public_key = generate_test_keypair()

    def fail_directory_fsync(path):
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(bundle, "_fsync_directory", fail_directory_fsync)
    with pytest.raises(BackupBundleError, match="bundle_invalid"):
        _capture(workspace, public_key)
    assert not list(workspace.bundles_root.iterdir())
    assert not list(workspace.temp_root.iterdir())


@pytest.mark.parametrize(
    "operation,expected_status",
    [
        (
            lambda raw, hs, hl, ps: raw.__setitem__(hs + 10, raw[hs + 10] ^ 1),
            "bundle_invalid",
        ),
        (
            lambda raw, hs, hl, ps: raw.__setitem__(ps + 1, raw[ps + 1] ^ 1),
            "authentication_failed",
        ),
        (lambda raw, hs, hl, ps: raw.__setitem__(-1, raw[-1] ^ 1), "authentication_failed"),
        (lambda raw, hs, hl, ps: raw.__delitem__(slice(-9, None)), "authentication_failed"),
        (lambda raw, hs, hl, ps: raw.extend(b"garbage"), "authentication_failed"),
    ],
)
def test_container_tampering_and_length_changes_fail_closed(
    tmp_path, operation, expected_status
):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    (workspace.source_root / "file").write_bytes(b"payload")
    result = _capture(workspace, public_key)
    path = workspace.bundles_root / result.bundle_name
    _mutate_bundle(path, operation)
    with pytest.raises(BackupBundleError) as raised:
        verify_bundle(workspace.root, result.bundle_name, private_key)
    assert raised.value.status in {expected_status, "bundle_invalid"}
    assert not list(workspace.restored_root.iterdir())
    assert not list(workspace.temp_root.iterdir())


def test_wrapped_content_key_tampering_fails_authentication(tmp_path):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    (workspace.source_root / "file").write_bytes(b"payload")
    result = _capture(workspace, public_key)
    path = workspace.bundles_root / result.bundle_name

    def modify_wrapped_key(raw, header_start, header_length, payload_start):
        del payload_start
        header = json.loads(bytes(raw[header_start:header_start + header_length]))
        value = header["wrapped_content_key"]
        header["wrapped_content_key"] = ("A" if value[0] != "A" else "B") + value[1:]
        encoded = bundle._canonical_json_bytes(header)
        assert len(encoded) == header_length
        raw[header_start:header_start + header_length] = encoded

    _mutate_bundle(path, modify_wrapped_key)
    with pytest.raises(BackupBundleError, match="authentication_failed"):
        verify_bundle(workspace.root, result.bundle_name, private_key)


@pytest.mark.parametrize("corruption", ["missing", "extra", "hash", "size"])
def test_authenticated_manifest_entry_corruption_fails_closed(tmp_path, corruption):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    payload = b"x"
    entry = {
        "relative_path": "file.bin",
        "entry_type": "regular",
        "category": "supporting",
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    entries = [] if corruption == "extra" else [entry]
    manifest = bundle._build_manifest(
        workspace=workspace,
        bundle_id="a" * 32,
        created_at="2026-08-01T00:00:00.000000+00:00",
        ob_commit_sha=BASE_SHA,
        remember_me_version=bundle.EXPECTED_REMEMBER_ME_VERSION,
        recipient_fingerprint=bundle._public_key_fingerprint(public_key),
        entries=entries,
        exclusions=[],
    )
    members = [(_regular_info("manifest.json"), b"")]
    if corruption == "missing":
        pass
    elif corruption == "extra":
        members.append((_regular_info("data/extra.bin"), payload))
    elif corruption == "hash":
        manifest["entries"][0]["sha256"] = "0" * 64
        manifest = _recompute_manifest_digest(manifest)
        members.append((_regular_info("data/file.bin"), payload))
    else:
        manifest["entries"][0]["size_bytes"] = 2
        manifest["total_plaintext_bytes"] = 2
        manifest = _recompute_manifest_digest(manifest)
        members.append((_regular_info("data/file.bin"), payload))
    members[0] = (
        members[0][0],
        bundle._canonical_json_bytes(manifest),
    )
    name = _encrypted_malicious_archive(workspace, public_key, members)
    with pytest.raises(BackupBundleError, match="manifest_invalid"):
        verify_bundle(workspace.root, name, private_key)


def test_wrong_private_key_fails_before_plaintext_publish(tmp_path):
    workspace = _workspace(tmp_path)
    _, public_key = generate_test_keypair()
    wrong_private, _ = generate_test_keypair()
    (workspace.source_root / "file").write_bytes(b"payload")
    result = _capture(workspace, public_key)
    with pytest.raises(BackupBundleError, match="key_invalid"):
        restore_bundle(workspace.root, result.bundle_name, wrong_private)
    assert not list(workspace.restored_root.iterdir())
    assert not list(workspace.temp_root.iterdir())


@pytest.mark.parametrize(
    "member",
    [
        "../escape",
        "/absolute",
        "data/A.txt",
        "data/\u00e9.txt",
        "data/CON.txt",
    ],
)
def test_malicious_archive_paths_fail_closed(tmp_path, member):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    name = _encrypted_malicious_archive(
        workspace,
        public_key,
        [
            (_regular_info("manifest.json"), b"{}"),
            (_regular_info(member), b"payload"),
        ],
    )
    with pytest.raises(BackupBundleError, match="manifest_invalid"):
        verify_bundle(workspace.root, name, private_key)


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE])
def test_nonregular_archive_members_fail_closed(tmp_path, member_type):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    unsafe = tarfile.TarInfo("data/unsafe")
    unsafe.type = member_type
    unsafe.linkname = "manifest.json"
    name = _encrypted_malicious_archive(
        workspace,
        public_key,
        [(_regular_info("manifest.json"), b"{}"), (unsafe, b"")],
    )
    with pytest.raises(BackupBundleError, match="manifest_invalid"):
        verify_bundle(workspace.root, name, private_key)


def test_duplicate_and_case_colliding_archive_paths_fail_closed(tmp_path):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    for names in [
        ["data/same", "data/same"],
        ["data/Case", "data/case"],
        ["data/\u00e9", "data/e\u0301"],
    ]:
        name = _encrypted_malicious_archive(
            workspace,
            public_key,
            [(_regular_info("manifest.json"), b"{}")] + [
                (_regular_info(path), b"x") for path in names
            ],
        )
        with pytest.raises(BackupBundleError, match="manifest_invalid"):
            verify_bundle(workspace.root, name, private_key)
        (workspace.bundles_root / name).unlink()


def test_restore_rejects_any_existing_target_and_preserves_it(tmp_path):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    (workspace.source_root / "file").write_bytes(b"payload")
    result = _capture(workspace, public_key)
    target = workspace.restored_root / ("b" * 32)
    target.mkdir()
    existing = target / "existing"
    existing.write_bytes(b"unchanged")
    with pytest.raises(BackupBundleError, match="restore_target_invalid"):
        restore_bundle(
            workspace.root,
            result.bundle_name,
            private_key,
            restore_name="b" * 32,
        )
    assert existing.read_bytes() == b"unchanged"
    empty_name = "c" * 32
    empty = workspace.restored_root / empty_name
    empty.mkdir()
    with pytest.raises(BackupBundleError, match="restore_target_invalid"):
        restore_bundle(
            workspace.root,
            result.bundle_name,
            private_key,
            restore_name=empty_name,
        )
    assert empty.is_dir() and not any(empty.iterdir())


def test_restore_publish_failure_leaves_no_target_and_cleans_plaintext(
    tmp_path, monkeypatch
):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    (workspace.source_root / "file").write_bytes(b"payload")
    result = _capture(workspace, public_key)
    target_name = "b" * 32
    target = workspace.restored_root / target_name

    def fail_restore(source, destination):
        raise OSError("synthetic private path")

    monkeypatch.setattr(bundle, "_publish_directory_no_replace", fail_restore)
    with pytest.raises(BackupBundleError, match="restore_failed"):
        restore_bundle(
            workspace.root,
            result.bundle_name,
            private_key,
            restore_name=target_name,
        )
    assert not target.exists()
    assert not list(workspace.temp_root.iterdir())


def test_restore_fsync_failure_removes_just_published_target(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    (workspace.source_root / "file").write_bytes(b"payload")
    result = _capture(workspace, public_key)

    def fail_directory_fsync(path):
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(bundle, "_fsync_directory", fail_directory_fsync)
    with pytest.raises(BackupBundleError, match="restore_failed"):
        restore_bundle(workspace.root, result.bundle_name, private_key)
    assert not (workspace.restored_root / result.bundle_id).exists()
    assert not list(workspace.temp_root.iterdir())


def test_verify_never_publishes_and_cleans_temporary_plaintext(tmp_path):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    (workspace.source_root / "secret-content").write_bytes(b"synthetic-only")
    result = _capture(workspace, public_key)
    assert verify_bundle(workspace.root, result.bundle_name, private_key)["status"] == "success"
    assert not list(workspace.restored_root.iterdir())
    assert not list(workspace.temp_root.iterdir())


def test_bundle_is_portable_across_isolated_workspaces(tmp_path):
    first = prepare_backup_workspace(tmp_path / "first")
    second = prepare_backup_workspace(tmp_path / "second")
    private_key, public_key = generate_test_keypair()
    (first.source_root / "bucket.bin").write_bytes(b"portable synthetic bytes")
    result = _capture(first, public_key)
    copied = second.bundles_root / result.bundle_name
    copied.write_bytes((first.bundles_root / result.bundle_name).read_bytes())
    inspection = inspect_bundle(second.root, result.bundle_name)
    assert inspection["capture_workspace_id"] == first.workspace_id
    assert inspection["authenticated"] is False
    verified = verify_bundle(second.root, result.bundle_name, private_key)
    assert verified["capture_workspace_id"] == first.workspace_id
    assert verified["authenticated"] is True
    restored = restore_bundle(second.root, result.bundle_name, private_key)
    assert restored["capture_workspace_id"] == first.workspace_id
    assert restored["authenticated"] is True
    assert (
        second.restored_root / result.bundle_id / "bucket.bin"
    ).read_bytes() == b"portable synthetic bytes"


def test_portable_bundle_still_requires_valid_local_workspace(tmp_path):
    first = prepare_backup_workspace(tmp_path / "first")
    second = prepare_backup_workspace(tmp_path / "second")
    private_key, public_key = generate_test_keypair()
    result = _capture(first, public_key)
    (second.bundles_root / result.bundle_name).write_bytes(
        (first.bundles_root / result.bundle_name).read_bytes()
    )
    marker = json.loads((second.root / bundle.WORKSPACE_MARKER).read_text())
    marker["nonce"] = "0" * 64
    (second.root / bundle.WORKSPACE_MARKER).write_text(json.dumps(marker))
    operations = (
        lambda: inspect_bundle(second.root, result.bundle_name),
        lambda: verify_bundle(second.root, result.bundle_name, private_key),
        lambda: restore_bundle(second.root, result.bundle_name, private_key),
    )
    for operation in operations:
        with pytest.raises(BackupBundleError, match="workspace_invalid"):
            operation()


def test_bundle_cannot_be_read_from_outside_workspace_bundles(tmp_path):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    result = _capture(workspace, public_key)
    outside = tmp_path / result.bundle_name
    outside.write_bytes((workspace.bundles_root / result.bundle_name).read_bytes())
    for operation in (
        lambda: inspect_bundle(workspace.root, str(outside)),
        lambda: verify_bundle(workspace.root, str(outside), private_key),
        lambda: restore_bundle(workspace.root, str(outside), private_key),
    ):
        with pytest.raises(BackupBundleError, match="bundle_invalid"):
            operation()


@pytest.mark.parametrize(
    "field,value",
    [
        ("bundle_format_version", 2),
        ("bundle_id", "b" * 32),
        ("capture_workspace_id", "b" * 32),
        ("created_at", "2026-08-01T00:00:01.000000+00:00"),
        ("encryption_profile", "invalid-profile"),
        ("recipient_key_fingerprint", "x25519-sha256:" + "0" * 64),
    ],
)
def test_header_manifest_identity_mismatch_is_rejected(tmp_path, field, value):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    manifest = bundle._build_manifest(
        workspace=workspace,
        bundle_id="a" * 32,
        created_at="2026-08-01T00:00:00.000000+00:00",
        ob_commit_sha=BASE_SHA,
        remember_me_version=bundle.EXPECTED_REMEMBER_ME_VERSION,
        recipient_fingerprint=bundle._public_key_fingerprint(public_key),
        entries=[],
        exclusions=[],
    )
    manifest[field] = value
    manifest = _recompute_manifest_digest(manifest)
    name = _encrypted_malicious_archive(
        workspace,
        public_key,
        [
            (
                _regular_info("manifest.json"),
                bundle._canonical_json_bytes(manifest),
            )
        ],
    )
    with pytest.raises(BackupBundleError, match="manifest_invalid"):
        verify_bundle(workspace.root, name, private_key)


def test_syntactically_modified_header_is_untrusted_until_verify(tmp_path):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    result = _capture(workspace, public_key)
    path = workspace.bundles_root / result.bundle_name

    def modify_created_at(raw, header_start, header_length, payload_start):
        del payload_start
        header = json.loads(bytes(raw[header_start:header_start + header_length]))
        old = header["created_at"]
        position = old.index("+") - 1
        replacement = "1" if old[position] != "1" else "2"
        header["created_at"] = old[:position] + replacement + old[position + 1:]
        encoded = bundle._canonical_json_bytes(header)
        assert len(encoded) == header_length
        raw[header_start:header_start + header_length] = encoded

    _mutate_bundle(path, modify_created_at)
    inspection = inspect_bundle(workspace.root, result.bundle_name)
    assert inspection["status"] == "success"
    assert inspection["authenticated"] is False
    assert inspection["metadata_trust"] == "unverified_header"
    with pytest.raises(BackupBundleError, match="authentication_failed"):
        verify_bundle(workspace.root, result.bundle_name, private_key)


def test_early_file_change_during_later_capture_is_detected(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    first = workspace.source_root / "a.bin"
    second = workspace.source_root / "b.bin"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    _, public_key = generate_test_keypair()
    original = bundle._copy_regular_file_stable

    def copy_then_change(source, destination, *, chunk_size):
        result = original(source, destination, chunk_size=chunk_size)
        if source == second:
            first.write_bytes(b"changed after its copy")
        return result

    monkeypatch.setattr(bundle, "_copy_regular_file_stable", copy_then_change)
    with pytest.raises(BackupBundleError, match="source_changed"):
        _capture(workspace, public_key)
    assert not list(workspace.bundles_root.iterdir())


@pytest.mark.parametrize(
    "change",
    ["add", "delete", "rename", "excluded_add", "excluded_delete", "type"],
)
def test_final_inventory_rejects_whole_source_structure_changes(
    tmp_path, monkeypatch, change
):
    workspace = _workspace(tmp_path)
    source = workspace.source_root / "source.bin"
    source.write_bytes(b"source")
    excluded = workspace.source_root / "secret.pem"
    if change == "excluded_delete":
        excluded.write_bytes(b"synthetic excluded")
    _, public_key = generate_test_keypair()
    original = bundle._capture_source

    def capture_then_change(source_root, staging_root, inventory, *, chunk_size):
        result = original(
            source_root,
            staging_root,
            inventory,
            chunk_size=chunk_size,
        )
        if change == "add":
            (source_root / "added.bin").write_bytes(b"added")
        elif change == "delete":
            source.unlink()
        elif change == "rename":
            source.rename(source_root / "renamed.bin")
        elif change == "excluded_add":
            excluded.write_bytes(b"synthetic excluded")
        elif change == "excluded_delete":
            excluded.unlink()
        else:
            source.unlink()
            source.mkdir()
        return result

    monkeypatch.setattr(bundle, "_capture_source", capture_then_change)
    with pytest.raises(BackupBundleError, match="source_changed"):
        _capture(workspace, public_key)
    assert not list(workspace.bundles_root.iterdir())


def test_sqlite_commit_after_snapshot_before_final_inventory_is_detected(
    tmp_path, monkeypatch
):
    workspace = _workspace(tmp_path)
    database = workspace.source_root / "assets.sqlite3"
    writer = _sqlite(database, wal=True)
    _, public_key = generate_test_keypair()
    original = bundle._capture_source

    def capture_then_commit(source_root, staging_root, inventory, *, chunk_size):
        result = original(
            source_root,
            staging_root,
            inventory,
            chunk_size=chunk_size,
        )
        writer.execute("INSERT INTO items(value) VALUES (?)", (b"late",))
        writer.commit()
        return result

    monkeypatch.setattr(bundle, "_capture_source", capture_then_commit)
    try:
        with pytest.raises(BackupBundleError, match="source_changed"):
            _capture(workspace, public_key)
    finally:
        writer.close()
    assert not list(workspace.bundles_root.iterdir())


def test_wal_appearance_after_snapshot_is_detected(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    database = workspace.source_root / "assets.sqlite3"
    _sqlite(database).close()
    _, public_key = generate_test_keypair()
    original = bundle._capture_source

    def capture_then_add_wal(source_root, staging_root, inventory, *, chunk_size):
        result = original(
            source_root,
            staging_root,
            inventory,
            chunk_size=chunk_size,
        )
        Path(str(database) + "-wal").write_bytes(b"synthetic late wal")
        return result

    monkeypatch.setattr(bundle, "_capture_source", capture_then_add_wal)
    with pytest.raises(BackupBundleError, match="source_changed"):
        _capture(workspace, public_key)
    assert not list(workspace.bundles_root.iterdir())


def test_existing_bundle_and_racing_bundle_are_never_overwritten(
    tmp_path, monkeypatch
):
    workspace = _workspace(tmp_path)
    (workspace.source_root / "file").write_bytes(b"payload")
    _, public_key = generate_test_keypair()
    fixed_id = "d" * 32
    existing = workspace.bundles_root / f"{fixed_id}{BUNDLE_SUFFIX}"
    existing.write_bytes(b"existing bundle bytes")
    monkeypatch.setattr(bundle.secrets, "token_hex", lambda count: fixed_id)
    with pytest.raises(BackupBundleError, match="bundle_invalid"):
        _capture(workspace, public_key)
    assert existing.read_bytes() == b"existing bundle bytes"

    existing.unlink()
    original_link = bundle.os.link

    def racing_link(source, destination):
        Path(destination).write_bytes(b"racing bundle bytes")
        return original_link(source, destination)

    monkeypatch.setattr(bundle.os, "link", racing_link)
    with pytest.raises(BackupBundleError, match="bundle_invalid"):
        _capture(workspace, public_key)
    assert existing.read_bytes() == b"racing bundle bytes"


def test_restore_lock_serializes_compliant_publishers(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    (workspace.source_root / "file").write_bytes(b"payload")
    result = _capture(workspace, public_key)
    entered = threading.Event()
    release = threading.Event()
    original = bundle._decrypt_and_validate

    def blocking_decrypt(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(bundle, "_decrypt_and_validate", blocking_decrypt)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(
            restore_bundle,
            workspace.root,
            result.bundle_name,
            private_key,
        )
        assert entered.wait(timeout=10)
        with pytest.raises(BackupBundleError, match="restore_target_invalid"):
            restore_bundle(workspace.root, result.bundle_name, private_key)
        release.set()
        assert first.result(timeout=10)["status"] == "success"
    assert (workspace.restored_root / result.bundle_id / "file").read_bytes() == b"payload"


def test_restore_base_exception_propagates_and_releases_lock(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    result = _capture(workspace, public_key)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(bundle, "_decrypt_and_validate", interrupt)
    with pytest.raises(KeyboardInterrupt):
        restore_bundle(workspace.root, result.bundle_name, private_key)
    assert not (workspace.temp_root / ".restore-operation.lock").exists()
    assert not (workspace.restored_root / result.bundle_id).exists()


def test_fresh_randomness_produces_distinct_ciphertext_but_equal_restores(tmp_path):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    (workspace.source_root / "bucket").write_bytes(b"same logical source")
    first = _capture(workspace, public_key)
    second = _capture(workspace, public_key)
    assert (workspace.bundles_root / first.bundle_name).read_bytes() != (
        workspace.bundles_root / second.bundle_name
    ).read_bytes()
    restore_bundle(workspace.root, first.bundle_name, private_key)
    restore_bundle(workspace.root, second.bundle_name, private_key)
    assert _content_snapshot(workspace.restored_root / first.bundle_id) == _content_snapshot(
        workspace.restored_root / second.bundle_id
    )


def test_capture_restore_second_capture_logical_round_trip(tmp_path):
    first = prepare_backup_workspace(tmp_path / "first")
    private_key, public_key = generate_test_keypair()
    (first.source_root / "bucket.md").write_bytes(b"round-trip")
    database = first.source_root / "assets.sqlite3"
    _sqlite(database).close()
    first_capture = _capture(first, public_key)
    restore_bundle(first.root, first_capture.bundle_name, private_key)
    restored = first.restored_root / first_capture.bundle_id

    second = prepare_backup_workspace(tmp_path / "second")
    for path in restored.rglob("*"):
        relative = path.relative_to(restored)
        destination = second.source_root / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(path.read_bytes())
    second_capture = _capture(second, public_key)
    restore_bundle(second.root, second_capture.bundle_name, private_key)
    second_restored = second.restored_root / second_capture.bundle_id
    assert (second_restored / "bucket.md").read_bytes() == b"round-trip"
    assert _read_sqlite_values(second_restored / "assets.sqlite3") == [
        (b"synthetic-row",)
    ]


def test_manifest_and_public_results_do_not_disclose_source_content_or_paths(tmp_path):
    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    sensitive = "synthetic-private-bucket-body"
    (workspace.source_root / "bucket.md").write_text(sensitive)
    result = _capture(workspace, public_key)
    public_values = json.dumps(
        [result.to_dict(), inspect_bundle(workspace.root, result.bundle_name)]
    )
    assert sensitive not in public_values
    assert str(workspace.root) not in public_values
    operation = workspace.temp_root / "manifest-check"
    operation.mkdir()
    try:
        validated = bundle._decrypt_and_validate(
            workspace,
            workspace.bundles_root / result.bundle_name,
            private_key,
            operation,
            chunk_size=4096,
        )
        manifest_json = json.dumps(validated["manifest"], sort_keys=True)
        assert sensitive not in manifest_json
        assert str(workspace.root) not in manifest_json
        assert "private_key" not in manifest_json.casefold()
    finally:
        bundle._safe_rmtree(workspace, operation)


def test_private_key_extension_is_ignored_and_no_private_key_is_tracked():
    gitignore = (Path(bundle.__file__).parent / ".gitignore").read_text()
    assert "*.obx25519-private" in gitignore.splitlines()
    assert not any(
        path.suffix == bundle.PRIVATE_KEY_SUFFIX
        for path in Path(bundle.__file__).parent.rglob("*")
        if ".git" not in path.parts
    )


@pytest.mark.parametrize("raised", [KeyboardInterrupt(), SystemExit(9)])
def test_cli_does_not_swallow_base_exception(tmp_path, monkeypatch, raised):
    workspace = _workspace(tmp_path)

    def fail(*args, **kwargs):
        raise raised

    monkeypatch.setattr(bundle, "inspect_bundle", fail)
    with pytest.raises(type(raised)):
        main(["inspect", str(workspace.root), f"{'a' * 32}{BUNDLE_SUFFIX}"])


def test_cli_maps_ordinary_failure_to_one_redacted_json_line(
    tmp_path, monkeypatch, capsys
):
    workspace = _workspace(tmp_path)

    def fail(*args, **kwargs):
        raise OSError(f"private path {workspace.root}")

    monkeypatch.setattr(bundle, "inspect_bundle", fail)
    exit_code = main(
        ["inspect", str(workspace.root), f"{'a' * 32}{BUNDLE_SUFFIX}"]
    )
    captured = capsys.readouterr()
    assert exit_code == bundle._EXIT_CODES["internal_error"]
    assert captured.out == '{"status": "internal_error"}\n'
    assert captured.err == ""
    assert str(workspace.root) not in captured.out


def test_module_import_has_no_server_or_network_side_effect(monkeypatch):
    del monkeypatch
    assert "server" not in bundle.__dict__
    assert "backup_export" not in bundle.__dict__
    assert "httpx" not in sys.modules or "httpx" not in bundle.__dict__


def test_capture_does_not_use_network_or_legacy_payload_export(tmp_path, monkeypatch):
    import backup_export
    import socket

    workspace = _workspace(tmp_path)
    private_key, public_key = generate_test_keypair()
    del private_key
    (workspace.source_root / "file").write_bytes(b"payload")

    def forbidden(*args, **kwargs):
        raise AssertionError("forbidden collaborator called")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(backup_export, "backup_payload_json", forbidden)
    result = _capture(workspace, public_key)
    assert result.status == "success"
