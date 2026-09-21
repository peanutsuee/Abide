import io
import json
import re
import threading
from collections.abc import Iterator, Mapping
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from remember_me_core_adapter import (
    RememberMeCoreAdapter,
    RememberMeCoreAdapterError,
)
from remember_me_download_links import (
    RememberMeDownloadLinkError,
    RememberMeObDownloadLinkCollaborator,
    safe_download_filename,
)
from remember_me_mcp_presenter import (
    RememberMeMcpCompatibilityPresenter,
)

ASSET_ID = "a" * 32
STORED_SHA = "1" * 64
SOURCE_SHA = "2" * 64
TOKEN_A = "A" * 43
TOKEN_B = "B" * 43
PUBLIC_METADATA = {
    "asset_id": ASSET_ID,
    "source_sha256": SOURCE_SHA,
    "stored_sha256": STORED_SHA,
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
_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "application/octet-stream": ".bin",
}
_DEFAULT_MUTATION_RESULT = object()


def _asset_object():
    return SimpleNamespace(
        asset_id=ASSET_ID,
        source_sha256=SOURCE_SHA,
        stored_sha256=STORED_SHA,
        decoded_bytes=123,
        stored_bytes=91,
        mime_type="image/png",
        original_filename="sample.png",
        kind="image",
        width=8,
        height=6,
        created_at="2026-01-01T00:00:00+00:00",
        title="Updated",
        description="Description",
        tags=("one", "标签"),
        updated_at="2026-01-01T00:00:01+00:00",
    )


def _png_bytes():
    image = Image.new("RGB", (8, 6), "red")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    image.close()
    return output.getvalue()


class MutationOnlyService:
    def __init__(self):
        self.update_calls = []
        self.get_calls = 0
        self.error = None

    def update_metadata(self, request):
        self.update_calls.append(request)
        if self.error is not None:
            raise self.error
        return _asset_object()

    def get_asset(self, request):
        self.get_calls += 1
        raise AssertionError("metadata mutation must not read")


def _core_adapter(service=None):
    runtime = SimpleNamespace(
        service=service or MutationOnlyService(),
        repository=object(),
        blob_store=object(),
    )
    return RememberMeCoreAdapter(runtime)


class PresenterCore:
    def __init__(
        self,
        mutation_result=_DEFAULT_MUTATION_RESULT,
        blob=None,
    ):
        self.metadata = deepcopy(PUBLIC_METADATA)
        self.mutation_result = (
            deepcopy(PUBLIC_METADATA)
            if mutation_result is _DEFAULT_MUTATION_RESULT
            else mutation_result
        )
        self.blob = blob if blob is not None else _png_bytes()
        self.get_calls = 0
        self.update_calls = []
        self.error = None

    def get(self, asset_id):
        return None

    def get_ob_public_metadata(self, asset_id):
        self.get_calls += 1
        if self.error is not None:
            raise self.error
        return deepcopy(self.metadata)

    def update_ob_public_metadata(
        self,
        asset_id,
        title=None,
        description=None,
        tags=None,
    ):
        self.update_calls.append(
            (asset_id, title, description, tags)
        )
        if self.error is not None:
            raise self.error
        result = deepcopy(self.mutation_result)
        if isinstance(result, dict):
            if title is not None:
                result["title"] = title
            if description is not None:
                result["description"] = description
            if tags is not None:
                result["tags"] = list(tags)
        return result

    def resolve_blob(self, asset_id):
        return (
            {
                "asset_id": ASSET_ID,
                "original_filename": "sample.png",
                "mime_type": "image/png",
                "kind": "image",
                "stored_bytes": len(self.blob),
                "width": 8,
                "height": 6,
                "title": "Sample",
                "tags": ["one"],
            },
            self.blob,
        )

    def search(self, *args, **kwargs):
        raise AssertionError("search must not be used")


class FixedDownloadLinks:
    def create_download_link(self, asset):
        return {
            "ok": True,
            "asset_id": asset["asset_id"],
            "filename": asset["filename"],
            "mime_type": asset["mime_type"],
            "stored_bytes": asset["stored_bytes"],
            "stored_sha256": asset["stored_sha256"],
            "download_path": f"/rm/asset-download/{TOKEN_A}",
            "download_url": "",
            "expires_in_seconds": 300,
        }


def _presenter(core=None, links=None):
    return RememberMeMcpCompatibilityPresenter(
        core or PresenterCore(),
        links or FixedDownloadLinks(),
    )


