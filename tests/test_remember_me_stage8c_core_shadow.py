import gc
import hashlib
import io
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import ExifTags, Image, PngImagePlugin

from asset_store import AssetStore, AssetStoreError
from remember_me_adapter import RememberMeAdapter
from remember_me.core import (
    ImagePixelLimitExceeded,
    ImageValidationError,
    StorageConsistencyError,
    UploadSizeMismatch,
    UploadTooLarge,
)
from remember_me_core_adapter import (
    RememberMeCoreAdapter,
    RememberMeCoreAdapterError,
)


ASSET_ID = re.compile(r"[0-9a-f]{32}")
SAFE_FIELDS = {
    "asset_id",
    "original_filename",
    "mime_type",
    "kind",
    "decoded_bytes",
    "stored_bytes",
    "width",
    "height",
    "created_at",
    "updated_at",
    "title",
    "description",
    "tags",
    "deduplicated",
}
FORBIDDEN_FIELDS = {
    "source_sha256",
    "stored_sha256",
    "stored_relpath",
    "blob_key",
    "data_root",
    "db_path",
}


def _png(
    *,
    size=(31, 19),
    color=(12, 34, 56, 255),
    transparent=False,
    private_text=False,
):
    mode = "RGBA" if transparent else "RGB"
    if transparent:
        color = (*color[:3], 120)
    image = Image.new(mode, size, color)
    metadata = PngImagePlugin.PngInfo()
    if private_text:
        metadata.add_text("private-note", "remove me")
    output = io.BytesIO()
    image.save(
        output,
        format="PNG",
        pnginfo=metadata,
        icc_profile=b"private-profile" if private_text else None,
    )
    image.close()
    return output.getvalue()


def _jpeg(
    *,
    size=(23, 13),
    color="red",
    orientation=None,
    private_metadata=False,
):
    image = Image.new("RGB", size, color)
    exif = Image.Exif()
    if orientation is not None:
        exif[ExifTags.Base.Orientation] = orientation
    if private_metadata:
        exif[ExifTags.Base.ImageDescription] = "private-description"
    output = io.BytesIO()
    image.save(
        output,
        format="JPEG",
        quality=95,
        exif=exif,
        icc_profile=b"private-profile" if private_metadata else None,
        comment=b"private-comment" if private_metadata else None,
    )
    image.close()
    return output.getvalue()


def _pixel_limit_png():
    image = Image.new("1", (5000, 4001), 0)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    image.close()
    return output.getvalue()


class LegacyFacade:
    def __init__(self, root):
        self.store = AssetStore(root)

    def ingest_image(
        self,
        content,
        expected_bytes,
        filename,
        mime_type="application/octet-stream",
        *,
        title="",
        description="",
        tags=(),
    ):
        source = self.store.create_temp_path()
        source.write_bytes(content)
        result = self.store.persist_upload(
            source,
            hashlib.sha256(content).hexdigest(),
            expected_bytes,
            filename,
            mime_type,
            require_image=True,
        )
        if title or description or tags:
            result = self.store.update_metadata(
                result["asset_id"],
                title=title,
                description=description,
                tags=list(tags),
            )
            result["deduplicated"] = False
        return _safe_legacy(result)

    def get(self, asset_id):
        result = self.store.get(asset_id)
        return None if result is None else _safe_legacy(result)

    def update_metadata(self, asset_id, title=None, description=None, tags=None):
        return _safe_legacy(
            self.store.update_metadata(
                asset_id,
                title=title,
                description=description,
                tags=None if tags is None else list(tags),
            )
        )

    def search(self, **kwargs):
        result = self.store.search(**kwargs)
        return {
            **{key: result[key] for key in ("total", "offset", "limit")},
            "results": [
                {
                    key: value
                    for key, value in item.items()
                    if key not in FORBIDDEN_FIELDS
                }
                for item in result["results"]
            ],
        }

    def resolve_blob(self, asset_id):
        resolved = self.store.resolve_file(asset_id)
        if resolved is None:
            raise RememberMeCoreAdapterError("blob_missing")
        asset, path = resolved
        return _safe_legacy(asset), path.read_bytes()

    def delete(self, asset_id):
        return self.store.delete(asset_id)


