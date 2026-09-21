import importlib
import json
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import frontmatter
import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    return server


def _age_bucket(bucket_mgr, bucket_id, days=40, **overrides):
    path = bucket_mgr._find_bucket_file(bucket_id)
    post = frontmatter.load(path)
    old = datetime.now() - timedelta(days=days)
    post["created"] = old.isoformat()
    post["last_active"] = old.isoformat()
    post["created_at"] = old.date().isoformat()
    post["updated_at"] = old.date().isoformat()
    for key, value in overrides.items():
        if value is None:
            post.pop(key, None)
        else:
            post[key] = value
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(post))


@pytest.mark.asyncio
async def test_bucket_manager_todos_are_canonical_and_tri_state(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="todo authority",
        todos=["  keep  ", "", "keep", "new"],
    )

    bucket = await bucket_mgr.get(bucket_id)
    assert bucket["metadata"]["todos"] == ["keep", "new"]

    await bucket_mgr.update(bucket_id, content="body changed")
    assert (await bucket_mgr.get(bucket_id))["metadata"]["todos"] == ["keep", "new"]

    await bucket_mgr.update(bucket_id, todos=[])
    assert (await bucket_mgr.get(bucket_id))["metadata"]["todos"] == []


def test_todo_parsers_treat_missing_or_malformed_llm_fields_as_empty(test_config):
    from dehydrator import Dehydrator
    from import_memory import ImportEngine

    dehydrator = Dehydrator(test_config)
    analysis = dehydrator._parse_analysis(
        json.dumps({"domain": ["事务"], "todos": "not-a-list"}, ensure_ascii=False)
    )
    digest = dehydrator._parse_digest(
        json.dumps([{"content": "digest body", "todos": {"bad": "shape"}}])
    )
    extraction = ImportEngine._parse_extraction(
        json.dumps([{"content": "import body", "todos": "not-a-list"}])
    )

    assert analysis["todos"] == []
    assert digest[0]["todos"] == []
    assert extraction[0]["todos"] == []


