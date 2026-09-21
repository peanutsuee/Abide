import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from bucket_manager import BucketManager


def _bucket(bucket_id, content="memory", **metadata):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "name": metadata.pop("name", bucket_id),
            "domain": metadata.pop("domain", ["test"]),
            "type": metadata.pop("type", "dynamic"),
            "importance": metadata.pop("importance", 5),
            "valence": metadata.pop("valence", 0.5),
            "arousal": metadata.pop("arousal", 0.3),
            **metadata,
        },
    }


def _fixed_scores(manager, *, topic=0.6, exact=0.0, emotion=0.6, time=0.6):
    manager._calc_topic_score = MagicMock(return_value=topic)
    manager._calc_exact_match_score = MagicMock(return_value=exact)
    manager._calc_emotion_score = MagicMock(return_value=emotion)
    manager._calc_time_score = MagicMock(return_value=time)


@pytest.mark.asyncio
async def test_trace_admits_resolved_before_ranking_penalty(test_config):
    manager = BucketManager(test_config)
    _fixed_scores(manager)
    trace = {}

    results = await manager.search(
        "resolved query",
        candidate_buckets=[_bucket("resolved", resolved=True, importance=6)],
        trace=trace,
    )

    entry = trace["candidates"][0]
    assert results[0]["id"] == "resolved"
    assert entry["pre_penalty_score"] == 60.0
    assert entry["threshold"] == 50
    assert entry["admitted"] is True
    assert entry["ranking_penalty"] == 0.3
    assert entry["final_ranking_score"] == 18.0
    assert entry["final_ranking_score"] < entry["threshold"]


@pytest.mark.asyncio
async def test_trace_reports_semantic_hit_and_hybrid_state(test_config):
    embedding = MagicMock(enabled=True, last_error="")
    embedding.search_similar = AsyncMock(return_value=[("semantic", 0.91)])
    manager = BucketManager(test_config, embedding_engine=embedding)
    _fixed_scores(manager, topic=0.05, emotion=0.1, time=0.1)
    trace = {}

    results = await manager.search(
        "weak lexical query",
        candidate_buckets=[_bucket("semantic", importance=1)],
        trace=trace,
    )

    entry = trace["candidates"][0]
    assert results[0]["id"] == "semantic"
    assert trace["semantic"]["status"] == "available"
    assert entry["scores"]["semantic"] == 0.91
    assert entry["semantic_threshold"] is True
    assert entry["admitted"] is True


@pytest.mark.asyncio
async def test_trace_reports_exact_match_tier(test_config):
    manager = BucketManager(test_config)
    _fixed_scores(manager, topic=1.0, exact=1.0)
    trace = {}

    await manager.search(
        "exact query",
        candidate_buckets=[_bucket("exact")],
        trace=trace,
    )

    entry = trace["candidates"][0]
    assert entry["scores"]["exact_match"] == 1.0
    assert entry["match_tier"] == 3


@pytest.mark.asyncio
async def test_trace_distinguishes_disabled_and_provider_error(test_config):
    disabled = BucketManager(test_config)
    disabled_trace = {}
    await disabled.search(
        "query",
        candidate_buckets=[_bucket("disabled")],
        trace=disabled_trace,
    )
    assert disabled_trace["semantic"] == {"enabled": False, "status": "disabled"}

    failing_embedding = MagicMock(enabled=True, last_error="")

    async def fail_search(*args, **kwargs):
        failing_embedding.last_error = "embedding_provider_error"
        return []

    failing_embedding.search_similar = AsyncMock(side_effect=fail_search)
    failing = BucketManager(test_config, embedding_engine=failing_embedding)
    failing_trace = {}
    await failing.search(
        "query",
        candidate_buckets=[_bucket("provider-error")],
        trace=failing_trace,
    )
    assert failing_trace["semantic"]["status"] == "provider_error"
    assert failing_trace["semantic"]["error_code"] == "embedding_provider_error"


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "test-seal-filters")
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server._require_auth = lambda request: None
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None, **kwargs: content[:120]
    )
    return server


def _debug_client(server):
    app = Starlette(routes=[
        Route("/api/breath-debug", server.api_breath_debug, methods=["GET"]),
    ])
    return TestClient(app)