def _safe_legacy(asset):
    result = {
        key: value
        for key, value in asset.items()
        if key in SAFE_FIELDS
    }
    result.setdefault("title", "")
    result.setdefault("description", "")
    result.setdefault("tags", [])
    return result


def _semantic(asset):
    assert ASSET_ID.fullmatch(asset["asset_id"])
    assert not FORBIDDEN_FIELDS.intersection(asset)
    return {
        key: value
        for key, value in asset.items()
        if key not in {"asset_id", "created_at", "updated_at", "stored_bytes"}
    }


def _row(root, asset_id):
    with sqlite3.connect(root / "assets.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        return dict(
            connection.execute(
                "SELECT * FROM assets WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
        )


def _schema(root):
    with sqlite3.connect(root / "assets.sqlite3") as connection:
        return {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type IN ('table', 'index') AND sql IS NOT NULL"
            )
        }


def _rows(root):
    with sqlite3.connect(root / "assets.sqlite3") as connection:
        return connection.execute(
            "SELECT * FROM assets ORDER BY asset_id"
        ).fetchall()


def _blobs(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted((root / "assets").rglob("*"))
        if path.is_file() and ".tmp" not in path.parts
    }


def _new_rm(root):
    owner = RememberMeAdapter()
    return owner, RememberMeCoreAdapter.from_host_adapter(owner, root)


def test_core_adapter_import_has_no_side_effects(tmp_path):
    sentinel = tmp_path / "must-not-exist"
    project_root = Path(__file__).resolve().parent.parent
    code = (
        "import os,sys;"
        "os.chdir(sys.argv[1]);"
        "sys.path.insert(0,sys.argv[2]);"
        "import remember_me_core_adapter;"
        "print('server' in sys.modules);"
        "print('fastapi' in sys.modules);"
        "print('remember_me.standalone' in sys.modules)"
    )
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP")
        if key in os.environ
    }
    environment["OMBRE_BUCKETS_DIR"] = str(sentinel)
    if os.environ.get("PYTHONPATH"):
        environment["PYTHONPATH"] = os.environ["PYTHONPATH"]
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(tmp_path),
            str(project_root),
        ],
        cwd=project_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.splitlines() == ["False", "False", "False"]
    assert not sentinel.exists()
    assert list(tmp_path.iterdir()) == []


