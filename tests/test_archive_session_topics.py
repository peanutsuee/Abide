import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import frontmatter
import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "test-seal-topics")
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: content[:120]
    )
    return server


def _bucket_id(result: str) -> str:
    return result.split("bucket_id:", 1)[1].strip()


@pytest.mark.asyncio
async def test_archive_session_topics_normalize_persist_and_reload(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    topics = [
        " 项目/RM ",
        "学习/生化",
        "  ",
        "",
        "项目/RM",
        " Foo  /  Bar ",
        "Foo  /  Bar",
        "关系/沟通",
    ]

    result = await server.archive_session("topic round-trip", topics=topics)
    bucket_id = _bucket_id(result)
    bucket = await server.bucket_mgr.get(bucket_id)

    expected = ["项目/RM", "学习/生化", "Foo  /  Bar", "关系/沟通"]
    assert bucket["metadata"]["topics"] == expected

    post = frontmatter.load(bucket["path"])
    assert post["topics"] == expected

    reopened = server.BucketManager({"buckets_dir": str(tmp_path / "buckets")})
    reloaded = await reopened.get(bucket_id)
    assert reloaded["metadata"]["topics"] == expected


@pytest.mark.asyncio
async def test_archive_session_topics_omitted_none_and_empty_persist_empty_lists(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)

    results = [
        await server.archive_session("topics omitted"),
        await server.archive_session("topics none", topics=None),
        await server.archive_session("topics empty", topics=[]),
    ]

    for result in results:
        bucket = await server.bucket_mgr.get(_bucket_id(result))
        assert bucket["metadata"]["topics"] == []
        assert frontmatter.load(bucket["path"])["topics"] == []


@pytest.mark.asyncio
async def test_archive_session_topics_reject_non_string_without_persisting(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)

    result = await server.archive_session("invalid topics", topics=["valid", 123])

    assert result == "topics must be a list of strings."
    assert await server.bucket_mgr.list_all(include_archive=True) == []


@pytest.mark.asyncio
async def test_old_archive_without_topics_reads_as_logical_empty_list(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    archive_dir = Path(tmp_path / "buckets" / "archive" / "session")
    archive_dir.mkdir(parents=True, exist_ok=True)
    legacy_path = archive_dir / "legacy-session.md"
    legacy_path.write_text(
        frontmatter.dumps(
            frontmatter.Post(
                "# legacy session\n\n## Summary\nold archive",
                id="legacy-session",
                name="legacy-session",
                tags=["session", "archive"],
                domain=["session"],
                type="archived",
                sealed=0,
            )
        ),
        encoding="utf-8",
    )

    bucket = await server.bucket_mgr.get("legacy-session")

    assert "topics" not in bucket["metadata"]
    assert bucket["metadata"].get("topics", []) == []


@pytest.mark.asyncio
async def test_sealed_archive_topics_do_not_create_new_mcp_output_surface(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    topic = "sealed-only-topic"

    result = await server.archive_session(
        "sealed topic archive",
        topics=[topic],
        sealed=True,
    )
    bucket_id = _bucket_id(result)
    bucket = await server.bucket_mgr.get(bucket_id)

    assert bucket["metadata"]["topics"] == [topic]
    assert bucket["metadata"]["sealed"] == 1
    assert bucket_id not in await server.boot()
    assert topic not in await server.breath(domain="session")


@pytest.mark.asyncio
async def test_archive_session_mcp_schema_has_optional_topic_strings(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server.mcp) as client:
        tools = (await client.list_tools()).tools

    schema = next(tool.inputSchema for tool in tools if tool.name == "archive_session")
    topics_schema = schema["properties"]["topics"]
    array_schema = next(
        option for option in topics_schema["anyOf"] if option.get("type") == "array"
    )

    assert list(schema["properties"]) == [
        "summary",
        "highlights",
        "mood",
        "valence",
        "arousal",
        "letter",
        "sealed",
        "topics",
    ]
    assert schema["required"] == ["summary"]
    assert array_schema == {"items": {"type": "string"}, "type": "array"}
    assert topics_schema["default"] is None
    assert topics_schema["description"] == (
        "Optional structured topic labels describing the main subjects "
        "covered by the archived session."
    )