@pytest.mark.asyncio
async def test_debug_excludes_sealed_and_dormant_metadata(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    sealed_id = await server.bucket_mgr.create(
        content="sealed-debug-sentinel",
        name="Sealed Debug Sentinel",
        domain=["sealed-domain"],
    )
    dormant_id = await server.bucket_mgr.create(
        content="dormant-debug-sentinel",
        name="Dormant Debug Sentinel",
    )
    await server.trace(sealed_id, sealed=1)
    await server.trace(dormant_id, dormant=1)

    response = _debug_client(server).get(
        "/api/breath-debug",
        params={"q": "debug-sentinel"},
    )
    payload_text = response.text

    assert response.status_code == 200
    assert "sealed-debug-sentinel" not in payload_text
    assert "Sealed Debug Sentinel" not in payload_text
    assert "sealed-domain" not in payload_text
    assert "dormant-debug-sentinel" not in payload_text
    assert "Dormant Debug Sentinel" not in payload_text


@pytest.mark.asyncio
async def test_debug_reuses_structured_tag_filter_and_preserves_response_fields(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    wanted_id = await server.bucket_mgr.create(
        content="wanted diagnostic memory",
        tags=["wanted"],
    )
    excluded_id = await server.bucket_mgr.create(
        content="excluded diagnostic memory",
        tags=["other"],
    )

    response = _debug_client(server).get(
        "/api/breath-debug",
        params={"q": "diagnostic memory", "tags": "wanted"},
    )
    payload = response.json()
    result_ids = {item["id"] for item in payload["results"]}

    assert response.status_code == 200
    assert payload["equivalence"] == "runtime_query_trace"
    assert payload["candidate_source"] == "structured_filtered_active_buckets"
    assert wanted_id in result_ids
    assert excluded_id not in result_ids
    assert {"weights", "threshold", "results", "passed_count"} <= payload.keys()


@pytest.mark.asyncio
async def test_debug_does_not_touch_memory_state(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        content="side effect diagnostic memory",
        name="Side Effect Diagnostic",
    )
    before = await server.bucket_mgr.get(bucket_id)
    server.bucket_mgr.touch = AsyncMock(side_effect=AssertionError("debug touched memory"))

    response = _debug_client(server).get(
        "/api/breath-debug",
        params={"q": "side effect diagnostic"},
    )
    after = await server.bucket_mgr.get(bucket_id)

    assert response.status_code == 200
    assert server.bucket_mgr.touch.await_count == 0
    assert before["metadata"] == after["metadata"]


@pytest.mark.asyncio
async def test_normal_query_breath_touches_selected_bucket_once(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        content="normal query activation diagnostic memory",
        name="Normal Query Activation",
    )
    before = await server.bucket_mgr.get(bucket_id)
    real_touch = server.bucket_mgr.touch
    server.bucket_mgr.touch = AsyncMock(side_effect=real_touch)

    response = await server.breath(
        query="normal query activation diagnostic",
        max_results=1,
    )
    after = await server.bucket_mgr.get(bucket_id)

    assert bucket_id in response
    assert server.bucket_mgr.touch.await_count == 1
    assert server.bucket_mgr.touch.await_args.args[0] == bucket_id
    assert after["metadata"]["activation_count"] == (
        before["metadata"]["activation_count"] + 1
    )


@pytest.mark.asyncio
async def test_debug_explains_token_budget_omission(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        content="budget diagnostic memory",
        name="Budget Diagnostic",
    )
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None, **kwargs: "x" * 500
    )

    response = _debug_client(server).get(
        "/api/breath-debug",
        params={"q": "budget diagnostic", "max_tokens": "1"},
    )
    payload = response.json()
    entry = next(item for item in payload["results"] if item["id"] == bucket_id)

    assert response.status_code == 200
    assert payload["final_composition"]["surfaced_count"] == 0
    assert entry["final_decision"] == "omitted_token_budget"


def test_dashboard_renders_runtime_trace_fields():
    dashboard = open("dashboard.html", encoding="utf-8").read()
    assert "runtime_query_trace" in dashboard or "Runtime Breath trace" in dashboard
    assert "final_decision" in dashboard
    assert "semantic.status" in dashboard
