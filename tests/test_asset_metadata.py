import concurrent.futures
import hashlib
import importlib
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from PIL import Image

from asset_store import AssetStore, AssetStoreError


def _persist(store, data, filename="asset.bin", mime_type="application/octet-stream"):
    source = store.create_temp_path()
    source.write_bytes(data)
    return store.persist_upload(
        source,
        hashlib.sha256(data).hexdigest(),
        len(data),
        filename,
        mime_type,
    )


def _png_bytes(color="blue"):
    image = Image.new("RGB", (8, 6), color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    image.close()
    return output.getvalue()


def _set_created_at(store, asset_id, value):
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE assets SET created_at = ?, updated_at = ? WHERE asset_id = ?",
            (value, value, asset_id),
        )


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def test_stage1_database_migrates_without_losing_asset(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    db_path = root / "assets.sqlite3"
    created_at = "2026-07-01T01:02:03+00:00"
    asset_id = "a" * 32
    stored_sha = "b" * 64
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE assets (
                asset_id TEXT PRIMARY KEY,
                source_sha256 TEXT NOT NULL,
                stored_sha256 TEXT NOT NULL UNIQUE,
                stored_relpath TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                kind TEXT NOT NULL,
                decoded_bytes INTEGER NOT NULL,
                stored_bytes INTEGER NOT NULL,
                width INTEGER NOT NULL DEFAULT 0,
                height INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                stored_sha,
                stored_sha,
                f"assets/{stored_sha[:2]}/{stored_sha}.bin",
                "legacy.bin",
                "application/octet-stream",
                "file",
                12,
                12,
                0,
                0,
                created_at,
            ),
        )

    store = AssetStore(root)
    migrated = store.get(asset_id)
    assert migrated["original_filename"] == "legacy.bin"
    assert migrated["title"] == ""
    assert migrated["description"] == ""
    assert migrated["tags"] == []
    assert migrated["updated_at"] == created_at
    with sqlite3.connect(store.db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(assets)")}
        tag_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(asset_tags)")
        }
        indexes = {
            row[1] for row in conn.execute(
                "SELECT type, name FROM sqlite_master WHERE type = 'index'"
            )
        }
    assert {"title", "description", "updated_at"}.issubset(columns)
    assert {
        "asset_id", "tag_normalized", "tag_display", "created_at",
    }.issubset(tag_columns)
    assert {
        "idx_asset_tags_asset_id", "idx_asset_tags_normalized",
    }.issubset(indexes)

    restarted = AssetStore(root)
    assert restarted.get(asset_id) == migrated


def test_metadata_update_semantics_normalization_and_persistence(tmp_path):
    store = AssetStore(tmp_path / "data")
    payload = b"metadata must not alter asset bytes"
    asset = _persist(store, payload, "original.bin")
    path = store.resolve_file(asset["asset_id"])[1]
    original_bytes = path.read_bytes()
    original_hash = asset["stored_sha256"]
    original_size = asset["stored_bytes"]

    updated = store.update_metadata(
        asset["asset_id"],
        title="  ＲＭ title\x00  ",
        description="  中文描述\n第二行  ",
        tags=[" Work ", "work", "", "旅行", "Ｔａｇ", "tag"],
    )
    assert updated["title"] == "RM title"
    assert updated["description"] == "中文描述 第二行"
    assert updated["tags"] == ["Tag", "Work", "旅行"]

    idempotent = store.update_metadata(
        asset["asset_id"],
        tags=["tag", "WORK", "旅行"],
    )
    assert idempotent["tags"] == ["Tag", "Work", "旅行"]
    preserved = store.update_metadata(asset["asset_id"])
    assert preserved["title"] == "RM title"
    assert preserved["description"] == "中文描述 第二行"
    assert preserved["tags"] == ["Tag", "Work", "旅行"]

    cleared = store.update_metadata(
        asset["asset_id"],
        title="",
        description="",
        tags=[],
    )
    assert cleared["title"] == ""
    assert cleared["description"] == ""
    assert cleared["tags"] == []
    assert path.read_bytes() == original_bytes
    assert cleared["stored_sha256"] == original_hash
    assert cleared["stored_bytes"] == original_size

    restored = AssetStore(store.data_root).get(asset["asset_id"])
    assert restored["title"] == ""
    assert restored["description"] == ""
    assert restored["tags"] == []


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"title": "x" * 201}, "title_too_long"),
        ({"description": "x" * 4001}, "description_too_long"),
        ({"tags": [f"tag-{index}" for index in range(31)]}, "too_many_tags"),
        ({"tags": ["x" * 65]}, "tag_too_long"),
        ({"tags": "not-a-list"}, "invalid_tags"),
        ({"tags": [123]}, "invalid_tag"),
    ],
)
def test_metadata_validation_limits(tmp_path, kwargs, error):
    store = AssetStore(tmp_path / "data")
    asset = _persist(store, b"validation")
    with pytest.raises(AssetStoreError, match=error):
        store.update_metadata(asset["asset_id"], **kwargs)
    assert store.get(asset["asset_id"])["title"] == ""


