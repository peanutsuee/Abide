import importlib
import sqlite3
import sys
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch, seal="boot-delta-seal"):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", seal)
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: content[:120]
    )
    return server


def _checkpoint(server):
    return server.bucket_mgr.get_boot_delta_checkpoint()


@pytest.mark.asyncio
async def test_first_boot_establishes_baseline_then_reports_new_bucket_once(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)

    first = await server.boot()
    baseline = _checkpoint(server)
    bucket_id = await server.bucket_mgr.create("delta creation body", name="delta creation")
    second = await server.boot()
    third = await server.boot()

    assert "（暂无上次 boot 基线）" in first
    assert baseline is not None
    assert f"[bucket_id:{bucket_id}] delta creation：新建" in second
    assert "（无新增变化）" in third
    assert _checkpoint(server)["last_event_id"] > baseline["last_event_id"]


@pytest.mark.asyncio
async def test_boot_delta_reports_content_todo_and_superseded_state_changes(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        "before body", name="delta state carrier", todos=["close me", "stay open"]
    )
    await server.boot()

    assert await server.bucket_mgr.update(
        bucket_id,
        content="after body",
        todos=["stay open"],
        superseded_by="none",
    )
    result = await server.boot()

    locator = f"[bucket_id:{bucket_id}] delta state carrier"
    assert f"{locator}：正文已修改" in result
    assert f"{locator}：todo 已关闭 1 项" in result
    assert f"{locator}：已标记 invalidated" in result
    assert "before body" not in result
    assert "after body" not in result


@pytest.mark.asyncio
async def test_boot_delta_hides_sealed_and_deleted_buckets(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    await server.boot()
    sealed_id = await server.bucket_mgr.create(
        "sealed delta body", name="sealed delta name", sealed=True
    )
    deleted_id = await server.bucket_mgr.create(
        "deleted delta body", name="deleted delta name"
    )
    assert await server.bucket_mgr.delete(deleted_id)

    result = await server.boot()

    assert sealed_id not in result
    assert "sealed delta name" not in result
    assert "sealed delta body" not in result
    assert deleted_id not in result
    assert "deleted delta name" not in result
    assert "（无新增变化）" in result


@pytest.mark.asyncio
async def test_boot_failure_does_not_advance_delta_checkpoint(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    await server.boot()
    before = _checkpoint(server)
    bucket_id = await server.bucket_mgr.create("failure carrier", name="failure carrier")
    original_fit = server._fit_sections_to_budget

    def fail_fit(*args, **kwargs):
        raise RuntimeError("synthetic boot failure")

    monkeypatch.setattr(server, "_fit_sections_to_budget", fail_fit)
    with pytest.raises(RuntimeError, match="synthetic boot failure"):
        await server.boot()
    assert _checkpoint(server) == before

    monkeypatch.setattr(server, "_fit_sections_to_budget", original_fit)
    recovered = await server.boot()
    assert f"[bucket_id:{bucket_id}] failure carrier：新建" in recovered


@pytest.mark.asyncio
async def test_boot_delta_is_read_only_for_bucket_activation_and_repeated_boots(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create("read-only delta", name="read-only delta")
    bucket_path = server.bucket_mgr._find_bucket_file(bucket_id)
    before = open(bucket_path, "rb").read()

    await server.boot()
    after_first = open(bucket_path, "rb").read()
    await server.boot()
    after_second = open(bucket_path, "rb").read()

    assert before == after_first == after_second


@pytest.mark.asyncio
async def test_boot_delta_budget_keeps_complete_records_and_names_omissions(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    await server.boot()
    for index in range(80):
        await server.bucket_mgr.create(
            f"delta budget body {index}",
            name=f"delta budget bucket {index:02d} " + "x" * 70,
        )

    visible = await server.bucket_mgr.list_all(include_archive=True)
    checkpoint = _checkpoint(server)
    delta = server._format_boot_delta(
        checkpoint=checkpoint,
        high_water=server.bucket_mgr.get_boot_delta_high_water(),
        visible_buckets=visible,
    )

    assert server.count_tokens_approx(delta) <= server.BOOT_DELTA_MAX_TOKENS
    assert "还有" in delta and "项未展开" in delta
    assert all("：" in line for line in delta.splitlines() if line.startswith("- "))


def test_boot_delta_schema_has_event_log_and_success_checkpoint(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    with sqlite3.connect(server.bucket_mgr.history_db_path) as conn:
        event_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(boot_delta_events)")
        }
        checkpoint_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(boot_delta_checkpoint)")
        }
        profile_checkpoint_columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(boot_delta_profile_checkpoints)"
            )
        }

    assert {"id", "bucket_id", "event_type", "payload_json", "occurred_at"} <= event_columns
    assert {"singleton", "last_event_id", "completed_at"} <= checkpoint_columns
    assert {"profile", "last_event_id", "completed_at"} <= profile_checkpoint_columns
