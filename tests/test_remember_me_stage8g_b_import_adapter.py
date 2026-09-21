import hashlib
import io
import json
import os
import sys
from pathlib import Path
import tempfile

import pytest
from PIL import Image

from asset_store import AssetStore
from remember_me.core import (
    AssetUnavailable,
    BeginAssetVerificationRequest,
    CompleteAssetVerificationRequest,
    GetAssetRequest,
    ImportAssetDisposition,
    ImportAssetRequest,
    ImportAssetResult,
    ImportAssetTag,
    ListAssetVerificationPageRequest,
    ReindexEmbeddingsRequest,
    RememberMeCore,
    VerifyAssetBlobRequest,
)
from remember_me.metadata import PROJECT_VERSION
from remember_me_adapter import RememberMeAdapter
from remember_me_import_adapter import (
    LegacyAssetImportAdapter,
    LegacyAssetImportAdapterError,
    LegacyAssetImportDisposition,
    LegacyAssetImportErrorCode,
    LegacyAssetImportFixtureContext,
    LegacyAssetImportRequest,
    _FIXTURE_MARKER_NAME,
    _is_within,
    _validate_fixture_roots,
    create_legacy_asset_import_fixture_context,
)


ROOT = Path(__file__).resolve().parent.parent
RM_VERSION = "0.1.0.dev7"
RM_COMMIT = "a00ea991442d7581a3856b178525a8e77da833fe"
RM_ARCHIVE_SHA256 = (
    "80a0b334f08db19c95c053537dec484be645f29fcf67898037e6641224012214"
)
RM_ARCHIVE_URL = (
    "https://github.com/peanutsuee/Remember-Me/releases/download/"
    "v0.1.0-dev.7-public.1/Remember-Me-0.1.0.dev7-public.1-"
    "a00ea991442d7581a3856b178525a8e77da833fe.tar.gz"
)
CREATED_AT = "2026-06-01T01:02:03+00:00"
TAG_CREATED_AT = "2026-06-01T01:03:04+00:00"
UPDATED_AT = "2026-06-02T05:06:07+00:00"


def _image_bytes(image_format):
    output = io.BytesIO()
    image = Image.new("RGB", (7, 5), "green")
    save_kwargs = {}
    if image_format == "WEBP":
        save_kwargs["lossless"] = True
    image.save(output, format=image_format, **save_kwargs)
    image.close()
    return output.getvalue()


def _persist_image(store, image_format="PNG", asset_id="a" * 32):
    mime_type = "image/png" if image_format == "PNG" else "image/jpeg"
    extension = ".png" if image_format == "PNG" else ".jpg"
    source = _image_bytes(image_format)
    source_path = store.create_temp_path(extension)
    source_path.write_bytes(source)
    asset = store.persist_upload(
        source_path,
        hashlib.sha256(source).hexdigest(),
        len(source),
        "legacy{}".format(extension),
        mime_type,
        require_image=True,
    )
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE assets
            SET asset_id = ?, created_at = ?, updated_at = ?,
                title = ?, description = ?
            WHERE asset_id = ?
            """,
            (
                asset_id,
                CREATED_AT,
                UPDATED_AT,
                "Legacy title",
                "Legacy description",
                asset["asset_id"],
            ),
        )
        conn.execute(
            """
            INSERT INTO asset_tags (
                asset_id, tag_normalized, tag_display, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (asset_id, "legacy tag", "Legacy Tag", TAG_CREATED_AT),
        )
    return store.get_import_record(asset_id)


