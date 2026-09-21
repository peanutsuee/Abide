import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import frontmatter
import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "test-seal-filters")
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: content[:120]
    )
    return server


def _bucket_id(result: str) -> str:
    return result.split("bucket_id:", 1)[1].split()[0]


def _set_bucket_date(server, bucket_id: str, date: str) -> None:
    path = server.bucket_mgr._find_bucket_file(bucket_id)
    post = frontmatter.load(path)
    post["updated_at"] = date
    post["last_active"] = f"{date}T12:00:00"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(post))


@pytest.mark.asyncio
async def test_tags_filter_exact_case_sensitive_slash_unicode_and_any(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    exact_id = await server.bucket_mgr.create(
        content="exact tag result",
        tags=["项目/RM", "研究"],
    )
    case_id = await server.bucket_mgr.create(
        content="case mismatch",
        tags=["项目/rm"],
    )
    other_id = await server.bucket_mgr.create(
        content="other tag result",
        tags=["日常"],
    )

    result = await server.breath(
        tags_filter=[" 项目/RM ", "不存在"],
        max_results=10,
    )

    assert exact_id in result
    assert case_id not in result
    assert other_id not in result


@pytest.mark.asyncio
async def test_topic_filter_routes_archived_sessions_and_old_archives_are_safe(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    matching_id = _bucket_id(
        await server.archive_session("matching topic", topics=["项目/RM"])
    )
    other_id = _bucket_id(
        await server.archive_session("other topic", topics=["项目/OB"])
    )

    archive_dir = Path(tmp_path / "buckets" / "archive" / "session")
    archive_dir.mkdir(parents=True, exist_ok=True)
    legacy_path = archive_dir / "legacy-session.md"
    legacy_path.write_text(
        frontmatter.dumps(
            frontmatter.Post(
                "# legacy session\n\n## Summary\nlegacy",
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

    result = await server.breath(topic_filter=["项目/RM"], max_results=10)

    assert matching_id in result
    assert other_id not in result
    assert "legacy-session" not in result


@pytest.mark.asyncio
async def test_topic_and_tag_filters_are_conjunctive(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    matching_id = _bucket_id(
        await server.archive_session("matching combined", topics=["项目/RM"])
    )

    result = await server.breath(
        tags_filter=["session"],
        topic_filter=["项目/RM"],
    )
    rejected = await server.breath(
        tags_filter=["not-a-session-tag"],
        topic_filter=["项目/RM"],
    )

    assert matching_id in result
    assert matching_id not in rejected
    assert "没有找到对话归档。" in rejected


@pytest.mark.asyncio
async def test_query_filter_has_no_semantic_fallback_and_restricts_vectors(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    desired_id = await server.bucket_mgr.create(
        content="needle desired",
        tags=["wanted"],
    )
    unrelated_id = await server.bucket_mgr.create(
        content="needle unrelated",
        tags=["other"],
    )

    embedding = MagicMock()
    embedding.enabled = True
    embedding.search_similar = AsyncMock(
        return_value=[(unrelated_id, 0.99), (desired_id, 0.80)]
    )
    server.bucket_mgr.embedding_engine = embedding

    result = await server.breath(query="needle", tags_filter=["wanted"])
    call = embedding.search_similar.await_args

    assert desired_id in result
    assert unrelated_id not in result
    assert call.kwargs["candidate_ids"] == {desired_id}

    no_match = await server.breath(query="needle", tags_filter=["missing"])
    assert "未找到相关记忆。" in no_match
    assert desired_id not in no_match
    assert unrelated_id not in no_match


@pytest.mark.asyncio
async def test_query_topic_filter_uses_existing_session_query_behavior(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    matching_id = _bucket_id(
        await server.archive_session("needle in matching session", topics=["项目/RM"])
    )
    other_id = _bucket_id(
        await server.archive_session("needle in another session", topics=["项目/OB"])
    )

    result = await server.breath(
        query="needle in matching",
        topic_filter=["项目/RM"],
    )

    assert matching_id in result
    assert other_id not in result


@pytest.mark.asyncio
async def test_structured_filters_respect_dates_and_domain(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    inside_id = await server.bucket_mgr.create(
        content="inside date",
        tags=["dated"],
        domain=["project"],
    )
    outside_id = await server.bucket_mgr.create(
        content="outside date",
        tags=["dated"],
        domain=["project"],
    )
    _set_bucket_date(server, inside_id, "2026-06-10")
    _set_bucket_date(server, outside_id, "2026-05-01")

    result = await server.breath(
        tags_filter=["dated"],
        domain="project",
        date_from="2026-06-01",
        date_to="2026-06-12",
    )
    wrong_domain = await server.breath(
        tags_filter=["dated"],
        domain="missing-domain",
    )

    assert inside_id in result
    assert outside_id not in result
    assert inside_id not in wrong_domain
    assert outside_id not in wrong_domain


@pytest.mark.asyncio
async def test_topic_filter_domain_mismatch_does_not_fallback(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    session_id = _bucket_id(
        await server.archive_session("topic session", topics=["项目/RM"])
    )

    result = await server.breath(
        domain="feel",
        topic_filter=["项目/RM"],
    )

    assert session_id not in result
    assert "没有找到对话归档。" in result


@pytest.mark.asyncio
async def test_filter_only_is_newest_first_and_bypasses_random_surfacing(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    newest_id = await server.bucket_mgr.create(
        content="newest exact result",
        tags=["ordered"],
    )
    older_id = await server.bucket_mgr.create(
        content="older exact result",
        tags=["ordered"],
    )
    _set_bucket_date(server, newest_id, "2026-06-10")
    _set_bucket_date(server, older_id, "2026-05-01")
    shuffle = MagicMock(side_effect=AssertionError("shuffle used"))
    monkeypatch.setattr(server.random, "shuffle", shuffle)

    result = await server.breath(tags_filter=["ordered"], max_results=2)

    assert result.index(newest_id) < result.index(older_id)
    shuffle.assert_not_called()


@pytest.mark.asyncio
async def test_empty_filter_preserves_historical_surfacing(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    await server.bucket_mgr.create(content="surfacing one", importance=5)
    await server.bucket_mgr.create(content="surfacing two", importance=5)
    shuffle = MagicMock()
    monkeypatch.setattr(server.random, "shuffle", shuffle)

    await server.breath(tags_filter=[])

    shuffle.assert_called()


@pytest.mark.asyncio
async def test_filter_normalization_rejects_blank_and_non_string_values(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)

    blank = await server.breath(tags_filter=["  "])
    non_string = await server.breath(topic_filter=[123])

    assert "cannot contain blank" in blank
    assert "only strings" in non_string


@pytest.mark.asyncio
async def test_malformed_legacy_tags_are_tolerated_without_stringifying_values(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    scalar_id = await server.bucket_mgr.create(content="scalar legacy")
    malformed_id = await server.bucket_mgr.create(content="malformed list")

    scalar_path = server.bucket_mgr._find_bucket_file(scalar_id)
    scalar_post = frontmatter.load(scalar_path)
    scalar_post["tags"] = "legacy,tag"
    with open(scalar_path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(scalar_post))

    malformed_path = server.bucket_mgr._find_bucket_file(malformed_id)
    malformed_post = frontmatter.load(malformed_path)
    malformed_post["tags"] = [123, "safe"]
    with open(malformed_path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(malformed_post))

    scalar_result = await server.breath(tags_filter=["legacy"])
    numeric_result = await server.breath(tags_filter=["123"])

    assert scalar_id in scalar_result
    assert malformed_id not in numeric_result


@pytest.mark.asyncio
async def test_sealed_exact_matches_are_hidden_before_filtering_and_authorized_include_works(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    sealed_id = await server.bucket_mgr.create(
        content="sealed exact tag",
        tags=["secret-filter"],
    )
    await server.trace(sealed_id, sealed=1)

    hidden = await server.breath(tags_filter=["secret-filter"])
    included = await server.breath(
        tags_filter=["secret-filter"],
        include_sealed=True,
    )

    assert sealed_id not in hidden
    assert "未找到相关记忆。" in hidden
    assert sealed_id in included


@pytest.mark.asyncio
async def test_filtered_active_results_touch_but_filtered_out_records_do_not(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    selected_id = await server.bucket_mgr.create(
        content="selected touch",
        tags=["touchable"],
    )
    excluded_id = await server.bucket_mgr.create(
        content="excluded touch",
        tags=["other"],
    )
    selected_before = (await server.bucket_mgr.get(selected_id))["metadata"][
        "activation_count"
    ]
    excluded_before = (await server.bucket_mgr.get(excluded_id))["metadata"][
        "activation_count"
    ]

    result = await server.breath(tags_filter=["touchable"])

    selected_after = (await server.bucket_mgr.get(selected_id))["metadata"][
        "activation_count"
    ]
    excluded_after = (await server.bucket_mgr.get(excluded_id))["metadata"][
        "activation_count"
    ]
    assert selected_id in result
    assert selected_after > selected_before
    assert excluded_after == excluded_before


@pytest.mark.asyncio
async def test_session_and_feel_filtered_routes_do_not_touch(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    session_id = _bucket_id(
        await server.archive_session("no touch session", topics=["no-touch"])
    )
    feel_id = await server.bucket_mgr.create(
        content="no touch feel",
        tags=["no-touch-feel"],
        bucket_type="feel",
    )
    session_before = (await server.bucket_mgr.get(session_id))["metadata"][
        "activation_count"
    ]
    feel_before = (await server.bucket_mgr.get(feel_id))["metadata"][
        "activation_count"
    ]

    await server.breath(topic_filter=["no-touch"])
    await server.breath(domain="feel", tags_filter=["no-touch-feel"])

    assert (await server.bucket_mgr.get(session_id))["metadata"][
        "activation_count"
    ] == session_before
    assert (await server.bucket_mgr.get(feel_id))["metadata"][
        "activation_count"
    ] == feel_before


@pytest.mark.asyncio
async def test_mcp_schema_has_nullable_string_arrays_and_descriptions(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server.mcp) as client:
        tools = (await client.list_tools()).tools
    schema = next(tool.inputSchema for tool in tools if tool.name == "breath")

    assert schema.get("required", []) == []
    for name, description in (
        (
            "cursor",
            "Opaque cursor returned by a previous query Breath page. Reuse it with the same query and filters.",
        ),
        ("tags_filter", "Optional exact bucket-tag filters. Any listed tag may match."),
        (
            "topic_filter",
            "Optional exact archived-session topic filters. Any listed topic may match.",
        ),
    ):
        property_schema = schema["properties"][name]
        if name != "cursor":
            array_schema = next(
                option for option in property_schema["anyOf"] if option.get("type") == "array"
            )
            assert array_schema == {"items": {"type": "string"}, "type": "array"}
            assert property_schema["default"] is None
        else:
            assert property_schema["default"] == ""
            assert property_schema["type"] == "string"
        assert property_schema["description"] == description


@pytest.mark.asyncio
async def test_embedding_search_candidate_restriction_beats_global_top_n(
    test_config,
):
    from embedding_engine import EmbeddingEngine

    config = {
        **test_config,
        "embedding": {
            **test_config["embedding"],
            "enabled": True,
            "api_key": "test-key",
        },
    }
    engine = EmbeddingEngine(config)
    engine._generate_embedding = AsyncMock(return_value=[1.0, 0.0])
    engine._store_embedding("global-top", [1.0, 0.0])
    engine._store_embedding("filtered-valid", [0.8, 0.6])

    unrestricted = await engine.search_similar("query", top_k=1)
    restricted = await engine.search_similar(
        "query",
        top_k=1,
        candidate_ids={"filtered-valid"},
    )

    assert unrestricted[0][0] == "global-top"
    assert restricted[0][0] == "filtered-valid"
