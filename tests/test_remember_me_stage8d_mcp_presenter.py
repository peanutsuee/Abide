import base64
import gc
import io
import json
from copy import deepcopy
from pathlib import Path

import pytest
from PIL import Image

from remember_me_adapter import RememberMeAdapter
from remember_me_core_adapter import (
    RememberMeCoreAdapter,
    RememberMeCoreAdapterError,
)
from remember_me_mcp_presenter import (
    RememberMeMcpCompatibilityPresenter,
    RememberMeMcpCompatibilityPresenterError,
)
import remember_me_mcp_presenter as presenter_module


ASSET_ID = "a" * 32
MISSING_ID = "f" * 32
PUBLIC_METADATA = {
    "asset_id": ASSET_ID,
    "source_sha256": "1" * 64,
    "stored_sha256": "2" * 64,
    "decoded_bytes": 123,
    "stored_bytes": 91,
    "mime_type": "image/png",
    "filename": "sample.png",
    "kind": "image",
    "width": 8,
    "height": 6,
    "created_at": "2026-01-01T00:00:00+00:00",
    "title": "Sample",
    "description": "Description",
    "tags": ["one", "标签"],
    "updated_at": "2026-01-01T00:00:00+00:00",
}


def _png_bytes(color="red"):
    image = Image.new("RGB", (8, 6), color)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    image.close()
    return output.getvalue()


def _jpeg_bytes(color="blue"):
    image = Image.new("RGB", (7, 5), color)
    output = io.BytesIO()
    image.save(
        output,
        format="JPEG",
        quality=90,
        optimize=True,
        exif=b"",
    )
    image.close()
    return output.getvalue()


class FakeCoreAdapter:
    def __init__(self, metadata=None, blob=None):
        self.metadata = deepcopy(metadata or PUBLIC_METADATA)
        self.blob = blob if blob is not None else _png_bytes()
        self.update_calls = []
        self.error = None

    def get(self, asset_id):
        if self.error:
            raise RememberMeCoreAdapterError(self.error)
        if asset_id == MISSING_ID:
            return None
        return {
            "asset_id": self.metadata["asset_id"],
            "original_filename": self.metadata["filename"],
        }

    def get_ob_public_metadata(self, asset_id):
        if self.error:
            raise RememberMeCoreAdapterError(self.error)
        if asset_id == MISSING_ID:
            return None
        return deepcopy(self.metadata)

    def update_metadata(
        self,
        asset_id,
        title=None,
        description=None,
        tags=None,
    ):
        if self.error:
            raise RememberMeCoreAdapterError(self.error)
        if asset_id == MISSING_ID:
            raise RememberMeCoreAdapterError("asset_not_found")
        self.update_calls.append(
            {
                "asset_id": asset_id,
                "title": title,
                "description": description,
                "tags": tags,
            }
        )
        if title is not None:
            self.metadata["title"] = title
        if description is not None:
            self.metadata["description"] = description
        if tags is not None:
            self.metadata["tags"] = list(tags)
        return deepcopy(self.metadata)

    def update_ob_public_metadata(
        self,
        asset_id,
        title=None,
        description=None,
        tags=None,
    ):
        return self.update_metadata(
            asset_id,
            title=title,
            description=description,
            tags=tags,
        )

    def resolve_blob(self, asset_id):
        if self.error:
            raise RememberMeCoreAdapterError(self.error)
        if asset_id == MISSING_ID:
            raise RememberMeCoreAdapterError("asset_not_found")
        return (
            {
                "asset_id": self.metadata["asset_id"],
                "original_filename": self.metadata["filename"],
                "mime_type": self.metadata["mime_type"],
                "kind": self.metadata["kind"],
                "stored_bytes": len(self.blob),
                "width": self.metadata["width"],
                "height": self.metadata["height"],
                "title": self.metadata["title"],
                "tags": list(self.metadata["tags"]),
            },
            self.blob,
        )

    def search(self, *args, **kwargs):
        raise AssertionError("search must not be used")


class FakeDownloadLinks:
    def __init__(self):
        self.calls = []
        self.error = ""

    def create_download_link(self, asset):
        self.calls.append(deepcopy(dict(asset)))
        if self.error:
            raise RememberMeMcpCompatibilityPresenterError(self.error)
        return {
            "ok": True,
            "asset_id": asset["asset_id"],
            "filename": asset["filename"],
            "mime_type": asset["mime_type"],
            "stored_bytes": asset["stored_bytes"],
            "stored_sha256": asset["stored_sha256"],
            "download_path": "/rm/asset-download/fake-ticket",
            "download_url": (
                "https://example.invalid/rm/asset-download/fake-ticket"
            ),
            "expires_in_seconds": 300,
        }


def _presenter(core=None, links=None):
    core = core or FakeCoreAdapter()
    links = links or FakeDownloadLinks()
    return (
        RememberMeMcpCompatibilityPresenter(core, links),
        core,
        links,
    )