def test_unknown_asset_metadata_update_is_rejected(tmp_path):
    store = AssetStore(tmp_path / "data")
    with pytest.raises(AssetStoreError, match="asset_unavailable"):
        store.update_metadata("f" * 32, title="missing")


def test_search_text_ranking_reasons_and_casefold(tmp_path):
    store = AssetStore(tmp_path / "data")
    tag_asset = _persist(store, b"tag", "tag.bin")
    title_asset = _persist(store, b"title", "title.bin")
    filename_asset = _persist(store, b"filename", "Alpha-document.bin")
    description_asset = _persist(store, b"description", "notes.bin")
    chinese_asset = _persist(store, b"chinese", "cn.bin")

    store.update_metadata(tag_asset["asset_id"], tags=["Alpha"])
    store.update_metadata(title_asset["asset_id"], title="ALPHA")
    store.update_metadata(
        description_asset["asset_id"],
        description="Contains alpha in the body",
    )
    store.update_metadata(
        chinese_asset["asset_id"],
        title="旅行照片合集",
        description="这是一次夏日旅行记录",
    )
    for index, asset in enumerate(
        [tag_asset, title_asset, filename_asset, description_asset, chinese_asset],
        start=1,
    ):
        _set_created_at(
            store,
            asset["asset_id"],
            f"2026-07-0{index}T00:00:00+00:00",
        )

    result = store.search(query="aLpHa")
    assert [item["asset_id"] for item in result["results"]] == [
        tag_asset["asset_id"],
        title_asset["asset_id"],
        filename_asset["asset_id"],
        description_asset["asset_id"],
    ]
    assert result["results"][0]["match_reasons"] == ["tag_exact"]
    assert result["results"][1]["match_reasons"] == ["title_exact"]
    assert result["results"][2]["match_reasons"] == ["filename"]
    assert result["results"][3]["match_reasons"] == ["description"]

    chinese = store.search(query="夏日旅行")
    assert [item["asset_id"] for item in chinese["results"]] == [
        chinese_asset["asset_id"]
    ]
    assert chinese["results"][0]["match_reasons"] == ["description"]

    exact_id = store.search(query=title_asset["asset_id"])
    assert exact_id["results"][0]["asset_id"] == title_asset["asset_id"]
    assert exact_id["results"][0]["match_reasons"][0] == "asset_id_exact"