def test_update_ob_public_metadata_uses_mutation_result_without_read():
    service = MutationOnlyService()
    adapter = _core_adapter(service)
    result = adapter.update_ob_public_metadata(
        ASSET_ID,
        title="Updated",
        tags=["one", "标签"],
    )

    assert result == {
        **PUBLIC_METADATA,
        "title": "Updated",
        "updated_at": "2026-01-01T00:00:01+00:00",
    }
    assert len(service.update_calls) == 1
    assert service.get_calls == 0


def test_legacy_update_metadata_contract_is_preserved():
    service = MutationOnlyService()
    result = _core_adapter(service).update_metadata(ASSET_ID)

    assert result == {
        "asset_id": ASSET_ID,
        "original_filename": "sample.png",
        "mime_type": "image/png",
        "kind": "image",
        "decoded_bytes": 123,
        "stored_bytes": 91,
        "width": 8,
        "height": 6,
        "title": "Updated",
        "description": "Description",
        "tags": ["one", "标签"],
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:01+00:00",
    }
    assert service.get_calls == 0


def test_presenter_returns_success_without_post_mutation_read():
    core = PresenterCore()
    result = json.loads(
        _presenter(core=core).rm_asset_update_metadata(
            ASSET_ID,
            title="新标题",
            description="",
            tags=[],
        )
    )

    assert result["ok"] is True
    assert result["title"] == "新标题"
    assert result["description"] == ""
    assert result["tags"] == []
    assert core.get_calls == 0
    assert core.update_calls == [(ASSET_ID, "新标题", "", [])]


def test_presenter_mutation_failure_does_not_read():
    core = PresenterCore()
    core.error = RememberMeCoreAdapterError(
        "invalid_metadata",
        ob_code="invalid_title",
    )
    result = json.loads(
        _presenter(core=core).rm_asset_update_metadata(
            ASSET_ID,
            title="bad",
        )
    )

    assert result == {"ok": False, "error": "invalid_title"}
    assert core.get_calls == 0


@pytest.mark.parametrize(
    "result",
    [
        None,
        object(),
        {"asset_id": ASSET_ID},
        {**PUBLIC_METADATA, "title": object()},
    ],
)
def test_presenter_rejects_invalid_mutation_result_safely(result):
    output = _presenter(
        core=PresenterCore(mutation_result=result)
    ).rm_asset_update_metadata(ASSET_ID)
    assert json.loads(output) == {
        "ok": False,
        "error": "asset_unavailable",
    }


def _legacy_filename(filename, mime_type, asset_id=ASSET_ID):
    extension = _EXTENSIONS[mime_type]
    name = re.sub(
        r"[^A-Za-z0-9._ -]+",
        "_",
        filename,
    ).strip(" .")
    if not name:
        name = f"remember-me-{asset_id}{extension}"
    elif not name.lower().endswith(extension.lower()):
        name += extension
    return name[:180]


@pytest.mark.parametrize(
    ("filename", "mime_type"),
    [
        ("photo.png", "image/png"),
        ("照片.png", "image/png"),
        ("été.jpg", "image/jpeg"),
        ("photo", "image/png"),
        ("photo.jpeg", "image/jpeg"),
        ("photo.gif", "image/png"),
        ("photo.PNG", "image/png"),
        ("", "image/png"),
        (" ... ", "image/jpeg"),
        ("../bad\\name\n.png", "image/png"),
        ("a" * 220 + ".png", "image/png"),
        ("archive", "application/octet-stream"),
    ],
)
def test_safe_download_filename_matches_frozen_legacy_matrix(
    filename,
    mime_type,
):
    asset = {
        "asset_id": ASSET_ID,
        "filename": filename,
        "mime_type": mime_type,
    }
    assert safe_download_filename(asset) == _legacy_filename(
        filename,
        mime_type,
    )
    assert len(safe_download_filename(asset)) <= 180


def test_download_collaborator_preserves_ticket_and_payload_shape():
    store = {}
    asset = deepcopy(PUBLIC_METADATA)
    collaborator = RememberMeObDownloadLinkCollaborator(
        token_store=store,
        clock=lambda: 1000.0,
        token_factory=lambda: TOKEN_A,
        public_base_url="https://downloads.example.invalid/",
    )

    result = collaborator.create_download_link(asset)

    assert store == {
        TOKEN_A: {
            "asset_id": ASSET_ID,
            "expires_at": 1300.0,
            "get_count": 0,
        }
    }
    assert result == {
        "ok": True,
        "asset_id": ASSET_ID,
        "filename": "sample.png",
        "mime_type": "image/png",
        "stored_bytes": 91,
        "stored_sha256": STORED_SHA,
        "download_path": f"/rm/asset-download/{TOKEN_A}",
        "download_url": (
            "https://downloads.example.invalid"
            f"/rm/asset-download/{TOKEN_A}"
        ),
        "expires_in_seconds": 300,
    }
    assert asset == PUBLIC_METADATA
    assert "stored_relpath" not in result