def test_presenter_import_and_construction_have_no_production_side_effects():
    assert not hasattr(presenter_module, "server")
    assert "remember_me.standalone" not in presenter_module.__dict__
    presenter, _, _ = _presenter()
    assert presenter is not None


def test_get_matches_current_ob_json_envelope_exactly():
    presenter, _, _ = _presenter()
    assert json.loads(presenter.rm_asset_get(ASSET_ID)) == {
        "ok": True,
        **PUBLIC_METADATA,
    }
    assert json.loads(presenter.rm_asset_get(MISSING_ID)) == {
        "ok": False,
        "error": "asset_unavailable",
    }


def test_update_preserves_omitted_null_empty_and_unicode_semantics():
    presenter, core, _ = _presenter()
    unchanged = json.loads(
        presenter.rm_asset_update_metadata(ASSET_ID)
    )
    assert core.update_calls[-1] == {
        "asset_id": ASSET_ID,
        "title": None,
        "description": None,
        "tags": None,
    }
    assert unchanged == {"ok": True, **PUBLIC_METADATA}

    explicit_null = json.loads(
        presenter.rm_asset_update_metadata(
            ASSET_ID,
            title=None,
            description=None,
            tags=None,
        )
    )
    assert explicit_null == unchanged

    cleared = json.loads(
        presenter.rm_asset_update_metadata(
            ASSET_ID,
            title="",
            description="",
            tags=[],
        )
    )
    assert cleared["title"] == ""
    assert cleared["description"] == ""
    assert cleared["tags"] == []

    unicode_result = json.loads(
        presenter.rm_asset_update_metadata(
            ASSET_ID,
            title="照片标题",
            description="说明",
            tags=["标签", "été"],
        )
    )
    assert unicode_result["title"] == "照片标题"
    assert unicode_result["description"] == "说明"
    assert unicode_result["tags"] == ["标签", "été"]


@pytest.mark.parametrize(
    ("core_code", "expected"),
    [
        ("asset_not_found", "asset_unavailable"),
        ("invalid_title", "invalid_title"),
        ("title_too_long", "title_too_long"),
        ("invalid_tags", "invalid_tags"),
        ("repository_failure", "asset_unavailable"),
    ],
)
def test_update_errors_match_safe_ob_codes(core_code, expected):
    core = FakeCoreAdapter()
    core.error = core_code
    presenter, _, _ = _presenter(core=core)
    assert json.loads(
        presenter.rm_asset_update_metadata(ASSET_ID, title="x")
    ) == {"ok": False, "error": expected}


def test_download_link_matches_ob_payload_and_delegates_ticket_ownership():
    presenter, _, links = _presenter()
    result = json.loads(presenter.rm_asset_download_link(ASSET_ID))
    assert result == {
        "ok": True,
        "asset_id": ASSET_ID,
        "filename": "sample.png",
        "mime_type": "image/png",
        "stored_bytes": 91,
        "stored_sha256": "2" * 64,
        "download_path": "/rm/asset-download/fake-ticket",
        "download_url": (
            "https://example.invalid/rm/asset-download/fake-ticket"
        ),
        "expires_in_seconds": 300,
    }
    assert links.calls == [PUBLIC_METADATA]
    assert json.loads(presenter.rm_asset_download_link(MISSING_ID)) == {
        "ok": False,
        "error": "asset_unavailable",
    }
    links.error = "download_store_full"
    assert json.loads(presenter.rm_asset_download_link(ASSET_ID)) == {
        "ok": False,
        "error": "download_store_full",
    }


def test_view_matches_current_ob_result_structure_exactly():
    blob = _png_bytes()
    metadata = {
        **PUBLIC_METADATA,
        "stored_bytes": len(blob),
    }
    presenter, _, _ = _presenter(
        core=FakeCoreAdapter(metadata=metadata, blob=blob)
    )
    result = presenter.rm_asset_view(ASSET_ID)
    assert result.isError is False
    assert [item.type for item in result.content] == ["text"]
    assert result.content[0].text == (
        "Remember-Me image: Sample\n"
        "If this client does not display the inline viewer, use this "
        "short-lived download link: "
        "https://example.invalid/rm/asset-download/fake-ticket"
    )
    assert result.structuredContent == {
        "asset_id": ASSET_ID,
        "title": "Sample",
        "filename": "sample.png",
        "mime_type": "image/png",
        "width": 8,
        "height": 6,
        "tags": ["one", "标签"],
        "stored_bytes": len(blob),
    }
    assert result.meta == {
        "rememberMe": {
            "schemaVersion": 1,
            "imageBase64": base64.b64encode(blob).decode("ascii"),
            "mimeType": "image/png",
        }
    }