def test_same_data_root_rejects_a_second_runtime_owner(tmp_path):
    first_owner = RememberMeAdapter()
    first = RememberMeCoreAdapter.from_host_adapter(first_owner, tmp_path)
    assert first.get("0" * 32) is None
    with pytest.raises(RememberMeCoreAdapterError) as caught:
        RememberMeCoreAdapter.from_host_adapter(
            RememberMeAdapter(),
            tmp_path,
        )
    assert caught.value.code == "runtime_already_owned"
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.asyncio
async def test_shadow_business_scenarios_and_cleaned_bytes(tmp_path):
    legacy_root = tmp_path / "legacy"
    rm_root = tmp_path / "rm"
    legacy = LegacyFacade(legacy_root)
    rm_owner, rm = _new_rm(rm_root)
    fixtures = [
        (_png(size=(31, 19), color=(12, 34, 56, 255)), "plain.png", "image/png"),
        (_jpeg(size=(23, 13), color="red"), "needle-file.jpg", "image/jpeg"),
        (_jpeg(size=(29, 11), color="green", orientation=6), "oriented.jpg", "image/jpeg"),
        (_jpeg(size=(17, 21), color="blue", private_metadata=True), "private.jpg", "image/jpeg"),
        (_png(size=(27, 15), color=(90, 20, 10, 255), private_text=True), "text.png", "image/png"),
        (_png(size=(19, 17), color=(1, 2, 3, 255), transparent=True), "alpha.png", "image/png"),
    ]
    pairs = []
    for index, (content, filename, mime_type) in enumerate(fixtures):
        title = "Needle" if index == 0 else ""
        description = "Unicode 照片" if index == 1 else ""
        tags = ("Private", " private ", "合成") if index == 2 else ()
        old = legacy.ingest_image(
            content,
            len(content),
            filename,
            mime_type,
            title=title,
            description=description,
            tags=tags,
        )
        new = rm.ingest_image(
            content,
            len(content),
            filename,
            mime_type,
            title=title,
            description=description,
            tags=tags,
        )
        assert _semantic(old) == _semantic(new)
        old_asset, old_blob = legacy.resolve_blob(old["asset_id"])
        new_asset, new_blob = rm.resolve_blob(new["asset_id"])
        assert _semantic(old_asset) == _semantic(new_asset)
        old_row = _row(legacy_root, old["asset_id"])
        new_row = _row(rm_root, new["asset_id"])
        assert old_row["stored_sha256"] == hashlib.sha256(old_blob).hexdigest()
        assert new_row["stored_sha256"] == hashlib.sha256(new_blob).hexdigest()
        assert Path(old_row["stored_relpath"]).suffix == Path(
            new_row["stored_relpath"]
        ).suffix
        for clean_blob in (old_blob, new_blob):
            with Image.open(io.BytesIO(clean_blob)) as cleaned:
                cleaned.load()
                assert not cleaned.getexif()
                assert "icc_profile" not in cleaned.info
                assert "comment" not in cleaned.info
                assert "private-note" not in cleaned.info
        pairs.append((old, new))

    duplicate_old = legacy.ingest_image(
        fixtures[0][0],
        len(fixtures[0][0]),
        "duplicate.png",
        "image/png",
    )
    duplicate_new = rm.ingest_image(
        fixtures[0][0],
        len(fixtures[0][0]),
        "duplicate.png",
        "image/png",
    )
    assert duplicate_old["deduplicated"] is True
    assert duplicate_new["deduplicated"] is True
    assert duplicate_old["asset_id"] == pairs[0][0]["asset_id"]
    assert duplicate_new["asset_id"] == pairs[0][1]["asset_id"]

    old_id, new_id = pairs[0][0]["asset_id"], pairs[0][1]["asset_id"]
    old_updated = legacy.update_metadata(
        old_id,
        title="Needle",
        description="CaseFold SEARCH",
        tags=["  Blue Sky ", "blue sky", "标签", ""],
    )
    new_updated = rm.update_metadata(
        new_id,
        title="Needle",
        description="CaseFold SEARCH",
        tags=["  Blue Sky ", "blue sky", "标签", ""],
    )
    assert _semantic(old_updated) == _semantic(new_updated)
    assert old_updated["tags"] == ["Blue Sky", "标签"]

    old_empty = legacy.update_metadata(old_id, title="", description="")
    new_empty = rm.update_metadata(new_id, title="", description="")
    assert _semantic(old_empty) == _semantic(new_empty)
    old_unicode = legacy.update_metadata(old_id, title="Ｎｅｅｄｌｅ", description="照片")
    new_unicode = rm.update_metadata(new_id, title="Ｎｅｅｄｌｅ", description="照片")
    assert _semantic(old_unicode) == _semantic(new_unicode)
    assert old_unicode["title"] == "Needle"

    queries = [
        "needle",
        "NEEDLE",
        "照片",
        "blue sky",
        "needle-file.jpg",
        "missing",
    ]
    for query in queries:
        old_search = legacy.search(query=query)
        new_search = await rm.search(query=query)
        assert old_search["total"] == new_search["total"]
        old_items = sorted(
            (
                item["filename"],
                tuple(item["match_reasons"]),
            )
            for item in old_search["results"]
        )
        new_items = sorted(
            (
                item["filename"],
                tuple(item["match_reasons"]),
            )
            for item in new_search["results"]
        )
        assert old_items == new_items
    assert [
        item["filename"]
        for item in legacy.search(query="needle")["results"]
    ] == ["plain.png", "needle-file.jpg"]
    assert [
        item["filename"]
        for item in (await rm.search(query="needle"))["results"]
    ] == ["plain.png", "needle-file.jpg"]
    assert legacy.search(query="missing")["results"] == []
    assert (await rm.search(query="missing"))["results"] == []

    assert legacy.get("bad-id") is None
    assert rm.get("bad-id") is None
    assert legacy.get("f" * 32) is None
    assert rm.get("f" * 32) is None
    with pytest.raises(RememberMeCoreAdapterError) as invalid_id:
        rm.update_metadata("bad-id", title="x")
    assert invalid_id.value.code == "invalid_asset_id"
    with pytest.raises(AssetStoreError):
        legacy.update_metadata("bad-id", title="x")

    with pytest.raises(AssetStoreError):
        legacy.ingest_image(b"not an image", 12, "bad.bin", "image/png")
    with pytest.raises(RememberMeCoreAdapterError) as invalid_image:
        rm.ingest_image(b"not an image", 12, "bad.bin", "image/png")
    assert invalid_image.value.code == "invalid_image"

    with pytest.raises(RememberMeCoreAdapterError) as size_mismatch:
        rm.ingest_image(fixtures[0][0], 1, "bad.png", "image/png")
    assert size_mismatch.value.code == "upload_size_mismatch"
    with pytest.raises(RememberMeCoreAdapterError) as too_large:
        rm.ingest_image(b"x" * (10 * 1024 * 1024 + 1), 10 * 1024 * 1024 + 1, "large.png", "image/png")
    assert too_large.value.code == "upload_too_large"
    pixel_image = _pixel_limit_png()
    with pytest.raises(AssetStoreError, match="image_pixel_limit"):
        legacy.ingest_image(pixel_image, len(pixel_image), "pixels.png", "image/png")
    with pytest.raises(RememberMeCoreAdapterError) as pixel_limit:
        rm.ingest_image(pixel_image, len(pixel_image), "pixels.png", "image/png")
    assert pixel_limit.value.code == "pixel_limit"

    deleted_old = legacy.delete(pairs[-1][0]["asset_id"])
    deleted_new = rm.delete(pairs[-1][1]["asset_id"])
    assert deleted_old["deleted"] == deleted_new["deleted"] is True
    assert legacy.get(pairs[-1][0]["asset_id"]) is None
    assert rm.get(pairs[-1][1]["asset_id"]) is None
    del rm, rm_owner
    gc.collect()