@pytest.mark.parametrize(
    "base_url",
    [
        "https://example.invalid",
        "https://example.invalid/",
        "http://localhost:8080/",
        "http://127.0.0.1:8000",
        "http://[::1]:8000/",
    ],
)
def test_valid_public_base_url_produces_download_url(base_url):
    collaborator = RememberMeObDownloadLinkCollaborator(
        token_factory=lambda: TOKEN_A,
        public_base_url=base_url,
    )
    assert collaborator.create_download_link(
        PUBLIC_METADATA
    )["download_url"] == (
        f"{base_url.rstrip('/')}/rm/asset-download/{TOKEN_A}"
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "ftp://example.invalid",
        "https://user@example.invalid",
        "https://user:password@example.invalid",
        "https://example.invalid/;unexpected",
        "https://example.invalid\\unexpected",
        "https:\\\\example.invalid",
        "https://example.invalid/path",
        "https://example.invalid?query=yes",
        "https://example.invalid#fragment",
        "https://example.invalid\u00a0",
        "https://example.invalid\u200e",
        "https://example.invalid\n",
        "https://example.invalid\t",
        "https://example.invalid:",
        "not-a-url",
    ],
)
def test_invalid_public_base_url_produces_empty_download_url(base_url):
    collaborator = RememberMeObDownloadLinkCollaborator(
        token_factory=lambda: TOKEN_A,
        public_base_url=base_url,
    )
    assert collaborator.create_download_link(
        PUBLIC_METADATA
    )["download_url"] == ""


@pytest.mark.parametrize(
    "argument",
    [
        {"ttl_seconds": True},
        {"ttl_seconds": False},
        {"max_tokens": True},
        {"max_tokens": False},
    ],
)
def test_download_collaborator_rejects_bool_limits(argument):
    with pytest.raises(RememberMeDownloadLinkError) as caught:
        RememberMeObDownloadLinkCollaborator(**argument)
    assert caught.value.code == "download_unavailable"
    assert str(caught.value) == "download_unavailable"


def test_public_base_url_provider_is_supported():
    collaborator = RememberMeObDownloadLinkCollaborator(
        token_factory=lambda: TOKEN_A,
        public_base_url=lambda: "http://localhost:8080/",
    )
    assert collaborator.create_download_link(
        PUBLIC_METADATA
    )["download_url"].startswith(
        f"http://localhost:8080/rm/asset-download/{TOKEN_A}"
    )


def test_failing_base_url_provider_does_not_leave_ticket():
    store = {}
    collaborator = RememberMeObDownloadLinkCollaborator(
        token_store=store,
        token_factory=lambda: TOKEN_A,
        public_base_url=lambda: (_ for _ in ()).throw(
            OSError("private-token-value")
        ),
    )
    with pytest.raises(RememberMeDownloadLinkError) as caught:
        collaborator.create_download_link(PUBLIC_METADATA)
    assert caught.value.code == "download_unavailable"
    assert store == {}


def test_expired_tickets_are_cleaned_before_capacity_check():
    store = {
        "expired": {
            "asset_id": "old",
            "expires_at": 99.0,
            "get_count": 0,
        }
    }
    collaborator = RememberMeObDownloadLinkCollaborator(
        token_store=store,
        clock=lambda: 100.0,
        token_factory=lambda: TOKEN_A,
        max_tokens=1,
    )
    collaborator.create_download_link(PUBLIC_METADATA)
    assert "expired" not in store
    assert TOKEN_A in store


def test_full_ticket_store_raises_stable_error():
    store = {
        TOKEN_A: {
            "asset_id": "old",
            "expires_at": 200.0,
            "get_count": 0,
        }
    }
    collaborator = RememberMeObDownloadLinkCollaborator(
        token_store=store,
        clock=lambda: 100.0,
        token_factory=lambda: TOKEN_B,
        max_tokens=1,
    )
    with pytest.raises(RememberMeDownloadLinkError) as caught:
        collaborator.create_download_link(PUBLIC_METADATA)
    assert caught.value.code == "download_store_full"


