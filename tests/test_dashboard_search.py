import importlib
import sys
from unittest.mock import AsyncMock

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "test-password")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def _client(server):
    app = Starlette(routes=[Route("/api/search", server.api_search, methods=["GET"])])
    client = TestClient(app, base_url="http://testserver")
    token = server._create_session()
    client.cookies.set("ombre_session", token)
    return client


def _bucket(bucket_id, *, name=None, content="", related=(), sealed=False):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "name": name or bucket_id,
            "type": "dynamic",
            "domain": ["test"],
            "tags": [],
            "valence": 0.5,
            "arousal": 0.3,
            "importance": 5,
            "created": "2026-09-19T00:00:00",
            "last_active": "2026-09-19T00:00:00",
            "related_buckets": ",".join(related),
            "sealed": 1 if sealed else 0,
        },
    }


def _configure_search(server, monkeypatch, buckets, generic=()):
    async def list_all(include_archive=False):
        assert include_archive is True
        return buckets

    search = AsyncMock(return_value=list(generic))
    monkeypatch.setattr(server.bucket_mgr, "list_all", list_all)
    monkeypatch.setattr(server.bucket_mgr, "search", search)
    return search


@pytest.mark.parametrize("query", [
    "[bucket_id:e24f331b163e]",
    "bucket_id:e24f331b163e",
    "e24f331b163e",
    "e24f331b163e name='示例'",
])
def test_dashboard_search_extracts_common_bucket_id_formats(tmp_path, monkeypatch, query):
    server = _load_server(tmp_path, monkeypatch)
    target = _bucket("e24f331b163e")
    search = _configure_search(server, monkeypatch, [target])

    payload = _client(server).get("/api/search", params={"q": query}).json()

    assert payload["mode"] == "id"
    assert payload["normalized_query"] == target["id"]
    assert [item["id"] for item in payload["groups"]["id_matches"]] == [target["id"]]
    search.assert_awaited_once_with(target["id"], limit=10, include_sealed=True)


def test_dashboard_search_id_groups_references_dedupe_and_marks_sealed(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    target = _bucket("e24f331b163e", sealed=True)
    source = _bucket(
        "aabbccddeeff",
        content="see e24f331b163e again e24f331b163e",
        related=[target["id"]],
    )
    _configure_search(server, monkeypatch, [target, source])

    payload = _client(server).get("/api/search", params={"q": target["id"]}).json()

    assert payload["mode"] == "id"
    assert len(payload["groups"]["id_matches"]) == 1
    id_match = payload["groups"]["id_matches"][0]
    assert id_match["id"] == target["id"]
    assert id_match["sealed"] is True
    assert id_match["match_reason"] == "id_exact"
    assert [item["id"] for item in payload["groups"]["references"]] == [source["id"]]
    assert payload["groups"]["references"][0]["reference_kinds"] == [
        "content", "related_buckets"
    ]


def test_dashboard_search_supports_single_and_multiple_six_character_prefixes(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    single = _bucket("aaa111222333")
    first = _bucket("bbb222000001")
    second = _bucket("bbb222000002")
    _configure_search(server, monkeypatch, [second, single, first])
    client = _client(server)

    single_payload = client.get("/api/search", params={"q": "aaa111"}).json()
    multiple_payload = client.get("/api/search", params={"q": "bbb222"}).json()

    assert [item["id"] for item in single_payload["groups"]["id_matches"]] == [single["id"]]
    assert [item["id"] for item in multiple_payload["groups"]["id_matches"]] == [
        first["id"], second["id"]
    ]


def test_dashboard_search_short_hex_is_text_and_missing_id_keeps_explicit_empty_groups(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    generic = _bucket("111111111111", name="普通相关记忆")
    search = _configure_search(server, monkeypatch, [], generic=[generic])
    client = _client(server)

    short = client.get("/api/search", params={"q": "abcde"}).json()
    missing = client.get("/api/search", params={"q": "id:ffffffffffff"}).json()

    assert short["mode"] == "text"
    assert short["groups"]["id_matches"] == []
    assert [item["id"] for item in short["groups"]["related"]] == [generic["id"]]
    assert missing["mode"] == "id"
    assert missing["groups"]["id_matches"] == []
    assert missing["groups"]["references"] == []
    assert [item["id"] for item in missing["groups"]["related"]] == [generic["id"]]
    assert search.await_args_list[0].args[0] == "abcde"
    assert search.await_args_list[1].args[0] == "ffffffffffff"


def test_dashboard_search_id_prefix_forces_id_and_name_prefix_never_becomes_id(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    target = _bucket("e24f331b163e", name="abcdef123456")
    search = _configure_search(server, monkeypatch, [target])
    client = _client(server)

    explicit_id = client.get("/api/search", params={"q": "id:e24f331b163e"}).json()
    explicit_name = client.get("/api/search", params={"q": "name:abcdef123456"}).json()

    assert explicit_id["mode"] == "id"
    assert [item["id"] for item in explicit_id["groups"]["id_matches"]] == [target["id"]]
    assert explicit_name["mode"] == "name"
    assert explicit_name["groups"]["id_matches"] == []
    assert explicit_name["groups"]["references"] == []
    assert explicit_name["groups"]["related"][0]["match_reason"] == "name"
    assert explicit_name["groups"]["related"][0]["id"] == target["id"]
    assert search.await_args_list[1].args[0] == "abcdef123456"


def test_dashboard_search_preserves_generic_hybrid_results_for_plain_text(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    generic = _bucket("123456abcdef", name="混合检索结果")
    generic["score"] = 77.5
    search = _configure_search(server, monkeypatch, [generic], generic=[generic])

    payload = _client(server).get("/api/search", params={"q": "普通关键词"}).json()

    assert payload["mode"] == "text"
    assert [item["id"] for item in payload["groups"]["related"]] == [generic["id"]]
    assert payload["groups"]["related"][0]["score"] == 77.5
    search.assert_awaited_once_with("普通关键词", limit=10, include_sealed=True)