def test_sequential_cross_runtime_reopen_and_schema_guard(tmp_path):
    root = tmp_path / "roundtrip"
    legacy = LegacyFacade(root)
    content = _png(private_text=True)
    created = legacy.ingest_image(
        content,
        len(content),
        "roundtrip.png",
        "image/png",
    )
    schema_before = _schema(root)
    rows_before = _rows(root)
    blobs_before = _blobs(root)
    del legacy

    rm_owner, rm = _new_rm(root)
    schema_after = _schema(root)
    added = set(schema_after) - set(schema_before)
    assert added == {"asset_embeddings", "asset_verification_state"}
    for name, sql in schema_before.items():
        assert schema_after[name] == sql
    assert _rows(root) == rows_before
    assert _blobs(root) == blobs_before
    rm.update_metadata(
        created["asset_id"],
        title="RM update",
        description="first direction",
        tags=["rm", "roundtrip"],
    )
    assert rm.get(created["asset_id"])["title"] == "RM update"
    del rm, rm_owner
    gc.collect()

    reopened_ob = LegacyFacade(root)
    from_rm = reopened_ob.get(created["asset_id"])
    assert from_rm["title"] == "RM update"
    assert from_rm["tags"] == ["rm", "roundtrip"]
    reopened_ob.update_metadata(
        created["asset_id"],
        title="OB update",
        description="second direction",
        tags=["ob", "roundtrip"],
    )
    del reopened_ob

    final_owner, final_rm = _new_rm(root)
    from_ob = final_rm.get(created["asset_id"])
    assert from_ob["title"] == "OB update"
    assert from_ob["description"] == "second direction"
    assert from_ob["tags"] == ["ob", "roundtrip"]
    _, blob = final_rm.resolve_blob(created["asset_id"])
    assert blob
    del final_rm, final_owner
    gc.collect()