@pytest.mark.asyncio
async def test_real_hold_grow_import_todos_reach_todos_and_boot(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._detect_conflict_warning = AsyncMock(return_value="")
    server.dehydrator.analyze = AsyncMock(return_value={
        "domain": ["事务"],
        "valence": 0.5,
        "arousal": 0.3,
        "tags": [],
        "suggested_name": "hold todo",
        "todos": ["hold task"],
    })

    await server.hold("hold content with explicit task")
    assert "hold task" in await server.todos()

    server.dehydrator.analyze = AsyncMock(return_value={
        "domain": ["事务"],
        "valence": 0.5,
        "arousal": 0.3,
        "tags": [],
        "suggested_name": "grow todo",
        "todos": ["grow task"],
    })
    await server.grow("short grow input")

    server.dehydrator.digest = AsyncMock(return_value=[{
        "name": "digest todo",
        "content": "A long diary entry that contains an explicit unfinished task.",
        "domain": ["事务"],
        "valence": 0.5,
        "arousal": 0.3,
        "tags": [],
        "importance": 5,
        "todos": ["digest task"],
    }])
    await server.grow("This is a sufficiently long diary entry for digest processing.")

    from import_memory import ImportEngine

    engine = ImportEngine(server.config, server.bucket_mgr, server.dehydrator)
    engine._extract_memories = AsyncMock(return_value=[{
        "name": "import todo",
        "content": "Imported memory with an explicit unfinished task.",
        "domain": ["事务"],
        "valence": 0.5,
        "arousal": 0.3,
        "tags": [],
        "importance": 5,
        "todos": ["import task"],
        "preserve_raw": False,
    }])
    result = await engine.start("import source text", filename="import.txt")

    todo_output = await server.todos()
    assert result["status"] == "completed"
    for item in ("hold task", "grow task", "digest task", "import task"):
        assert item in todo_output

    server.dehydrator.dehydrate = AsyncMock(side_effect=lambda content, metadata=None: content)
    boot_output = await server.boot()
    assert "hold task" in boot_output


@pytest.mark.asyncio
async def test_todos_tri_state_resolved_filter_and_merge_preserve(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    target_id = await server.bucket_mgr.create(
        content="target todo content",
        todos=["old target task"],
    )
    await server.trace(target_id)
    assert "old target task" in (await server.bucket_mgr.get(target_id))["metadata"]["todos"]

    await server.trace(target_id, todos="")
    assert (await server.bucket_mgr.get(target_id))["metadata"]["todos"] == []
    await server.trace(target_id, todos="new target task,another target task")

    source_id = await server.bucket_mgr.create(
        content="source todo content",
        todos=["source task", "new target task"],
    )
    await server.trace(target_id, merge=source_id)
    merged = await server.bucket_mgr.get(target_id)
    assert merged["metadata"]["todos"] == [
        "new target task",
        "another target task",
        "source task",
    ]
    assert await server.bucket_mgr.get(source_id) is None

    resolved_id = await server.bucket_mgr.create(
        content="resolved todo content",
        todos=["hidden resolved task"],
    )
    await server.bucket_mgr.update(resolved_id, resolved=True)
    todo_output = await server.todos()
    assert "hidden resolved task" not in todo_output


@pytest.mark.asyncio
async def test_trace_todos_accepts_string_and_list_with_tri_state_semantics(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        content="todo input symmetry",
        todos=["initial"],
    )

    await server.trace(bucket_id, todos="string task, another string task")
    assert (await server.bucket_mgr.get(bucket_id))["metadata"]["todos"] == [
        "string task",
        "another string task",
    ]

    await server.trace(bucket_id, todos=[" list task ", "", "list task", "second"])
    bucket = await server.bucket_mgr.get(bucket_id)
    assert bucket["metadata"]["todos"] == ["list task", "second"]
    assert isinstance(bucket["metadata"]["todos"], list)
    assert all(isinstance(item, str) for item in bucket["metadata"]["todos"])

    await server.trace(bucket_id, todos="")
    assert (await server.bucket_mgr.get(bucket_id))["metadata"]["todos"] == []

    await server.trace(bucket_id, todos=["keep before None"])
    before_none = await server.bucket_mgr.get(bucket_id)
    await server.trace(bucket_id, todos=None)
    after_none = await server.bucket_mgr.get(bucket_id)
    assert after_none["metadata"]["todos"] == before_none["metadata"]["todos"]
    assert isinstance(after_none["metadata"]["todos"], list)


@pytest.mark.asyncio
async def test_breath_uses_current_metadata_todos_after_cached_summary(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        content="stable body used by a cached breath summary",
        todos=["old one", "old two"],
    )
    await server.trace(
        bucket_id,
        todos=["new one", "new two", "new three", "new four"],
    )
    current = await server.bucket_mgr.get(bucket_id)
    server.bucket_mgr.search = AsyncMock(return_value=[current])
    server.bucket_mgr.touch = AsyncMock(return_value=None)
    server.dehydrator.dehydrate = AsyncMock(
        return_value="cached summary without authoritative todos"
    )

    result = await server.breath(query="find the stable body")

    assert "=== 当前 todos（以 metadata 为准）===" in result
    for item in ("new one", "new two", "new three", "new four"):
        assert f"- {item}" in result
    assert "- old one" not in result
    assert "- old two" not in result


@pytest.mark.asyncio
async def test_trace_mcp_schema_and_runtime_accept_string_or_array_todos(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(content="MCP todo input")
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server.mcp) as client:
        tools = (await client.list_tools()).tools
        schema = next(tool.inputSchema for tool in tools if tool.name == "trace")
        todos_schema = schema["properties"]["todos"]
        branches = todos_schema["anyOf"]
        array_schema = next(branch for branch in branches if branch.get("type") == "array")
        assert {branch.get("type") for branch in branches} == {"array", "string", "null"}
        assert array_schema == {"items": {"type": "string"}, "type": "array"}
        assert todos_schema["default"] is None

        result = await client.call_tool(
            "trace",
            {"bucket_id": bucket_id, "todos": ["runtime task", "", "runtime task"]},
        )

    assert result.isError is False
    assert (await server.bucket_mgr.get(bucket_id))["metadata"]["todos"] == [
        "runtime task"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_field", ["last_active", "updated_at"])
async def test_decay_invalid_time_metadata_fails_closed(
    bucket_mgr, invalid_field, tmp_path
):
    bucket_id = await bucket_mgr.create(
        content="must not be changed by invalid time",
        importance=1,
    )
    _age_bucket(bucket_mgr, bucket_id, **{invalid_field: "not-a-timestamp"})

    from decay_engine import DecayEngine

    engine = DecayEngine({"decay": {"threshold": 999}}, bucket_mgr)
    result = await engine.run_decay_cycle()
    bucket = await bucket_mgr.get(bucket_id)

    assert result["archived"] == 0
    assert result["auto_resolved"] == 0
    assert result["compressed"] == 0
    assert bucket["metadata"].get("resolved") is not True
    assert bucket["content"] == "must not be changed by invalid time"


@pytest.mark.asyncio
@pytest.mark.parametrize("protected_type", ["plan", "letter", "i", "anchor"])
async def test_decay_protected_types_are_not_mutated(bucket_mgr, protected_type):
    bucket_id = await bucket_mgr.create(
        content=f"protected {protected_type}",
        importance=1,
        bucket_type="dynamic",
    )
    if protected_type == "anchor":
        _age_bucket(bucket_mgr, bucket_id, anchor=True)
    else:
        path = bucket_mgr._find_bucket_file(bucket_id)
        post = frontmatter.load(path)
        post["type"] = protected_type
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(frontmatter.dumps(post))
        _age_bucket(bucket_mgr, bucket_id)

    from decay_engine import DecayEngine

    engine = DecayEngine({"decay": {"threshold": 999}}, bucket_mgr)
    await engine.run_decay_cycle()
    bucket = await bucket_mgr.get(bucket_id)

    assert bucket["content"] == f"protected {protected_type}"
    assert bucket["metadata"].get("resolved") is not True
    assert "archive" not in bucket["path"]


@pytest.mark.asyncio
async def test_decay_todo_and_legacy_missing_todos_behave_differently(bucket_mgr):
    todo_id = await bucket_mgr.create(
        content="active todo body",
        importance=1,
        todos=["keep this task"],
    )
    _age_bucket(bucket_mgr, todo_id)

    legacy_id = await bucket_mgr.create(
        content="legacy body eligible for decay",
        importance=1,
    )
    legacy_path = bucket_mgr._find_bucket_file(legacy_id)
    legacy_post = frontmatter.load(legacy_path)
    if "todos" in legacy_post:
        del legacy_post["todos"]
    with open(legacy_path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(legacy_post))
    _age_bucket(bucket_mgr, legacy_id)

    from decay_engine import DecayEngine

    engine = DecayEngine({"decay": {"threshold": -1}}, bucket_mgr)
    result = await engine.run_decay_cycle()
    todo_bucket = await bucket_mgr.get(todo_id)
    legacy_bucket = await bucket_mgr.get(legacy_id)

    assert todo_bucket["content"] == "active todo body"
    assert todo_bucket["metadata"].get("resolved") is not True
    assert result["compressed"] == 1
    assert result["auto_resolved"] == 1
    assert legacy_bucket["content"].endswith("...")
    assert legacy_bucket["metadata"]["resolved"] is True


@pytest.mark.asyncio
async def test_decay_compression_uses_existing_history_for_recovery(bucket_mgr):
    original = "original body that must be recoverable"
    bucket_id = await bucket_mgr.create(content=original, importance=1)
    _age_bucket(bucket_mgr, bucket_id)

    from decay_engine import DecayEngine

    engine = DecayEngine({"decay": {"threshold": -1}}, bucket_mgr)
    result = await engine.run_decay_cycle()
    history = bucket_mgr.get_history(bucket_id)

    assert result["compressed"] == 1
    assert history[0]["change_type"] == "decay_compression"
    assert history[0]["old_content"] == original