def test_inspect_matches_current_ob_result_structure_exactly():
    blob = _png_bytes()
    metadata = {
        **PUBLIC_METADATA,
        "stored_bytes": len(blob),
    }
    presenter, _, _ = _presenter(
        core=FakeCoreAdapter(metadata=metadata, blob=blob)
    )
    result = presenter.rm_asset_inspect(ASSET_ID)
    assert result.isError is False
    assert result.meta is None
    assert [item.type for item in result.content] == ["text", "image"]
    assert result.content[0].text == (
        f"Remember-Me image asset {ASSET_ID}; filename: sample.png; "
        "MIME type: image/png; dimensions: 8 x 6."
    )
    assert result.content[1].mimeType == "image/png"
    assert base64.b64decode(
        result.content[1].data,
        validate=True,
    ) == blob
    assert result.structuredContent == {
        "asset_id": ASSET_ID,
        "title": "Sample",
        "filename": "sample.png",
        "mime_type": "image/png",
        "width": 8,
        "height": 6,
        "tags": ["one", "标签"],
        "stored_bytes": len(blob),
    }


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("asset_not_found", "asset_unavailable"),
        ("blob_missing", "asset_unavailable"),
        ("repository_failure", "image_unavailable"),
    ],
)
def test_view_and_inspect_errors_are_exact_and_sanitized(
    tmp_path,
    code,
    expected,
):
    core = FakeCoreAdapter()
    core.error = code
    presenter, _, _ = _presenter(core=core)
    for result in (
        presenter.rm_asset_view(ASSET_ID),
        presenter.rm_asset_inspect(ASSET_ID),
    ):
        assert result.isError is True
        assert result.structuredContent == {
            "ok": False,
            "error": expected,
        }
        serialized = result.model_dump_json(by_alias=True)
        for forbidden in (
            str(tmp_path),
            "private-token",
            "Traceback",
            "stored_relpath",
            "<stored-sha256>",
        ):
            assert forbidden not in serialized
        assert "imageBase64" not in serialized


def test_corrupt_or_mismatched_blob_is_not_exposed():
    core = FakeCoreAdapter(blob=b"not an image")
    presenter, _, _ = _presenter(core=core)
    for result in (
        presenter.rm_asset_view(ASSET_ID),
        presenter.rm_asset_inspect(ASSET_ID),
    ):
        assert result.structuredContent == {
            "ok": False,
            "error": "image_unavailable",
        }
        assert "not an image" not in result.model_dump_json(by_alias=True)


@pytest.mark.parametrize(
    ("content", "filename", "mime_type"),
    [
        (_png_bytes(), "real.png", "image/png"),
        (_jpeg_bytes(), "real.jpg", "image/jpeg"),
    ],
)
def test_real_rm_core_presenter_flow_and_reopen(
    tmp_path,
    content,
    filename,
    mime_type,
):
    root = tmp_path / filename.replace(".", "-")
    owner = RememberMeAdapter()
    core = RememberMeCoreAdapter.from_host_adapter(owner, root)
    links = FakeDownloadLinks()
    presenter = RememberMeMcpCompatibilityPresenter(core, links)
    ingested = core.ingest_image(
        content,
        len(content),
        filename,
        mime_type,
    )
    asset_id = ingested["asset_id"]
    _, blob_before = core.resolve_blob(asset_id)

    fetched = json.loads(presenter.rm_asset_get(asset_id))
    assert fetched["ok"] is True
    assert fetched["filename"] == filename
    assert fetched["mime_type"] == mime_type
    assert "stored_relpath" not in fetched

    updated = json.loads(
        presenter.rm_asset_update_metadata(
            asset_id,
            title="真实标题",
            description="Presenter integration",
            tags=["真实", "stage8d"],
        )
    )
    assert updated["title"] == "真实标题"
    assert updated["tags"] == ["stage8d", "真实"]
    invalid = json.loads(
        presenter.rm_asset_update_metadata(
            asset_id,
            title=object(),
        )
    )
    assert invalid == {"ok": False, "error": "invalid_title"}

    inspected = presenter.rm_asset_inspect(asset_id)
    viewed = presenter.rm_asset_view(asset_id)
    download = json.loads(presenter.rm_asset_download_link(asset_id))
    assert inspected.isError is False
    assert viewed.isError is False
    assert download["download_path"] == (
        "/rm/asset-download/fake-ticket"
    )
    assert len(links.calls) == 2
    _, blob_after = core.resolve_blob(asset_id)
    assert blob_after == blob_before

    del presenter, core, owner
    gc.collect()

    reopened_owner = RememberMeAdapter()
    reopened_core = RememberMeCoreAdapter.from_host_adapter(
        reopened_owner,
        root,
    )
    reopened = RememberMeMcpCompatibilityPresenter(
        reopened_core,
        FakeDownloadLinks(),
    )
    assert json.loads(reopened.rm_asset_get(asset_id))["title"] == (
        "真实标题"
    )
    reopened_core.delete(asset_id)
    assert json.loads(reopened.rm_asset_get(asset_id)) == {
        "ok": False,
        "error": "asset_unavailable",
    }
    del reopened, reopened_core, reopened_owner
    gc.collect()


def test_presenter_has_no_production_import_or_registration():
    root = Path(__file__).resolve().parent.parent
    for relative in (
        "server.py",
        "asset_dashboard.py",
        "asset_viewer.py",
        "asset_embedding_index.py",
    ):
        assert "remember_me_mcp_presenter" not in (
            root / relative
        ).read_text(encoding="utf-8")