def test_blob_missing_and_safe_error_messages(tmp_path):
    owner, adapter = _new_rm(tmp_path)
    content = _png()
    asset = adapter.ingest_image(
        content,
        len(content),
        "missing.png",
        "image/png",
    )
    row = _row(tmp_path, asset["asset_id"])
    (tmp_path / row["stored_relpath"]).unlink()
    with pytest.raises(RememberMeCoreAdapterError) as caught:
        adapter.resolve_blob(asset["asset_id"])
    assert caught.value.code == "blob_missing"
    message = str(caught.value)
    assert str(tmp_path) not in message
    assert row["stored_sha256"] not in message
    assert "missing.png" not in message
    del adapter, owner
    gc.collect()


def test_metadata_not_found_and_repository_errors_are_safely_mapped(tmp_path):
    owner, adapter = _new_rm(tmp_path)
    content = _png()
    asset = adapter.ingest_image(
        content,
        len(content),
        "errors.png",
        "image/png",
    )
    with pytest.raises(RememberMeCoreAdapterError) as invalid_metadata:
        adapter.update_metadata(asset["asset_id"], title=object())
    assert invalid_metadata.value.code == "invalid_metadata"
    missing_delete = adapter.delete("f" * 32)
    assert missing_delete == {
        "asset_id": "f" * 32,
        "deleted": False,
        "cleanup_pending": False,
    }

    class BrokenService:
        @staticmethod
        def get_asset(_request):
            raise sqlite3.DatabaseError("private database detail")

    class BrokenRuntime:
        service = BrokenService()
        repository = object()
        blob_store = object()

    broken = RememberMeCoreAdapter(BrokenRuntime())
    with pytest.raises(RememberMeCoreAdapterError) as repository:
        broken.get("0" * 32)
    assert repository.value.code == "repository_failure"
    assert "private database detail" not in str(repository.value)
    assert str(tmp_path) not in str(repository.value)
    del adapter, owner
    gc.collect()


def test_production_modules_do_not_import_stage8c_adapter():
    root = Path(__file__).resolve().parent.parent
    for relative in (
        "server.py",
        "asset_dashboard.py",
        "asset_viewer.py",
        "asset_embedding_index.py",
    ):
        assert "remember_me_core_adapter" not in (
            root / relative
        ).read_text(encoding="utf-8")


OB_PUBLIC_UPLOAD_FIELDS = {
    "asset_id",
    "source_sha256",
    "stored_sha256",
    "decoded_bytes",
    "stored_bytes",
    "mime_type",
    "filename",
    "kind",
    "width",
    "height",
    "created_at",
    "title",
    "description",
    "tags",
    "updated_at",
    "deduplicated",
}


class _FakeObIngestService:
    def __init__(self, *, error=None, deduplicated=True):
        self.error = error
        self.deduplicated = deduplicated
        self.ingest_calls = []
        self.get_asset_calls = 0
        self.search_assets_calls = 0
        self.resolve_asset_calls = 0
        self.update_metadata_calls = 0

    def ingest_image(self, request):
        self.ingest_calls.append(request)
        if self.error is not None:
            raise self.error
        asset = SimpleNamespace(
            asset_id="a" * 32,
            source_sha256="1" * 64,
            stored_sha256="2" * 64,
            original_filename="clean.png",
            mime_type="image/png",
            kind="image",
            decoded_bytes=request.expected_bytes,
            stored_bytes=request.expected_bytes - 1,
            width=3,
            height=2,
            created_at="2026-01-02T03:04:05Z",
            updated_at="2026-01-02T03:04:06Z",
            title=request.title,
            description=request.description,
            tags=list(request.tags),
        )
        return SimpleNamespace(asset=asset, deduplicated=self.deduplicated)

    def get_asset(self, _request):
        self.get_asset_calls += 1
        raise AssertionError("post-read get_asset is forbidden")

    def search_assets(self, _request):
        self.search_assets_calls += 1
        raise AssertionError("post-read search_assets is forbidden")

    def resolve_asset(self, _request):
        self.resolve_asset_calls += 1
        raise AssertionError("post-read resolve_asset is forbidden")

    def update_metadata(self, _request):
        self.update_metadata_calls += 1
        raise AssertionError("post-read update_metadata is forbidden")