def test_token_collision_does_not_overwrite_existing_ticket():
    old_ticket = {
        "asset_id": "old",
        "expires_at": 200.0,
        "get_count": 0,
    }
    store = {TOKEN_A: deepcopy(old_ticket)}
    tokens = iter((TOKEN_A, TOKEN_B))
    collaborator = RememberMeObDownloadLinkCollaborator(
        token_store=store,
        clock=lambda: 100.0,
        token_factory=lambda: next(tokens),
    )
    collaborator.create_download_link(PUBLIC_METADATA)
    assert store[TOKEN_A] == old_ticket
    assert store[TOKEN_B]["asset_id"] == ASSET_ID


@pytest.mark.parametrize(
    "factory",
    [
        lambda: "invalid token",
        lambda: (_ for _ in ()).throw(OSError("private path")),
    ],
)
def test_invalid_or_failing_token_factory_exits_safely(factory):
    collaborator = RememberMeObDownloadLinkCollaborator(
        token_factory=factory,
    )
    with pytest.raises(RememberMeDownloadLinkError) as caught:
        collaborator.create_download_link(PUBLIC_METADATA)
    assert caught.value.code == "download_unavailable"
    assert "private path" not in str(caught.value)


class SpyLock:
    def __init__(self):
        self._lock = threading.Lock()
        self.entries = 0

    def __enter__(self):
        self._lock.acquire()
        self.entries += 1
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._lock.release()


def test_concurrent_ticket_creation_uses_injected_lock():
    store = {}
    lock = SpyLock()
    counter_lock = threading.Lock()
    counter = iter(range(20))

    def token_factory():
        with counter_lock:
            value = next(counter)
        return f"{value:043d}"

    collaborator = RememberMeObDownloadLinkCollaborator(
        token_store=store,
        lock=lock,
        token_factory=token_factory,
    )
    threads = [
        threading.Thread(
            target=collaborator.create_download_link,
            args=(PUBLIC_METADATA,),
        )
        for _ in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(store) == 20
    assert lock.entries == 20


class RaisingMapping(Mapping):
    def __getitem__(self, key):
        raise KeyError("C:\\Users\\private\\asset.png")

    def __iter__(self) -> Iterator[str]:
        raise OSError("/private/data/asset.png")

    def __len__(self):
        return 1


class HostileDownloadLinks:
    def __init__(self, behavior):
        self.behavior = behavior

    def create_download_link(self, asset):
        if isinstance(self.behavior, BaseException):
            raise self.behavior
        return self.behavior


class UnknownDownloadFailure(Exception):
    pass


_SECRET_MARKERS = (
    r"C:\Users\private\asset.png",
    "/private/data/asset.png",
    "private-token-value",
    "https://production.example.invalid",
    "stored_relpath",
    "Traceback",
    STORED_SHA,
)


@pytest.mark.parametrize(
    "behavior",
    [
        RuntimeError("private-token-value"),
        ValueError("https://production.example.invalid"),
        KeyError("stored_relpath"),
        OSError("/private/data/asset.png"),
        UnknownDownloadFailure("C:\\Users\\private\\asset.png"),
        RaisingMapping(),
        {"ok": False, "error": "Traceback"},
        {"ok": True},
        "not-a-mapping",
    ],
)
def test_unknown_download_failures_are_fully_sanitized(behavior):
    presenter = _presenter(
        links=HostileDownloadLinks(behavior)
    )
    download = presenter.rm_asset_download_link(ASSET_ID)
    view = presenter.rm_asset_view(ASSET_ID)

    assert json.loads(download) == {
        "ok": False,
        "error": "download_unavailable",
    }
    assert view.structuredContent == {
        "ok": False,
        "error": "download_unavailable",
    }
    serialized = download + view.model_dump_json(by_alias=True)
    for marker in _SECRET_MARKERS:
        assert marker not in serialized


def test_known_download_store_full_error_is_preserved():
    presenter = _presenter(
        links=HostileDownloadLinks(
            RememberMeDownloadLinkError("download_store_full")
        )
    )
    assert json.loads(
        presenter.rm_asset_download_link(ASSET_ID)
    ) == {"ok": False, "error": "download_store_full"}


def test_stage8e_modules_remain_outside_production_import_paths():
    root = Path(__file__).resolve().parent.parent
    server_source = (root / "server.py").read_text(encoding="utf-8")
    assert "remember_me_mcp_presenter" not in server_source

    for relative in (
        "asset_dashboard.py",
        "asset_viewer.py",
        "asset_embedding_index.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "remember_me_mcp_presenter" not in source
        assert "remember_me_download_links" not in source
