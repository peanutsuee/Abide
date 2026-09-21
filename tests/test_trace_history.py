import importlib
import sys
from unittest.mock import AsyncMock, Mock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    return server


def _confirm_token(result: str) -> str:
    for line in result.splitlines():
        if line.startswith("confirm_token:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("confirm_token not found")


@pytest.mark.asyncio
async def test_trace_append_preserves_content_and_records_history(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        content="original body",
        name="Append history control",
    )

    result = await server.trace(bucket_id, content="appended body", append=True)
    bucket = await server.bucket_mgr.get(bucket_id)
    history = server.bucket_mgr.get_history(bucket_id)

    assert "content=已追加" in result
    assert bucket["content"] == "original body\n\nappended body"
    assert history[0]["change_type"] == "append"
    assert history[0]["old_content"] == "original body"


@pytest.mark.asyncio
async def test_trace_replace_and_delete_record_history(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        content="before replace",
        name="Replace history control",
    )

    await server.trace(bucket_id, content="after replace")
    preview = await server.trace(bucket_id, delete=True)
    await server.trace(bucket_id, delete=True, confirm_token=_confirm_token(preview))
    history = server.bucket_mgr.get_history(bucket_id)

    assert [row["change_type"] for row in history[:2]] == ["delete", "replace"]
    assert history[0]["old_content"] == "after replace"
    assert history[1]["old_content"] == "before replace"


@pytest.mark.asyncio
async def test_protected_trace_content_targets_are_rejected(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    cases = (
        {"sealed": True},
        {"protected": True},
    )

    for index, options in enumerate(cases):
        bucket_id = await server.bucket_mgr.create(
            content=f"protected content {index}",
            **options,
        )

        replace = await server.trace(bucket_id, content="replacement")
        append = await server.trace(bucket_id, content="append", append=True)
        delete = await server.trace(bucket_id, delete=True)

        assert "受到保护" in replace
        assert "受到保护" in append
        assert "受到保护" in delete
        bucket = await server.bucket_mgr.get(bucket_id)
        assert bucket["content"] == f"protected content {index}"


@pytest.mark.asyncio
async def test_pinned_trace_replace_append_record_history_and_delete_stays_blocked(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        content="pinned original",
        pinned=True,
    )

    replaced = await server.trace(bucket_id, content="pinned replacement")
    appended = await server.trace(bucket_id, content="pinned append", append=True)
    deleted = await server.trace(bucket_id, delete=True)
    bucket = await server.bucket_mgr.get(bucket_id)
    history = server.bucket_mgr.get_history(bucket_id)

    assert "content=已替换" in replaced
    assert "content=已追加" in appended
    assert "受到保护" in deleted
    assert bucket["content"] == "pinned replacement\n\npinned append"
    assert [row["change_type"] for row in history[:2]] == ["append", "replace"]
    assert history[0]["old_content"] == "pinned replacement"
    assert history[1]["old_content"] == "pinned original"


@pytest.mark.asyncio
async def test_pinned_trace_unpin_requires_current_confirmation_token(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(content="pinned body", pinned=True)

    blocked = await server.trace(bucket_id, pinned=0)
    token = blocked.split("confirm_token:", 1)[1].split(".", 1)[0].strip()
    before = await server.bucket_mgr.get(bucket_id)

    assert "confirmation required" in blocked
    assert token == server._pinned_unpin_confirm_token(before)
    assert before["metadata"]["pinned"] is True

    wrong = await server.trace(bucket_id, pinned=0, confirm_token="wrong-token")
    assert "confirmation required" in wrong
    assert (await server.bucket_mgr.get(bucket_id))["metadata"]["pinned"] is True

    unpinned = await server.trace(bucket_id, pinned=0, confirm_token=token)
    assert "pinned=False" in unpinned
    assert (await server.bucket_mgr.get(bucket_id))["metadata"]["pinned"] is False


@pytest.mark.asyncio
async def test_unpin_non_pinned_bucket_does_not_require_confirmation(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(content="ordinary body")

    result = await server.trace(bucket_id, pinned=0)

    assert "confirmation required" not in result
    assert (await server.bucket_mgr.get(bucket_id))["metadata"]["pinned"] is False


@pytest.mark.asyncio
async def test_destructive_content_updates_fail_closed_when_history_capture_fails(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(content="original trace content")
    superseded_id = await server.bucket_mgr.create(content="original superseded content")
    server.bucket_mgr.record_history = Mock(side_effect=RuntimeError("history unavailable"))

    replaced = await server.trace(bucket_id, content="replacement")
    superseded = await server.hold("new superseded content", supersedes_id=superseded_id)

    assert replaced == f"修改失败: {bucket_id}"
    assert "supersedes update failed" in superseded
    assert (await server.bucket_mgr.get(bucket_id))["content"] == "original trace content"
    assert (await server.bucket_mgr.get(superseded_id))["content"] == "original superseded content"


@pytest.mark.asyncio
async def test_delete_history_capture_failure_does_not_delete_or_report_not_found(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(content="delete history failure")
    server.bucket_mgr.record_history = Mock(side_effect=RuntimeError("history unavailable"))

    preview = await server.trace(bucket_id, delete=True)
    result = await server.trace(bucket_id, delete=True, confirm_token=_confirm_token(preview))

    assert "删除失败" in result
    assert "未找到" not in result
    assert await server.bucket_mgr.get(bucket_id) is not None


@pytest.mark.asyncio
async def test_delete_execution_failure_is_not_reported_as_not_found(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(content="delete execution failure")
    server.bucket_mgr.delete = AsyncMock(return_value=False)

    preview = await server.trace(bucket_id, delete=True)
    result = await server.trace(bucket_id, delete=True, confirm_token=_confirm_token(preview))

    assert "删除失败" in result
    assert "未找到" not in result


@pytest.mark.asyncio
async def test_batch_trace_returns_identifiable_result_for_each_bucket(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    first_id = await server.bucket_mgr.create(content="batch first")
    second_id = await server.bucket_mgr.create(content="batch second")

    result = await server.trace(f"{first_id},{second_id}", resolved=1)

    assert f"[{first_id}]" in result
    assert f"[{second_id}]" in result
    assert (await server.bucket_mgr.get(first_id))["metadata"]["resolved"] is True
    assert (await server.bucket_mgr.get(second_id))["metadata"]["resolved"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("protection", ["pinned", "protected"])
async def test_trace_importance_change_reports_pinned_or_protected_lock(
    tmp_path, monkeypatch, protection
):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        content=f"{protection} importance lock",
        **{protection: True},
    )

    result = await server.trace(bucket_id, importance=3)

    assert "importance 未修改" in result
    assert "pinned/protected" in result
    assert (await server.bucket_mgr.get(bucket_id))["metadata"]["importance"] == 10


@pytest.mark.asyncio
async def test_archived_session_trace_allows_unsealed_body_but_not_sealed_body(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    open_result = await server.archive_session("open archive session")
    open_id = open_result.split("bucket_id:", 1)[1].strip()
    sealed_result = await server.archive_session("sealed archive session", sealed=True)
    sealed_id = sealed_result.split("bucket_id:", 1)[1].strip()
    sealed_before = await server.bucket_mgr.get(sealed_id)

    updated = await server.trace(open_id, content="updated archived body")
    blocked = await server.trace(sealed_id, content="must not replace")

    assert "content=已替换" in updated
    assert (await server.bucket_mgr.get(open_id))["content"] == "updated archived body"
    assert "受到保护" in blocked
    assert (await server.bucket_mgr.get(sealed_id))["content"] == sealed_before["content"]
