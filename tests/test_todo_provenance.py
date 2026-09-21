import importlib
import sys
from unittest.mock import AsyncMock

import pytest

from bucket_manager import merge_todo_provenance, reconcile_todo_provenance


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    return server


def _record(text, said_by="ting", said_at=None, source_bucket=None):
    return {
        "text": text,
        "said_by": said_by,
        "said_at": said_at,
        "source_bucket": source_bucket,
    }


def test_sidecar_reconciliation_keeps_legacy_todos_readable():
    assert reconcile_todo_provenance(["keep"], {"bad": "shape"}) == []
    assert reconcile_todo_provenance("- keep\n- next", None) == []
    assert reconcile_todo_provenance({"old": "task"}, None) == []
    with pytest.raises(ValueError, match="said_by"):
        reconcile_todo_provenance(
            ["keep"], [_record("keep", said_by="someone")], strict=True
        )
    with pytest.raises(ValueError, match="ISO-8601"):
        reconcile_todo_provenance(
            ["keep"], [_record("keep", said_at="not-a-date")], strict=True
        )


@pytest.mark.asyncio
async def test_trace_structured_todo_items_and_legacy_reconciliation(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(content="todo provenance")
    result = await server.trace(
        bucket_id,
        todo_items=[
            _record("ting task", "ting", "2026-09-17T12:00:00+08:00", "external-id"),
            _record("model task", "model"),
            _record("system task", "system"),
            {"text": "unknown task"},
        ],
    )
    assert result.startswith("已修改")
    bucket = await server.bucket_mgr.get(bucket_id)
    assert bucket["metadata"]["todos"] == [
        "ting task", "model task", "system task", "unknown task"
    ]
    assert bucket["metadata"]["todo_provenance"][0] == _record(
        "ting task", "ting", "2026-09-17T12:00:00+08:00", "external-id"
    )

    await server.trace(bucket_id, todos=["ting task", "legacy new"])
    bucket = await server.bucket_mgr.get(bucket_id)
    assert bucket["metadata"]["todos"] == ["ting task", "legacy new"]
    assert bucket["metadata"]["todo_provenance"] == [_record(
        "ting task", "ting", "2026-09-17T12:00:00+08:00", "external-id"
    )]

    await server.trace(bucket_id, todos=["renamed task"])
    bucket = await server.bucket_mgr.get(bucket_id)
    assert bucket["metadata"]["todos"] == ["renamed task"]
    assert "todo_provenance" not in bucket["metadata"]


@pytest.mark.asyncio
async def test_trace_structured_todo_items_rejects_invalid_or_ambiguous_input(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(content="todo validation")
    assert "不能同时" in await server.trace(
        bucket_id, todos=["legacy"], todo_items=[_record("structured")]
    )
    assert "非空字符串" in await server.trace(bucket_id, todo_items=[{"text": " "}])
    assert "said_by" in await server.trace(
        bucket_id, todo_items=[_record("bad", said_by="cheng")]
    )
    assert "ISO-8601" in await server.trace(
        bucket_id, todo_items=[_record("bad", said_at="tomorrow")]
    )


@pytest.mark.asyncio
async def test_automatic_extraction_defaults_to_unknown_without_sidecar(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    server._detect_conflict_warning = AsyncMock(return_value="")
    server.dehydrator.analyze = AsyncMock(return_value={
        "domain": ["事务"], "valence": 0.5, "arousal": 0.5,
        "tags": [], "suggested_name": "auto", "todos": ["extracted task"],
    })
    await server.hold("text containing an explicit unfinished task")
    bucket = next(
        bucket for bucket in await server.bucket_mgr.list_all()
        if "extracted task" in bucket["metadata"].get("todos", [])
    )
    assert "todo_provenance" not in bucket["metadata"]

    server.dehydrator.analyze = AsyncMock(return_value={
        "domain": ["事务"], "valence": 0.5, "arousal": 0.5,
        "tags": [], "suggested_name": "grow", "todos": ["short task"],
    })
    await server.grow("short grow input")
    server.dehydrator.digest = AsyncMock(return_value=[{
        "name": "long", "content": "long grow content with an unfinished task",
        "domain": ["事务"], "valence": 0.5, "arousal": 0.5,
        "tags": [], "importance": 5, "todos": ["long task"],
    }])
    await server.grow("This is a sufficiently long grow input for digest routing.")

    from import_memory import ImportEngine
    engine = ImportEngine(server.config, server.bucket_mgr, server.dehydrator)
    engine._extract_memories = AsyncMock(return_value=[{
        "name": "import", "content": "imported task context", "domain": ["事务"],
        "valence": 0.5, "arousal": 0.5, "tags": [], "importance": 5,
        "todos": ["import task"], "preserve_raw": False,
    }])
    assert (await engine.start("import source", filename="source.txt"))["status"] == "completed"
    for task in ("short task", "long task", "import task"):
        bucket = next(
            item for item in await server.bucket_mgr.list_all()
            if task in item["metadata"].get("todos", [])
        )
        assert "todo_provenance" not in bucket["metadata"]


@pytest.mark.asyncio
async def test_content_resolve_reopen_and_provenance_only_delta(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        content="before", todos=["keep"], todo_provenance=[_record("keep")]
    )
    high_water = server.bucket_mgr.get_boot_delta_high_water()
    await server.bucket_mgr.update(
        bucket_id, todo_provenance=[_record("keep", "model")]
    )
    assert server.bucket_mgr.get_boot_delta_high_water() == high_water
    await server.trace(bucket_id, content="after", resolved=1)
    bucket = await server.bucket_mgr.get(bucket_id)
    assert bucket["metadata"]["todo_provenance"] == [_record("keep", "model")]
    assert "keep" not in await server.todos()
    await server.trace(bucket_id, resolved=0)
    assert "keep" in await server.todos()


def test_merge_conflict_rules_are_conservative():
    target = [_record("same", "ting")]
    source = [_record("same", "model")]
    todos, records = merge_todo_provenance(["same"], target, ["same"], source)
    assert todos == ["same"]
    assert records == [_record("same", "unknown")]

    _, records = merge_todo_provenance(
        ["same"], [_record("same", "ting")], ["same"], [_record("same", "unknown")]
    )
    assert records == [_record("same", "ting")]

    _, records = merge_todo_provenance(
        ["same"], [_record("same", "ting")], ["same"], [_record("same", "ting")]
    )
    assert records == [_record("same", "ting")]


@pytest.mark.asyncio
async def test_merge_supersession_and_delete_preserve_or_remove_sidecar(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    target_id = await server.bucket_mgr.create(
        content="target", todos=["target task"], todo_provenance=[_record("target task", "ting")]
    )
    source_id = await server.bucket_mgr.create(
        content="source", todos=["source task"], todo_provenance=[_record("source task", "system")]
    )
    await server.trace(target_id, merge=source_id)
    merged = await server.bucket_mgr.get(target_id)
    assert merged["metadata"]["todo_provenance"] == [
        _record("target task", "ting"), _record("source task", "system")
    ]
    await server.trace(target_id, superseded_by="none")
    assert (await server.bucket_mgr.get(target_id))["metadata"]["todo_provenance"] == merged["metadata"]["todo_provenance"]
    assert await server.bucket_mgr.delete(target_id) is True
    assert await server.bucket_mgr.get(target_id) is None


@pytest.mark.asyncio
async def test_delete_never_rewrites_another_bucket_opaque_source_reference(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    source_id = await server.bucket_mgr.create(content="source")
    holder_id = await server.bucket_mgr.create(
        content="holder",
        todos=["copied"],
        todo_provenance=[_record("copied", "ting", source_bucket=source_id)],
    )
    assert await server.bucket_mgr.delete(source_id) is True
    holder = await server.bucket_mgr.get(holder_id)
    assert holder["metadata"]["todo_provenance"] == [
        _record("copied", "ting", source_bucket=source_id)
    ]


@pytest.mark.asyncio
async def test_todos_provenance_output_is_opt_in_and_sealed_safe(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    sealed_id = await server.bucket_mgr.create(content="sealed source", name="DO NOT LEAK", sealed=True)
    bucket_id = await server.bucket_mgr.create(content="todos")
    await server.trace(
        bucket_id,
        todo_items=[
            _record("ting task", "ting", "2026-09-17T12:00:00+08:00", sealed_id),
            _record("model task", "model"),
            _record("system task", "system"),
            _record("unknown task", "unknown"),
        ],
    )
    ordinary = await server.todos()
    detailed = await server.todos(include_provenance=True)
    assert "=== 婷明确说的 ===" not in ordinary
    assert "ting task" in ordinary
    for heading in ("=== 婷明确说的 ===", "=== 模型自己列的 ===", "=== 系统 ===", "=== 出处未知 ==="):
        assert heading in detailed
    assert "said_at:2026-09-17T12:00:00+08:00" in detailed
    assert sealed_id not in detailed
    assert "DO NOT LEAK" not in detailed

    server.dehydrator.dehydrate = AsyncMock(return_value="summary")
    boot = await server.boot()
    assert "=== 婷明确说的 ===" not in boot
    bucket = await server.bucket_mgr.get(bucket_id)
    server.bucket_mgr.search = AsyncMock(return_value=[bucket])
    breath = await server.breath(query="todos", touch=False)
    assert "=== 婷明确说的 ===" not in breath


@pytest.mark.asyncio
async def test_trace_and_todos_mcp_schemas_expose_only_opt_in_additions(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server.mcp) as client:
        schemas = {tool.name: tool.inputSchema for tool in (await client.list_tools()).tools}
    assert "todo_items" in schemas["trace"]["properties"]
    assert "include_provenance" in schemas["todos"]["properties"]
    assert schemas["todos"]["properties"]["include_provenance"]["default"] is False