def _replace_legacy_blob(
    store,
    asset_id,
    content,
    *,
    mime_type,
    extension,
    width=7,
    height=5,
):
    digest = hashlib.sha256(content).hexdigest()
    target = store.data_root / "assets" / digest[:2] / f"{digest}{extension}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE assets
            SET source_sha256 = ?, stored_sha256 = ?, stored_relpath = ?,
                original_filename = ?, mime_type = ?, decoded_bytes = ?,
                stored_bytes = ?, width = ?, height = ?
            WHERE asset_id = ?
            """,
            (
                digest,
                digest,
                target.relative_to(store.data_root).as_posix(),
                "legacy{}".format(extension),
                mime_type,
                len(content),
                len(content),
                width,
                height,
                asset_id,
            ),
        )
    return store.get_import_record(asset_id)


def _assert_zero_write(rm_root, legacy_root, rm_before, legacy_before):
    assert _snapshot(rm_root) == rm_before
    assert _snapshot(legacy_root) == legacy_before


def _adapter(tmp_path, legacy_store, runtime=None):
    fixture = create_legacy_asset_import_fixture_context(
        tmp_path,
        legacy_root=legacy_store.data_root,
        rm_root=tmp_path / "rm",
    )
    fixture.bind_legacy_store(legacy_store)
    if runtime is None:
        runtime = fixture.create_runtime()
    else:
        fixture.bind_core(runtime.service)
    adapter = LegacyAssetImportAdapter(
        legacy_store=legacy_store,
        core=runtime.service,
        fixture_context=fixture,
    )
    return adapter, runtime


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


def _reject(result, code):
    assert result.disposition is LegacyAssetImportDisposition.REJECTED
    assert result.error_code is code
    assert result.to_dict()["error_code"] == code.value


def test_pin_version_and_public_import_contract():
    requirement = next(
        line
        for line in (ROOT / "requirements-remember-me.txt").read_text(encoding="utf-8").splitlines()
        if line.startswith("remember-me @")
    )
    assert requirement == "remember-me @ {}#sha256={}".format(
        RM_ARCHIVE_URL,
        RM_ARCHIVE_SHA256,
    )
    assert RM_COMMIT in RM_ARCHIVE_URL
    assert PROJECT_VERSION == RM_VERSION
    assert all(
        item is not None
        for item in (
            ImportAssetTag,
            ImportAssetRequest,
            ImportAssetResult,
            ImportAssetDisposition,
            RememberMeCore,
        )
    )


def test_host_adapter_uses_only_public_rm_core_and_is_not_server_wired():
    text = (ROOT / "remember_me_import_adapter.py").read_text(encoding="utf-8")
    server_text = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "from remember_me.core import" in text
    assert "remember_me.storage" not in text
    assert "remember_me.repository" not in text
    assert ".repository" not in text
    assert ".blob_store" not in text
    assert "reindex" not in text.casefold()
    assert "ticket" not in text.casefold()
    assert "remember_me_import_adapter" not in server_text


@pytest.mark.asyncio
@pytest.mark.parametrize("image_format", ["PNG", "JPEG"])
async def test_real_single_asset_import_preserves_identity_bytes_and_history(
    tmp_path,
    image_format,
):
    legacy = AssetStore(tmp_path / "legacy")
    record = _persist_image(legacy, image_format=image_format)
    legacy_blob = legacy.resolve_file(record["asset_id"])[1].read_bytes()
    adapter, runtime = _adapter(tmp_path, legacy)

    result = adapter.import_asset(LegacyAssetImportRequest(record["asset_id"]))

    assert result.disposition is LegacyAssetImportDisposition.IMPORTED
    assert result.rm_disposition == "imported"
    stored = runtime.service.get_asset(GetAssetRequest(record["asset_id"]))
    assert stored.asset_id == record["asset_id"]
    assert stored.source_sha256 == record["source_sha256"]
    assert stored.stored_sha256 == record["stored_sha256"]
    assert stored.original_filename == record["original_filename"]
    assert stored.mime_type == record["mime_type"]
    assert stored.kind == "image"
    assert stored.decoded_bytes == record["decoded_bytes"]
    assert stored.stored_bytes == record["stored_bytes"]
    assert (stored.width, stored.height) == (record["width"], record["height"])
    assert stored.created_at == CREATED_AT
    assert stored.updated_at == UPDATED_AT
    assert stored.title == "Legacy title"
    assert stored.description == "Legacy description"
    assert stored.tags == ("Legacy Tag",)
    assert runtime.blob_store.read(stored.stored_relpath) == legacy_blob
    assert hashlib.sha256(legacy_blob).hexdigest() == stored.stored_sha256
    snapshot = runtime.service.begin_asset_verification(
        BeginAssetVerificationRequest(kind="image")
    )
    page = runtime.service.list_asset_verification_page(
        ListAssetVerificationPageRequest(snapshot.snapshot_id)
    )
    assert page.total_count == 1
    verified_record = page.records[0]
    assert verified_record.asset_id == record["asset_id"]
    assert len(verified_record.tags) == 1
    assert verified_record.tags[0].value == "Legacy Tag"
    assert verified_record.tags[0].created_at == TAG_CREATED_AT
    runtime.service.verify_asset_blob(
        VerifyAssetBlobRequest(
            snapshot.snapshot_id,
            verified_record.asset_id,
            stored.stored_sha256,
            len(legacy_blob),
            legacy_blob,
        )
    )
    completion = runtime.service.complete_asset_verification(
        CompleteAssetVerificationRequest(snapshot.snapshot_id)
    )
    assert completion.complete is True
    reindex = await runtime.service.reindex_embeddings(
        ReindexEmbeddingsRequest(asset_id=record["asset_id"])
    )
    assert (
        reindex.scanned,
        reindex.indexed,
        reindex.skipped,
        reindex.failed,
    ) == (1, 0, 1, 0)


def test_dry_run_is_zero_write_for_rm_and_legacy(tmp_path, monkeypatch):
    monkeypatch.delenv("OMBRE_RM_RUNTIME_ENABLED", raising=False)
    monkeypatch.delenv("OMBRE_RM_DATA_ROOT", raising=False)
    legacy = AssetStore(tmp_path / "legacy")
    record = _persist_image(legacy)
    adapter, runtime = _adapter(tmp_path, legacy)
    rm_before = _snapshot(tmp_path / "rm")
    legacy_before = _snapshot(tmp_path / "legacy")
    server_before = sys.modules.get("server")

    result = adapter.import_asset(
        LegacyAssetImportRequest(record["asset_id"], dry_run=True)
    )

    assert result.disposition is LegacyAssetImportDisposition.DRY_RUN_VALID
    assert result.rm_disposition == "would_import"
    assert _snapshot(tmp_path / "rm") == rm_before
    assert _snapshot(tmp_path / "legacy") == legacy_before
    assert sys.modules.get("server") is server_before
    assert "OMBRE_RM_RUNTIME_ENABLED" not in os.environ
    assert "OMBRE_RM_DATA_ROOT" not in os.environ


@pytest.mark.asyncio
async def test_repeat_is_idempotent_and_dry_run_reports_would_skip(tmp_path):
    legacy = AssetStore(tmp_path / "legacy")
    record = _persist_image(legacy)
    legacy_blob = legacy.resolve_file(record["asset_id"])[1].read_bytes()
    adapter, runtime = _adapter(tmp_path, legacy)

    first = adapter.import_asset(LegacyAssetImportRequest(record["asset_id"]))
    second = adapter.import_asset(LegacyAssetImportRequest(record["asset_id"]))
    dry = adapter.import_asset(
        LegacyAssetImportRequest(record["asset_id"], dry_run=True)
    )

    assert first.disposition is LegacyAssetImportDisposition.IMPORTED
    assert second.disposition is LegacyAssetImportDisposition.SKIPPED_IDEMPOTENT
    assert dry.disposition is LegacyAssetImportDisposition.DRY_RUN_VALID
    assert dry.rm_disposition == "would_skip_idempotent"
    stored = runtime.service.get_asset(GetAssetRequest(record["asset_id"]))
    assert stored.asset_id == record["asset_id"]
    snapshot = runtime.service.begin_asset_verification(
        BeginAssetVerificationRequest(kind="image")
    )
    page = runtime.service.list_asset_verification_page(
        ListAssetVerificationPageRequest(snapshot.snapshot_id)
    )
    assert page.total_count == 1
    assert tuple(item.asset_id for item in page.records) == (record["asset_id"],)
    runtime.service.verify_asset_blob(
        VerifyAssetBlobRequest(
            snapshot.snapshot_id,
            page.records[0].asset_id,
            stored.stored_sha256,
            len(legacy_blob),
            legacy_blob,
        )
    )
    completion = runtime.service.complete_asset_verification(
        CompleteAssetVerificationRequest(snapshot.snapshot_id)
    )
    assert completion.complete is True
    reindex = await runtime.service.reindex_embeddings(
        ReindexEmbeddingsRequest(asset_id=record["asset_id"])
    )
    assert (reindex.scanned, reindex.indexed, reindex.skipped, reindex.failed) == (
        1,
        0,
        1,
        0,
    )


def test_same_id_different_metadata_is_structured_conflict(tmp_path):
    legacy = AssetStore(tmp_path / "legacy")
    record = _persist_image(legacy)
    adapter, _ = _adapter(tmp_path, legacy)
    adapter.import_asset(LegacyAssetImportRequest(record["asset_id"]))
    with legacy._connect() as conn:
        conn.execute(
            "UPDATE assets SET title = ? WHERE asset_id = ?",
            ("Changed", record["asset_id"]),
        )

    result = adapter.import_asset(LegacyAssetImportRequest(record["asset_id"]))

    _reject(result, LegacyAssetImportErrorCode.ASSET_ID_CONFLICT)


def test_same_stored_sha_different_id_is_structured_conflict(tmp_path):
    first_store = AssetStore(tmp_path / "legacy-one")
    first = _persist_image(first_store, asset_id="a" * 32)
    adapter_one, runtime = _adapter(tmp_path, first_store)
    adapter_one.import_asset(LegacyAssetImportRequest(first["asset_id"]))
    second_store = AssetStore(tmp_path / "legacy-two")
    second = _persist_image(second_store, asset_id="b" * 32)
    assert second["stored_sha256"] == first["stored_sha256"]
    adapter_two, _ = _adapter(tmp_path, second_store, runtime=runtime)

    result = adapter_two.import_asset(LegacyAssetImportRequest(second["asset_id"]))

    _reject(result, LegacyAssetImportErrorCode.STORED_SHA_OWNERSHIP_CONFLICT)
    stored = runtime.service.get_asset(GetAssetRequest(first["asset_id"]))
    assert stored.stored_sha256 == first["stored_sha256"]
    with pytest.raises(AssetUnavailable):
        runtime.service.get_asset(GetAssetRequest(second["asset_id"]))


def test_legacy_file_and_invalid_asset_id_are_rejected_before_rm(tmp_path):
    legacy = AssetStore(tmp_path / "legacy")
    content = b"legacy file"
    source = legacy.create_temp_path(".bin")
    source.write_bytes(content)
    asset = legacy.persist_upload(
        source,
        hashlib.sha256(content).hexdigest(),
        len(content),
        "file.bin",
        "application/octet-stream",
    )
    adapter, _ = _adapter(tmp_path, legacy)

    _reject(
        adapter.import_asset(LegacyAssetImportRequest(asset["asset_id"])),
        LegacyAssetImportErrorCode.UNSUPPORTED_LEGACY_KIND,
    )
    _reject(
        adapter.import_asset(LegacyAssetImportRequest("A" * 32)),
        LegacyAssetImportErrorCode.INVALID_ASSET_ID,
    )
    _reject(
        adapter.import_asset(LegacyAssetImportRequest("a" * 31)),
        LegacyAssetImportErrorCode.INVALID_ASSET_ID,
    )


@pytest.mark.parametrize(
    ("image_format", "mime_type", "extension"),
    [
        ("GIF", "image/gif", ".gif"),
        ("WEBP", "image/webp", ".webp"),
        ("BMP", "image/bmp", ".bmp"),
    ],
)
def test_unsupported_legacy_media_types_are_rejected_with_real_bytes(
    tmp_path,
    image_format,
    mime_type,
    extension,
):
    legacy = AssetStore(tmp_path / "legacy")
    record = _persist_image(legacy)
    content = _image_bytes(image_format)
    record = _replace_legacy_blob(
        legacy,
        record["asset_id"],
        content,
        mime_type=mime_type,
        extension=extension,
    )
    adapter, runtime = _adapter(tmp_path, legacy)
    rm_before = _snapshot(tmp_path / "rm")
    legacy_before = _snapshot(legacy.data_root)

    result = adapter.import_asset(LegacyAssetImportRequest(record["asset_id"]))

    _reject(result, LegacyAssetImportErrorCode.UNSUPPORTED_MEDIA_TYPE)
    _assert_zero_write(tmp_path / "rm", legacy.data_root, rm_before, legacy_before)


@pytest.mark.parametrize(
    ("image_format", "claimed_mime", "extension"),
    [
        ("GIF", "image/png", ".png"),
        ("GIF", "image/jpeg", ".jpg"),
        ("WEBP", "image/png", ".png"),
        ("WEBP", "image/jpeg", ".jpg"),
        ("BMP", "image/png", ".png"),
        ("BMP", "image/jpeg", ".jpg"),
    ],
)
def test_masqueraded_unsupported_image_bytes_reach_rm_validation(
    tmp_path,
    image_format,
    claimed_mime,
    extension,
):
    legacy = AssetStore(tmp_path / "legacy")
    record = _persist_image(legacy)
    content = _image_bytes(image_format)
    record = _replace_legacy_blob(
        legacy,
        record["asset_id"],
        content,
        mime_type=claimed_mime,
        extension=extension,
    )
    assert record["source_sha256"] == hashlib.sha256(content).hexdigest()
    assert record["stored_sha256"] == hashlib.sha256(content).hexdigest()
    assert record["decoded_bytes"] == len(content)
    assert record["stored_bytes"] == len(content)
    adapter, runtime = _adapter(tmp_path, legacy)
    rm_before = _snapshot(tmp_path / "rm")
    legacy_before = _snapshot(legacy.data_root)

    result = adapter.import_asset(LegacyAssetImportRequest(record["asset_id"]))

    _reject(result, LegacyAssetImportErrorCode.RM_IMPORT_VALIDATION_FAILURE)
    assert result.error_code is not LegacyAssetImportErrorCode.UNSUPPORTED_MEDIA_TYPE
    assert content.hex() not in json.dumps(result.to_dict())
    _assert_zero_write(tmp_path / "rm", legacy.data_root, rm_before, legacy_before)


@pytest.mark.parametrize("image_format", ["PNG", "JPEG"])
def test_corrupt_image_is_rejected_by_rm_validation(tmp_path, image_format):
    legacy = AssetStore(tmp_path / "legacy")
    record = _persist_image(legacy, image_format=image_format)
    path = legacy.resolve_file(record["asset_id"])[1]
    corrupt = b"not an image and must never appear in an error"
    path.write_bytes(corrupt)
    digest = hashlib.sha256(corrupt).hexdigest()
    target = path.with_name("{}{}".format(digest, path.suffix))
    path.replace(target)
    with legacy._connect() as conn:
        conn.execute(
            """
            UPDATE assets
            SET stored_sha256 = ?, stored_relpath = ?, stored_bytes = ?
            WHERE asset_id = ?
            """,
            (
                digest,
                target.relative_to(legacy.data_root).as_posix(),
                len(corrupt),
                record["asset_id"],
            ),
        )
    adapter, _ = _adapter(tmp_path, legacy)

    result = adapter.import_asset(LegacyAssetImportRequest(record["asset_id"]))

    _reject(result, LegacyAssetImportErrorCode.RM_IMPORT_VALIDATION_FAILURE)
    assert corrupt.decode() not in json.dumps(result.to_dict())


def test_missing_unreadable_and_hash_mismatch_are_stable(tmp_path, monkeypatch):
    missing_store = AssetStore(tmp_path / "missing-legacy")
    missing = _persist_image(missing_store, asset_id="a" * 32)
    missing_store.resolve_file(missing["asset_id"])[1].unlink()
    missing_adapter, _ = _adapter(tmp_path, missing_store)
    _reject(
        missing_adapter.import_asset(LegacyAssetImportRequest(missing["asset_id"])),
        LegacyAssetImportErrorCode.LEGACY_BLOB_MISSING,
    )

    unreadable_store = AssetStore(tmp_path / "unreadable-legacy")
    unreadable = _persist_image(unreadable_store, asset_id="b" * 32)
    unreadable_path = unreadable_store.resolve_file(unreadable["asset_id"])[1]
    original_read_bytes = Path.read_bytes

    def denied(path):
        if path == unreadable_path:
            raise PermissionError("private path must not escape")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", denied)
    unreadable_adapter, _ = _adapter(tmp_path, unreadable_store)
    result = unreadable_adapter.import_asset(
        LegacyAssetImportRequest(unreadable["asset_id"])
    )
    _reject(result, LegacyAssetImportErrorCode.LEGACY_BLOB_UNREADABLE)
    assert "private path" not in json.dumps(result.to_dict())
    monkeypatch.setattr(Path, "read_bytes", original_read_bytes)

    mismatch_store = AssetStore(tmp_path / "mismatch-legacy")
    mismatch = _persist_image(mismatch_store, asset_id="c" * 32)
    with mismatch_store._connect() as conn:
        conn.execute(
            "UPDATE assets SET stored_sha256 = ? WHERE asset_id = ?",
            ("f" * 64, mismatch["asset_id"]),
        )
    mismatch_adapter, _ = _adapter(tmp_path, mismatch_store)
    _reject(
        mismatch_adapter.import_asset(LegacyAssetImportRequest(mismatch["asset_id"])),
        LegacyAssetImportErrorCode.STORED_SHA_MISMATCH,
    )


def test_malformed_tag_and_path_traversal_fail_closed(tmp_path):
    tag_store = AssetStore(tmp_path / "tag-legacy")
    tag_record = _persist_image(tag_store, asset_id="a" * 32)
    with tag_store._connect() as conn:
        conn.execute(
            "UPDATE asset_tags SET created_at = ? WHERE asset_id = ?",
            ("not-a-time", tag_record["asset_id"]),
        )
    tag_adapter, _ = _adapter(tmp_path, tag_store)
    _reject(
        tag_adapter.import_asset(LegacyAssetImportRequest(tag_record["asset_id"])),
        LegacyAssetImportErrorCode.RM_IMPORT_VALIDATION_FAILURE,
    )

    path_store = AssetStore(tmp_path / "path-legacy")
    path_record = _persist_image(path_store, asset_id="b" * 32)
    outside = path_store.data_root / "outside.png"
    outside.write_bytes(path_store.resolve_file(path_record["asset_id"])[1].read_bytes())
    with path_store._connect() as conn:
        conn.execute(
            "UPDATE assets SET stored_relpath = ? WHERE asset_id = ?",
            ("assets/../outside.png", path_record["asset_id"]),
        )
    path_adapter, _ = _adapter(tmp_path, path_store)
    _reject(
        path_adapter.import_asset(LegacyAssetImportRequest(path_record["asset_id"])),
        LegacyAssetImportErrorCode.MALFORMED_LEGACY_RECORD,
    )


def test_legacy_fixture_capability_rejects_old_path_authorization(tmp_path):
    legacy = AssetStore(tmp_path / "legacy")

    with pytest.raises(LegacyAssetImportAdapterError) as captured:
        LegacyAssetImportAdapter(
            legacy_store=legacy,
            core=object(),
            fixture_root=tmp_path.resolve(),
        )

    assert str(captured.value) == "invalid_fixture_capability"
    assert str(tmp_path) not in str(captured.value)


def _write_fixture_marker(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / _FIXTURE_MARKER_NAME).write_text("0" * 32, encoding="utf-8")


def test_factory_created_fixture_context_accepts_system_temp_leaf(tmp_path):
    with create_legacy_asset_import_fixture_context(tmp_path) as fixture:
        assert fixture.fixture_root == tmp_path.resolve()
        assert fixture.legacy_root == (tmp_path / "legacy").resolve()
        assert fixture.rm_root == (tmp_path / "rm").resolve()
        assert _is_within(Path(tempfile.gettempdir()), fixture.fixture_root)


def test_non_temp_workspace_reproduction_is_now_rejected():
    class NonTempWorkspaceStore:
        data_root = ROOT

    with pytest.raises(LegacyAssetImportAdapterError) as captured:
        LegacyAssetImportAdapter(
            legacy_store=NonTempWorkspaceStore(),
            core=object(),
            fixture_root=ROOT,
        )
    assert str(captured.value) == "invalid_fixture_capability"


def test_fixture_path_boundary_rejects_broad_roots(tmp_path, monkeypatch):
    drive_root = Path(tmp_path.anchor)
    cases = [
        drive_root,
        ROOT,
        ROOT / "buckets",
        Path(tempfile.gettempdir()),
        Path(tempfile.gettempdir()).resolve().parent,
        ROOT / "ordinary-data",
    ]
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "env-buckets"))
    cases.append(tmp_path / "env-buckets")
    for candidate in cases:
        with pytest.raises(LegacyAssetImportAdapterError):
            _validate_fixture_roots(
                candidate,
                candidate / "legacy",
                candidate / "rm",
            )


def test_fixture_path_boundary_rejects_temp_ancestor_and_missing_marker(tmp_path):
    with pytest.raises(LegacyAssetImportAdapterError):
        _validate_fixture_roots(tmp_path, tmp_path / "legacy", tmp_path / "rm")


def test_fixture_path_boundary_rejects_sibling_prefix_and_dotdot(tmp_path):
    root = tmp_path / "fixture"
    sibling = tmp_path / "fixture-sibling"
    _write_fixture_marker(root)
    sibling.mkdir()
    assert not _is_within(root, sibling / "asset.png")
    with pytest.raises(LegacyAssetImportAdapterError):
        _validate_fixture_roots(
            root,
            root / "legacy" / ".." / ".." / "fixture-sibling",
            root / "rm",
        )


def test_fixture_path_boundary_rejects_cross_drive_or_unc_root_when_available():
    candidates = []
    current_drive = Path.cwd().drive.upper()
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = Path(f"{letter}:/")
        if drive.drive.upper() != current_drive and drive.exists():
            candidates.append(drive)
            break
    candidates.append(Path("//server/share"))
    for candidate in candidates:
        with pytest.raises(LegacyAssetImportAdapterError):
            _validate_fixture_roots(
                candidate,
                candidate / "legacy",
                candidate / "rm",
            )


def test_fixture_path_boundary_rejects_symlink_escape(tmp_path):
    root = tmp_path / "fixture"
    _write_fixture_marker(root)
    outside = tmp_path.parent / "stage8g-outside"
    outside.mkdir(exist_ok=True)
    link = root / "legacy"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        assert not _is_within(root, outside)
    else:
        with pytest.raises(LegacyAssetImportAdapterError):
            _validate_fixture_roots(root, link, root / "rm")


def test_fixture_capability_rejects_store_core_mismatch_and_expired_use(tmp_path):
    legacy = AssetStore(tmp_path / "legacy")
    other_legacy = AssetStore(tmp_path / "other-legacy")
    fixture = create_legacy_asset_import_fixture_context(
        tmp_path,
        legacy_root=legacy.data_root,
        rm_root=tmp_path / "rm",
    )
    fixture.bind_legacy_store(legacy)
    runtime = fixture.create_runtime()
    with pytest.raises(LegacyAssetImportAdapterError):
        LegacyAssetImportAdapter(
            legacy_store=other_legacy,
            core=runtime.service,
            fixture_context=fixture,
        )

    other_fixture = create_legacy_asset_import_fixture_context(
        tmp_path / "other-fixture",
    )
    other_legacy_bound = AssetStore(other_fixture.legacy_root)
    other_fixture.bind_legacy_store(other_legacy_bound)
    with pytest.raises(LegacyAssetImportAdapterError):
        LegacyAssetImportAdapter(
            legacy_store=other_legacy_bound,
            core=runtime.service,
            fixture_context=other_fixture,
        )
    other_fixture.close()

    live_adapter = LegacyAssetImportAdapter(
        legacy_store=legacy,
        core=runtime.service,
        fixture_context=fixture,
    )
    fixture.close()
    with pytest.raises(LegacyAssetImportAdapterError):
        LegacyAssetImportAdapter(
            legacy_store=legacy,
            core=runtime.service,
            fixture_context=fixture,
        )
    with pytest.raises(LegacyAssetImportAdapterError) as captured:
        live_adapter.import_asset(LegacyAssetImportRequest("a" * 32))
    assert str(captured.value) == "fixture_root_violation"


def test_callers_cannot_directly_forge_fixture_capability():
    with pytest.raises(LegacyAssetImportAdapterError) as captured:
        LegacyAssetImportFixtureContext(_token=object())
    assert str(captured.value) == "invalid_fixture_capability"


def test_default_off_and_ownership_contracts_remain_unchanged():
    server_text = (ROOT / "server.py").read_text(encoding="utf-8")
    adapter_text = (ROOT / "remember_me_import_adapter.py").read_text(
        encoding="utf-8"
    )
    assert 'logger.info("remember-me runtime disabled")' in server_text
    assert "OMBRE_RM_RUNTIME_ENABLED" not in adapter_text
    assert "OMBRE_RM_DATA_ROOT" not in adapter_text
    assert "asset_embedding_index" not in adapter_text
    assert "backup" not in adapter_text.casefold()
    assert "dual write" not in adapter_text.casefold()
    assert "shadow write" not in adapter_text.casefold()
    assert "delete(" not in adapter_text
