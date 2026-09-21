import importlib
import sys
from unittest.mock import AsyncMock

import pytest

from bucket_manager import BucketManager


CONTENT = "public-literal-alias-marker"
UPDATED_CONTENT = "public-literal-alias-marker-updated"
TAG = "project/public-literal-alias-marker"


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.delenv("OMBRE_RM_RUNTIME_ENABLED", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    return server


@pytest.mark.asyncio
async def test_bucket_manager_preserves_literal_content_metadata_and_query(tmp_path):
    manager = BucketManager({"buckets_dir": str(tmp_path / "buckets")})
    bucket_id = await manager.create(
        content=CONTENT,
        name="public literal name",
        tags=[TAG],
        domain=["project"],
    )

    bucket = await manager.get(bucket_id)
    assert bucket["content"] == CONTENT
    assert bucket["metadata"]["name"] == "public literal name"
    assert bucket["metadata"]["tags"] == [TAG]
    assert bucket["metadata"]["domain"] == ["project"]

    assert await manager.update(
        bucket_id,
        content=UPDATED_CONTENT,
        name="updated public literal name",
        tags=[TAG],
        domain=["work"],
    )
    updated = await manager.get(bucket_id)
    assert updated["content"] == UPDATED_CONTENT
    assert updated["metadata"]["name"] == "updated public literal name"
    assert updated["metadata"]["tags"] == [TAG]
    assert updated["metadata"]["domain"] == ["work"]

    results = await manager.search(UPDATED_CONTENT)
    assert [item["id"] for item in results] == [bucket_id]


@pytest.mark.asyncio
async def test_hold_grow_breath_tags_and_conflict_keep_literals_unchanged(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    analysis = {
        "domain": ["project"],
        "valence": 0.5,
        "arousal": 0.5,
        "tags": [],
        "suggested_name": "",
        "todos": [],
    }
    server.dehydrator.analyze = AsyncMock(return_value=analysis)
    server._similarity_doorbell = AsyncMock(return_value="")
    server._detect_conflict_warning = AsyncMock(return_value="")
    server._merge_or_create = AsyncMock(return_value=("created", False))

    await server.hold(content=CONTENT, tags=TAG)
    hold_kwargs = server._merge_or_create.await_args.kwargs
    assert hold_kwargs["content"] == CONTENT
    assert hold_kwargs["tags"] == [TAG]

    server._merge_or_create.reset_mock()
    await server.grow(CONTENT)
    grow_kwargs = server._merge_or_create.await_args.kwargs
    assert grow_kwargs["content"] == CONTENT
    assert server._detect_conflict_warning.await_args.args[0] == CONTENT

    assert set(CONTENT.split("-")) <= server._conflict_tokens(CONTENT)
    assert server._normalize_breath_filter([TAG], "tags_filter") == [TAG]

    captured = {}

    async def capture_breath(*args, **kwargs):
        captured["query"] = kwargs["query"]
        return "breath-result"

    monkeypatch.setattr(server, "_breath_impl", capture_breath)
    assert (await server.breath(query=CONTENT, touch=False)).startswith("breath-result")
    assert captured["query"] == CONTENT
