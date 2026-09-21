import hashlib
import io
import json
import shutil
import sqlite3
from pathlib import Path

from PIL import ExifTags, Image, PngImagePlugin
import pytest

from asset_store import AssetStore
from remember_me.core import (
    DeleteAssetRequest,
    GetAssetRequest,
    IngestImageRequest,
    SearchAssetsRequest,
    UpdateMetadataRequest,
)
from remember_me_adapter import RememberMeAdapter


def _png_fixture():
    image = Image.new("RGBA", (32, 20), (12, 34, 56, 200))
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private-note", "remove me")
    output = io.BytesIO()
    image.save(
        output,
        format="PNG",
        pnginfo=metadata,
        icc_profile=b"private-profile",
    )
    image.close()
    return output.getvalue()


def _jpeg_fixture():
    image = Image.new("RGB", (20, 10), "red")
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6
    exif[ExifTags.Base.ImageDescription] = "private-description"
    output = io.BytesIO()
    image.save(
        output,
        format="JPEG",
        quality=95,
        exif=exif,
        icc_profile=b"private-profile",
        comment=b"private-comment",
    )
    image.close()
    return output.getvalue()


def _persist(store, content, filename, mime_type):
    source = store.create_temp_path()
    source.write_bytes(content)
    return store.persist_upload(
        source,
        hashlib.sha256(content).hexdigest(),
        len(content),
        filename,
        mime_type,
        require_image=True,
    )


def _schema(connection):
    return {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index') AND sql IS NOT NULL"
        )
    }


def _asset_rows(connection):
    return connection.execute(
        "SELECT * FROM assets ORDER BY asset_id"
    ).fetchall()


def _blob_inventory(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted((root / "assets").rglob("*"))
        if path.is_file() and ".tmp" not in path.parts
    }


@pytest.mark.asyncio
async def test_ob_and_public_rm_read_write_the_same_copied_data(tmp_path):
    ob_root = tmp_path / "ob"
    ob_store = AssetStore(ob_root)
    png = _png_fixture()
    jpeg = _jpeg_fixture()
    png_asset = _persist(ob_store, png, "private.png", "image/png")
    jpeg_asset = _persist(ob_store, jpeg, "private.jpg", "image/jpeg")
    del ob_store

    copied_root = tmp_path / "copy"
    shutil.copytree(ob_root, copied_root)
    db_path = copied_root / "assets.sqlite3"
    with sqlite3.connect(db_path) as connection:
        schema_before = _schema(connection)
        rows_before = _asset_rows(connection)
    blobs_before = _blob_inventory(copied_root)

    runtime = RememberMeAdapter().create_runtime(copied_root)
    with sqlite3.connect(db_path) as connection:
        schema_after_init = _schema(connection)
        rows_after_init = _asset_rows(connection)
    blobs_after_init = _blob_inventory(copied_root)

    added_schema = set(schema_after_init) - set(schema_before)
    assert added_schema == {"asset_embeddings", "asset_verification_state"}
    assert set(schema_before) <= set(schema_after_init)
    assert rows_after_init == rows_before
    assert blobs_after_init == blobs_before

    fetched = runtime.service.get_asset(
        GetAssetRequest(png_asset["asset_id"])
    )
    assert fetched.stored_relpath == png_asset["stored_relpath"]
    assert fetched.source_sha256 == png_asset["source_sha256"]
    assert fetched.stored_sha256 == png_asset["stored_sha256"]
    assert runtime.blob_store.read(fetched.stored_relpath) == (
        copied_root / fetched.stored_relpath
    ).read_bytes()

    updated = runtime.service.update_metadata(
        UpdateMetadataRequest(
            asset_id=fetched.asset_id,
            title="Stage 8B",
            description="synthetic compatibility fixture",
            tags=("compat", "synthetic"),
        )
    )
    result = await runtime.service.search_assets(
        SearchAssetsRequest(query="Stage 8B")
    )
    assert updated.asset_id == fetched.asset_id
    assert [item.asset.asset_id for item in result.results] == [
        fetched.asset_id
    ]

    duplicate = runtime.service.ingest_image(
        IngestImageRequest(
            content=png,
            expected_bytes=len(png),
            filename="duplicate.png",
            mime_type="image/png",
        )
    )
    # Public1 hashes its own deterministic sanitizer output.  Its encoder is
    # intentionally not byte-identical to the legacy encoder, so re-ingesting
    # the same source bytes beside legacy stored bytes creates a new asset.
    assert duplicate.deduplicated is False
    assert duplicate.asset.asset_id != fetched.asset_id
    assert runtime.service.get_asset(GetAssetRequest(fetched.asset_id)).asset_id == (
        fetched.asset_id
    )

    deleted = runtime.service.delete_asset(
        DeleteAssetRequest(jpeg_asset["asset_id"])
    )
    assert deleted.deleted is True
    del runtime

    reopened = AssetStore(copied_root)
    round_trip = reopened.get(png_asset["asset_id"])
    assert round_trip["title"] == "Stage 8B"
    assert round_trip["description"] == "synthetic compatibility fixture"
    assert round_trip["tags"] == ["compat", "synthetic"]
    assert reopened.resolve_file(png_asset["asset_id"])[1].read_bytes()
    assert reopened.get(jpeg_asset["asset_id"]) is None


def test_ob_and_rm_pillow_12_3_outputs_have_compatible_cleaning_contracts(tmp_path):
    from remember_me.imaging.pillow_sanitizer import PillowImageSanitizer

    store = AssetStore(tmp_path / "ob")
    sanitizer = PillowImageSanitizer()
    for content, claimed_mime in (
        (_png_fixture(), "image/png"),
        (_jpeg_fixture(), "image/jpeg"),
    ):
        source = store.create_temp_path()
        source.write_bytes(content)
        clean_path, ob_mime, ob_extension, width, height = (
            store._clean_image(source)
        )
        ob_content = clean_path.read_bytes()
        rm_first = sanitizer.sanitize(content, claimed_mime)
        rm_second = sanitizer.sanitize(content, claimed_mime)

        assert rm_first.content == rm_second.content
        assert ob_mime == rm_first.mime_type
        assert (width, height) == (rm_first.width, rm_first.height)
        assert ob_extension == rm_first.extension
        for clean_content in (ob_content, rm_first.content):
            with Image.open(io.BytesIO(clean_content)) as cleaned:
                cleaned.load()
                assert not cleaned.getexif()
                assert "icc_profile" not in cleaned.info
                assert "comment" not in cleaned.info
                assert "private-note" not in cleaned.info

        source.unlink(missing_ok=True)
        clean_path.unlink(missing_ok=True)
