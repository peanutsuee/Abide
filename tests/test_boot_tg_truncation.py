import importlib
import sys
from datetime import datetime
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "tg-truncation-seal")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None: content[:120]
    )
    return server


@pytest.mark.asyncio
async def test_tg_pinned_preview_marks_only_content_that_is_truncated(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    short_id = await server.bucket_mgr.create(
        "short TG pinned body",
        name="short TG pinned",
        pinned=True,
    )
    long_tail = "TG_PINNED_HIDDEN_TAIL"
    long_body = ("L" * 600) + long_tail
    long_id = await server.bucket_mgr.create(
        long_body,
        name="long TG pinned",
        pinned=True,
    )

    result = await server.boot(profile="tg")

    assert f"…已截断：bucket {short_id}" not in result
    assert f"…已截断：bucket {long_id}" in result
    assert f"显示 600 / {len(long_body)} 字符" in result
    assert f'dream(detail_ids="{long_id}")' in result
    assert long_tail not in result


@pytest.mark.asyncio
async def test_tg_global_budget_reports_omitted_pinned_bucket_ids(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    bucket_ids = []
    for index in range(5):
        bucket_ids.append(
            await server.bucket_mgr.create(
                ("长" * 600) + f"TG_GLOBAL_TAIL_{index}",
                name=f"TG global pinned {index}",
                pinned=True,
            )
        )

    result = await server.boot(profile="tg")

    assert "已按 boot 预算截断" in result
    assert "钉选索引（原 5 项，完整输出" in result
    assert "省略/截断：bucket_id:" in result
    budget_notice = result.split("已按 boot 预算截断：", 1)[1]
    assert any(f"bucket_id:{bucket_id}" in budget_notice for bucket_id in bucket_ids)
    assert server.count_tokens_approx(result) <= server.BOOT_PROFILE_CONFIG["tg"]["max_tokens"]


@pytest.mark.asyncio
async def test_tg_without_global_budget_pressure_has_no_omission_notice(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    await server.bucket_mgr.create("short pinned", name="short pinned", pinned=True)

    result = await server.boot(profile="tg")

    assert "已按 boot 预算截断" not in result
    assert "省略/截断：" not in result


@pytest.mark.asyncio
async def test_tg_mailbox_budget_truncation_identifies_letter_and_continuation(
    tmp_path, monkeypatch
):
    server = _load_server(tmp_path, monkeypatch)
    hidden_tail = "TG_LETTER_HIDDEN_TAIL"
    await server.archive_session(
        "long letter carrier",
        letter=("TG_LONG_LETTER_TOKEN " * 5000) + hidden_tail,
    )

    result = await server.boot(profile="tg")

    assert "=== boot: 最新信箱 ===" in result
    assert "已按 boot 预算截断" in result
    assert "letter_id:1" in result
    assert "完整内容请用 get_letter(letter_id=1) 读取" in result
    assert hidden_tail not in result
    assert server.count_tokens_approx(result) <= server.BOOT_PROFILE_CONFIG["tg"]["max_tokens"]


@pytest.mark.asyncio
async def test_tg_delta_omission_lists_bucket_ids(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    await server.boot(profile="tg")
    for index in range(20):
        await server.bucket_mgr.create(
            f"delta body {index}",
            name=f"TG delta {index} " + ("x" * 70),
            importance=9,
        )

    result = await server.boot(profile="tg")

    assert "项未展开：bucket_id:" in result
    assert "=== boot: 增量摘要 ===" in result
    assert server.count_tokens_approx(result) <= server.BOOT_PROFILE_CONFIG["tg"]["max_tokens"]


@pytest.mark.asyncio
async def test_tg_delta_marks_an_oversized_omission_manifest(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    await server.boot(profile="tg")
    for index in range(80):
        await server.bucket_mgr.create(
            f"oversized delta body {index}",
            name=f"TG oversized delta {index}",
            importance=9,
        )

    result = await server.boot(profile="tg")

    assert "ID 清单超出 TG delta 预算，仅列出前缀" in result
    assert server.count_tokens_approx(result) <= server.BOOT_PROFILE_CONFIG["tg"]["max_tokens"]


@pytest.mark.asyncio
async def test_tg_trigger_preview_marks_truncated_body(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    body = ("触" * 300) + "TG_TRIGGER_HIDDEN_TAIL"
    bucket_id = await server.bucket_mgr.create(
        body,
        name="TG trigger preview",
        importance=8,
    )
    assert await server.bucket_mgr.update(
        bucket_id,
        trigger_date=datetime.now().date().isoformat(),
    )

    result = await server.boot(profile="tg")

    assert f"…已截断：bucket {bucket_id}" in result
    assert f"显示 300 / {len(body)} 字符" in result
    assert f'dream(detail_ids="{bucket_id}")' in result
    assert "TG_TRIGGER_HIDDEN_TAIL" not in result


@pytest.mark.asyncio
async def test_talk_and_code_keep_existing_preview_behavior(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    tail = "PROFILE_PREVIEW_HIDDEN_TAIL"
    bucket_id = await server.bucket_mgr.create(
        ("P" * 2500) + tail,
        name="profile preview control",
        pinned=True,
        domain=["项目/OB"],
    )

    talk = await server.boot(profile="talk")
    code = await server.boot(profile="code")

    assert tail in talk
    assert tail not in code
    assert f"…已截断：bucket {bucket_id}" not in talk
    assert f"…已截断：bucket {bucket_id}" not in code
