import importlib
import sys

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "test-password")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def _bucket(
    bucket_id,
    *,
    name=None,
    content="",
    related=(),
    sealed=False,
    dormant=False,
    bucket_type="dynamic",
):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "name": name or bucket_id,
            "type": bucket_type,
            "domain": ["test"],
            "tags": [],
            "valence": 0.5,
            "arousal": 0.3,
            "importance": 5,
            "created": "2026-09-20T00:00:00",
            "last_active": "2026-09-20T00:00:00",
            "related_buckets": ",".join(related),
            "sealed": 1 if sealed else 0,
            "dormant": dormant,
        },
    }


def _client(server):
    app = Starlette(routes=[
        Route("/api/bucket/{bucket_id}", server.api_bucket_detail,
              methods=["GET"]),
    ])
    client = TestClient(app, base_url="http://testserver")
    token = server._create_session()
    client.cookies.set("ombre_session", token)
    return client


def _configure_detail(server, monkeypatch, current, all_buckets):
    async def get(bucket_id):
        return current if bucket_id == current["id"] else None

    calls = []

    async def list_all(include_archive=False):
        calls.append(include_archive)
        return all_buckets

    monkeypatch.setattr(server.bucket_mgr, "get", get)
    monkeypatch.setattr(server.bucket_mgr, "list_all", list_all)
    return calls


def test_dashboard_detail_returns_link_statuses_and_deduplicates_tokens(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    active = _bucket("111111111111", name="活动桶")
    sealed = _bucket("222222222222", name="封存桶", sealed=True)
    archived = _bucket("333333333333", name="归档桶", bucket_type="archived")
    dormant = _bucket("444444444444", name="休眠桶", dormant=True)
    current = _bucket(
        "aaaaaaaaaaaa",
        content=(
            "111111111111 222222222222 333333333333 444444444444 "
            "111111111111 deadbeefcafe"
        ),
    )
    calls = _configure_detail(
        server, monkeypatch, current, [current, active, sealed, archived, dormant]
    )

    response = _client(server).get("/api/bucket/aaaaaaaaaaaa")

    assert response.status_code == 200
    payload = response.json()
    assert calls == [True]
    assert set(payload["bucket_links"]) == {
        "111111111111", "222222222222", "333333333333", "444444444444",
        "deadbeefcafe",
    }
    assert payload["bucket_links"]["111111111111"] == {
        "id": active["id"], "exists": True, "name": "活动桶",
        "sealed": False, "dormant": False, "type": "dynamic",
    }
    assert payload["bucket_links"]["222222222222"]["sealed"] is True
    assert payload["bucket_links"]["222222222222"]["name"] == "封存桶"
    assert payload["bucket_links"]["333333333333"]["type"] == "archived"
    assert payload["bucket_links"]["444444444444"]["dormant"] is True
    assert payload["bucket_links"]["deadbeefcafe"] == {
        "id": "deadbeefcafe", "exists": False,
    }


def test_dashboard_detail_referenced_by_reuses_content_related_and_statuses(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    current = _bucket("aaaaaaaaaaaa", content="self aaaaaaaaaaaa")
    content_source = _bucket("111111111111", content="see aaaaaaaaaaaa")
    related_source = _bucket("222222222222", related=[current["id"]])
    both_source = _bucket(
        "333333333333", content="aaaaaaaaaaaa", related=[current["id"]],
        sealed=True,
    )
    archived_source = _bucket(
        "444444444444", content="aaaaaaaaaaaa", bucket_type="archived",
    )
    dormant_source = _bucket(
        "555555555555", related=[current["id"]], dormant=True,
    )
    supersession_only = _bucket("666666666666")
    supersession_only["metadata"]["superseded_by"] = current["id"]
    supersession_only["metadata"]["supersedes"] = [current["id"]]
    _configure_detail(
        server,
        monkeypatch,
        current,
        [
            current, content_source, related_source, both_source, archived_source,
            dormant_source, supersession_only,
        ],
    )

    payload = _client(server).get("/api/bucket/aaaaaaaaaaaa").json()
    references = {item["id"]: item for item in payload["referenced_by"]}

    assert set(references) == {
        current["id"], content_source["id"], related_source["id"],
        both_source["id"], archived_source["id"], dormant_source["id"],
    }
    assert references[current["id"]]["reference_kinds"] == ["content"]
    assert references[content_source["id"]]["reference_kinds"] == ["content"]
    assert references[related_source["id"]]["reference_kinds"] == ["related_buckets"]
    assert references[both_source["id"]]["reference_kinds"] == [
        "content", "related_buckets"
    ]
    assert references[both_source["id"]]["sealed"] is True
    assert references[archived_source["id"]]["type"] == "archived"
    assert references[dormant_source["id"]]["dormant"] is True


def test_dashboard_detail_missing_bucket_is_explicit_404(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    current = _bucket("aaaaaaaaaaaa")
    _configure_detail(server, monkeypatch, current, [current])

    response = _client(server).get("/api/bucket/bbbbbbbbbbbb")

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}