def test_search_filters_recent_order_and_pagination(tmp_path):
    store = AssetStore(tmp_path / "data")
    first = _persist(store, b"one", "one.bin")
    second = _persist(store, b"two", "two.bin")
    image = _persist(store, _png_bytes(), "photo.png", "image/png")
    store.update_metadata(first["asset_id"], tags=["shared", "one"])
    store.update_metadata(second["asset_id"], tags=["shared", "two"])
    store.update_metadata(image["asset_id"], tags=["shared", "two"])
    _set_created_at(store, first["asset_id"], "2026-06-01T00:00:00+00:00")
    _set_created_at(store, second["asset_id"], "2026-07-01T00:00:00+00:00")
    _set_created_at(store, image["asset_id"], "2026-07-15T00:00:00+00:00")

    recent = store.search()
    assert [item["asset_id"] for item in recent["results"]] == [
        image["asset_id"], second["asset_id"], first["asset_id"],
    ]
    assert all(item["match_reasons"] == [] for item in recent["results"])

    assert store.search(tags=["shared", "TWO"])["total"] == 2
    assert store.search(kind="image")["results"][0]["asset_id"] == image["asset_id"]
    assert store.search(mime_type="image/png")["total"] == 1
    dated = store.search(
        created_from="2026-07-01",
        created_to="2026-07-15",
    )
    assert {item["asset_id"] for item in dated["results"]} == {
        second["asset_id"], image["asset_id"],
    }
    page = store.search(limit=1, offset=1)
    assert page["total"] == 3
    assert page["offset"] == 1
    assert page["limit"] == 1
    assert page["results"][0]["asset_id"] == second["asset_id"]


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"limit": 0}, "invalid_limit"),
        ({"limit": 51}, "invalid_limit"),
        ({"offset": -1}, "invalid_offset"),
        ({"kind": "video"}, "invalid_kind"),
        ({"mime_type": "text/plain"}, "invalid_mime_type"),
        ({"created_from": "not-a-date"}, "invalid_created_from"),
        (
            {"created_from": "2026-08-01", "created_to": "2026-07-01"},
            "invalid_date_range",
        ),
    ],
)
def test_search_invalid_parameters(tmp_path, kwargs, error):
    store = AssetStore(tmp_path / "data")
    _persist(store, b"search validation")
    with pytest.raises(AssetStoreError, match=error):
        store.search(**kwargs)


def test_concurrent_metadata_updates_and_search_are_consistent(tmp_path):
    store = AssetStore(tmp_path / "data")
    asset = _persist(store, b"concurrent metadata", "concurrent.bin")
    original = store.get(asset["asset_id"])

    def update(index):
        return store.update_metadata(
            asset["asset_id"],
            title=f"title-{index}",
            tags=["shared", f"tag-{index % 4}"],
        )

    def search(_):
        return store.search(query="title", tags=["shared"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        futures = [
            executor.submit(update, index) for index in range(20)
        ] + [
            executor.submit(search, index) for index in range(20)
        ]
        results = [future.result() for future in futures]

    assert len(results) == 40
    final = store.get(asset["asset_id"])
    assert final["title"].startswith("title-")
    assert "shared" in [tag.casefold() for tag in final["tags"]]
    assert final["stored_sha256"] == original["stored_sha256"]
    assert final["stored_bytes"] == original["stored_bytes"]
    assert store.resolve_file(asset["asset_id"])[1].read_bytes() == b"concurrent metadata"


@pytest.mark.asyncio
async def test_stage2_tools_return_safe_metadata_and_errors(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    asset = _persist(server.asset_store, b"tool metadata", "../../safe.bin")

    update_text = await server.rm_asset_update_metadata(
        asset["asset_id"],
        title="Tool title",
        description="Tool description",
        tags=["Tool", "tool"],
    )
    update = json.loads(update_text)
    assert update["ok"] is True
    assert update["tags"] == ["Tool"]

    get_text = await server.rm_asset_get(asset["asset_id"])
    fetched = json.loads(get_text)
    assert fetched["title"] == "Tool title"
    assert fetched["description"] == "Tool description"
    assert fetched["tags"] == ["Tool"]

    search_text = await server.rm_asset_search(query="tool")
    search = json.loads(search_text)
    assert search["ok"] is True
    assert search["total"] == 1
    assert search["results"][0]["asset_id"] == asset["asset_id"]

    missing = json.loads(
        await server.rm_asset_update_metadata("f" * 32, title="missing")
    )
    invalid = json.loads(await server.rm_asset_search(limit=0))
    assert missing == {"ok": False, "error": "asset_unavailable"}
    assert invalid == {"ok": False, "error": "invalid_limit"}

    combined = update_text + get_text + search_text
    for forbidden in (
        "stored_relpath",
        "data_base64",
        "download_token",
        "upload_token",
        str(tmp_path),
    ):
        assert forbidden not in combined