class _FakeObIngestRuntime:
    def __init__(self, service):
        self.service = service
        self.repository = object()
        self.blob_store = object()


def test_ingest_ob_public_metadata_uses_single_mutation_result_contract():
    service = _FakeObIngestService(deduplicated=True)
    adapter = RememberMeCoreAdapter(_FakeObIngestRuntime(service))

    result = adapter.ingest_ob_public_metadata(
        b"content",
        7,
        "clean.png",
        "image/png",
        title="Title",
        description="Description",
        tags=["one", "two"],
    )

    assert len(service.ingest_calls) == 1
    assert set(result) == OB_PUBLIC_UPLOAD_FIELDS
    assert result["source_sha256"] == "1" * 64
    assert result["stored_sha256"] == "2" * 64
    assert result["filename"] == "clean.png"
    assert "original_filename" not in result
    assert result["deduplicated"] is True
    assert service.get_asset_calls == 0
    assert service.search_assets_calls == 0
    assert service.resolve_asset_calls == 0
    assert service.update_metadata_calls == 0


def test_ingest_ob_public_metadata_passes_request_arguments_exactly():
    service = _FakeObIngestService(deduplicated=False)
    adapter = RememberMeCoreAdapter(_FakeObIngestRuntime(service))
    content = b"abc"

    result = adapter.ingest_ob_public_metadata(
        content,
        3,
        "photo.png",
        "image/png",
        title="T",
        description="D",
        tags=("x", "y"),
    )

    assert result["deduplicated"] is False
    assert len(service.ingest_calls) == 1
    request = service.ingest_calls[0]
    assert request.content is content
    assert request.expected_bytes == 3
    assert request.filename == "photo.png"
    assert request.mime_type == "image/png"
    assert request.title == "T"
    assert request.description == "D"
    assert request.tags == ("x", "y")


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (UploadTooLarge("private path"), "upload_too_large"),
        (UploadSizeMismatch("private path"), "upload_size_mismatch"),
        (ImagePixelLimitExceeded("private path"), "pixel_limit"),
        (ImageValidationError("private path"), "invalid_image"),
        (StorageConsistencyError("private path"), "repository_failure"),
        (RuntimeError("private path"), "repository_failure"),
    ],
)
def test_ingest_ob_public_metadata_error_mapping_is_safe(exc, code, tmp_path):
    service = _FakeObIngestService(error=exc)
    adapter = RememberMeCoreAdapter(_FakeObIngestRuntime(service))

    with pytest.raises(RememberMeCoreAdapterError) as caught:
        adapter.ingest_ob_public_metadata(b"x", 1, "secret.png", "image/png")

    assert len(service.ingest_calls) == 1
    assert caught.value.code == code
    assert "private path" not in str(caught.value)
    assert "secret.png" not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_existing_ingest_image_contract_is_unchanged_for_original_filename():
    service = _FakeObIngestService(deduplicated=True)
    adapter = RememberMeCoreAdapter(_FakeObIngestRuntime(service))

    result = adapter.ingest_image(b"content", 7, "clean.png", "image/png")

    assert len(service.ingest_calls) == 1
    assert set(result) == SAFE_FIELDS
    assert result["original_filename"] == "clean.png"
    assert "filename" not in result
    assert "source_sha256" not in result
    assert "stored_sha256" not in result
    assert result["deduplicated"] is True
