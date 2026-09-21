import importlib
import sqlite3
import sys
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "profile-seal")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: content[:120]
    )
    return server


@pytest.mark.asyncio
async def test_default_boot_is_talk_and_all_profiles_identify_themselves(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)

    default = await server.boot()
    code = await server.boot(profile="code")
    tg = await server.boot(profile="tg")

    assert default.startswith("boot profile: talk\n")
    assert code.startswith("boot profile: code\n")
    assert tg.startswith("boot profile: tg\n")
    for result in (default, code, tg):
        assert "=== boot: 婷留言 ===" in result
        assert "=== boot: 增量摘要 ===" in result
        assert "=== boot: 今日浮现 ===" in result


@pytest.mark.asyncio
async def test_invalid_profile_is_rejected_without_starting_decay(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    result = await server.boot(profile="daily")

    assert result == "profile 必须是 talk、code 或 tg。"
    assert server.decay_engine.ensure_started.await_count == 0


@pytest.mark.asyncio
async def test_code_uses_structured_metadata_for_session_selection(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    code_id = await server.bucket_mgr.create(
        "ordinary archive summary",
        name="code session",
        domain=["session"],
        topics=["项目/OB"],
    )
    personal_id = await server.bucket_mgr.create(
        "项目 words in body must not classify this archive",
        name="personal session",
        domain=["session"],
        topics=["日常/作息"],
    )
    await server.bucket_mgr.archive(code_id)
    await server.bucket_mgr.archive(personal_id)

    code = await server.boot(profile="code")
    talk = await server.boot(profile="talk")

    assert code_id in code
    assert personal_id not in code
    assert code_id in talk and personal_id in talk


@pytest.mark.asyncio
async def test_profile_delta_checkpoints_do_not_consume_each_other(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    await server.boot(profile="talk")
    bucket_id = await server.bucket_mgr.create(
        "profile delta body",
        name="profile delta",
        domain=["项目/OB"],
        pinned=True,
    )

    talk = await server.boot(profile="talk")
    code = await server.boot(profile="code")
    tg = await server.boot(profile="tg")

    locator = f"[bucket_id:{bucket_id}] profile delta：新建"
    assert locator in talk
    assert locator in code
    assert locator in tg
    with sqlite3.connect(server.bucket_mgr.history_db_path) as conn:
        rows = conn.execute(
            "SELECT profile, last_event_id FROM boot_delta_profile_checkpoints"
        ).fetchall()
    assert {row[0] for row in rows} == {"talk", "code", "tg"}


def test_legacy_global_delta_checkpoint_seeds_all_profiles(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    with sqlite3.connect(server.bucket_mgr.history_db_path) as conn:
        conn.execute("DELETE FROM boot_delta_profile_checkpoints")
        conn.execute("DELETE FROM boot_delta_checkpoint")
        conn.execute(
            """
            INSERT INTO boot_delta_checkpoint (singleton, last_event_id, completed_at)
            VALUES (1, 17, '2026-09-17T00:00:00')
            """
        )

    server.bucket_mgr._init_history_db()

    assert all(
        server.bucket_mgr.get_boot_delta_checkpoint(profile)["last_event_id"] == 17
        for profile in ("talk", "code", "tg")
    )


@pytest.mark.asyncio
async def test_note_delivery_and_sealed_hiding_remain_global_across_profiles(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    await server.leave_note("single cross-profile note")
    sealed_id = await server.bucket_mgr.create(
        "sealed profile body", name="sealed profile", sealed=True, pinned=True
    )

    talk = await server.boot(profile="talk")
    code = await server.boot(profile="code")
    tg = await server.boot(profile="tg")

    assert "single cross-profile note" in talk
    assert "single cross-profile note" not in code
    assert "single cross-profile note" not in tg
    for result in (talk, code, tg):
        assert sealed_id not in result
        assert "sealed profile" not in result


@pytest.mark.asyncio
async def test_tg_is_compact_and_keeps_global_constraint_and_high_todo(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    constraint_id = await server.bucket_mgr.create(
        "global constraint body " * 300,
        name="protected operation rule",
        protected=True,
        domain=["日常/作息"],
    )
    high_todo_id = await server.bucket_mgr.create(
        "high todo carrier",
        name="high todo",
        importance=8,
        todos=["must ship"],
    )
    low_todo_id = await server.bucket_mgr.create(
        "low todo carrier",
        name="low todo",
        importance=3,
        todos=["later"],
    )

    talk = await server.boot(profile="talk")
    tg = await server.boot(profile="tg")

    assert constraint_id in tg
    assert high_todo_id in tg
    assert low_todo_id not in tg
    assert "=== boot: 最新信箱 ===" in tg
    assert "=== boot: 最近 3 次归档 ===" not in tg
    assert "=== boot: 回声 ===" not in tg
    assert len(tg) < len(talk)
    assert server.count_tokens_approx(tg) <= server.BOOT_PROFILE_CONFIG["tg"]["max_tokens"]


@pytest.mark.asyncio
async def test_tg_mailbox_shows_only_latest_visible_letter_without_sessions(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    await server.archive_session("older session", letter="TG_OLDER_VISIBLE_LETTER")
    await server.archive_session("latest session", letter="TG_LATEST_VISIBLE_LETTER")
    await server.archive_session(
        "sealed session", letter="TG_SEALED_LETTER_MUST_NOT_LEAK", sealed=True
    )
    await server.leave_note("TG_FUTURE_NOTE_MUST_NOT_LEAK", open_at="2099-01-01T00:00:00")

    talk = await server.boot(profile="talk")
    code = await server.boot(profile="code")
    tg = await server.boot(profile="tg")

    for result in (talk, code, tg):
        assert "TG_LATEST_VISIBLE_LETTER" in result
        assert "TG_OLDER_VISIBLE_LETTER" not in result
        assert "TG_SEALED_LETTER_MUST_NOT_LEAK" not in result
    assert "TG_FUTURE_NOTE_MUST_NOT_LEAK" not in tg
    assert "=== boot: 最新信箱 ===" in tg
    assert "=== boot: 最近 3 次归档 ===" not in tg
    assert "older session" not in tg
    assert "latest session" not in tg


@pytest.mark.asyncio
async def test_profile_boot_does_not_touch_bucket_files(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_id = await server.bucket_mgr.create(
        "profile read-only", name="profile read-only", pinned=True
    )
    path = server.bucket_mgr._find_bucket_file(bucket_id)
    before = open(path, "rb").read()

    await server.boot(profile="code")
    await server.boot(profile="tg")

    assert open(path, "rb").read() == before
