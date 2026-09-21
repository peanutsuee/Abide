import importlib
import sys
from unittest.mock import AsyncMock

import frontmatter
import pytest


def _load_server(tmp_path, monkeypatch, seal="test-seal-a"):
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


@pytest.mark.asyncio
async def test_archive_letter_boot_and_mailbox(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    pinned_id = await server.bucket_mgr.create(
        content="pinned startup principle body",
        name="Pinned startup",
        pinned=True,
    )
    todo_id = await server.bucket_mgr.create(
        content="todo carrier body",
        name="Todo carrier",
        importance=7,
    )
    todo_path = server.bucket_mgr._find_bucket_file(todo_id)
    post = frontmatter.load(todo_path)
    post["todos"] = ["finish boot validation"]
    with open(todo_path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(post))

    letter = "handoff letter exact body"
    await server.archive_session("session summary one", letter=letter)
    await server.archive_session("session summary two without letter")

    boot_result = await server.boot()
    mailbox_result = await server.breath(mailbox=True)

    assert pinned_id in boot_result
    assert "pinned startup principle body" in boot_result
    assert letter in boot_result
    assert "session summary two without letter" in boot_result
    assert "finish boot validation" in boot_result
    assert "seal: test-seal-a" in boot_result
    assert server.count_tokens_approx(boot_result) <= 16000
    section_headers = [
        "=== boot: 婷留言 ===",
        "=== boot: 增量摘要 ===",
        "=== boot: 今日浮现 ===",
        "=== boot: 最新信箱 ===",
        "=== boot: 未完结 todos ===",
        "=== boot: 最近 3 次归档 ===",
        "=== boot: 开机索引 ===",
        "=== boot: 回声 ===",
    ]
    assert [boot_result.index(header) for header in section_headers] == sorted(
        boot_result.index(header) for header in section_headers
    )

    assert letter in mailbox_result
    assert "seal: test-seal-a" in mailbox_result


@pytest.mark.asyncio
async def test_boot_pinned_index_defaults_to_5000_chars(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    long_pinned_body = (
        "开机桶正文开始\n"
        + ("A" * 2200)
        + "VISIBLE_AFTER_2000"
        + ("B" * 3000)
        + "HIDDEN_AFTER_5000"
    )

    await server.bucket_mgr.create(
        content=long_pinned_body,
        name="Startup index",
        pinned=True,
    )
    boot_result = await server.boot()

    assert "=== boot: 开机索引 ===" in boot_result
    assert "VISIBLE_AFTER_2000" in boot_result
    assert "HIDDEN_AFTER_5000" not in boot_result
    assert "seal: test-seal-a" in boot_result


@pytest.mark.asyncio
async def test_boot_defaults_and_clamps_max_tokens_to_16000(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    observed = {}
    original_fit = server._fit_sections_to_budget

    def capture_fit(sections, max_tokens, **kwargs):
        observed["max_tokens"] = max_tokens
        return original_fit(sections, max_tokens, **kwargs)

    monkeypatch.setattr(server, "_fit_sections_to_budget", capture_fit)

    assert server.boot.__defaults__ == (5000, 16000, "talk")
    await server.boot(max_tokens=99999)
    assert observed["max_tokens"] == 16000 - 40


def test_fit_sections_outputs_blocks_that_exactly_fit(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    sections = [
        ("first", "第一块", "first section"),
        ("second", "第二块", "second section"),
    ]
    exact_budget = sum(
        server.count_tokens_approx(text) for _, _, text in sections
    )

    result = server._fit_sections_to_budget(sections, exact_budget)

    assert result == "first section\n\nsecond section"
    assert "已按 boot 预算截断" not in result


def test_fit_sections_reports_partial_and_later_omitted_blocks(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch)
    result = server._fit_sections_to_budget(
        [
            ("first", "第一块", "short first block"),
            ("second", "第二块", "中" * 2000),
            ("third", "第三块", "third block must be omitted"),
        ],
        max_tokens=180,
    )

    assert "short first block" in result
    assert "- 部分截断：第二块" in result
    assert "- 未输出：第三块" in result


def test_fit_sections_reports_multiple_blocks_after_budget_exhaustion(
    tmp_path,
    monkeypatch,
):
    server = _load_server(tmp_path, monkeypatch)
    result = server._fit_sections_to_budget(
        [
            ("first", "第一块", "中" * 2000),
            ("second", "第二块", "second block"),
            ("third", "第三块", "third block"),
        ],
        max_tokens=180,
    )

    assert "- 部分截断：第一块" in result
    assert "- 未输出：第二块、第三块" in result
    assert server.count_tokens_approx(result) <= 180


@pytest.mark.parametrize(
    ("protected_key", "protected_name"),
    [
        ("mailbox", "最新 letter"),
        ("todos", "todos"),
        ("sessions", "最近归档"),
        ("pinned", "钉选索引"),
    ],
)
def test_fit_sections_reserves_each_configured_minimum(
    tmp_path,
    monkeypatch,
    protected_key,
    protected_name,
):
    server = _load_server(tmp_path, monkeypatch)
    minimum = server.BOOT_SECTION_MINIMUM_CHARS[protected_key]
    sections = [
        ("triggers", "今日触发", "触" * 20),
        (protected_key, protected_name, "保" * (minimum + 500)),
        ("echo", "feel 回声", "回" * 2000),
    ]
    result = server._fit_sections_to_budget(
        sections,
        max_tokens=(
            server.BOOT_TRUNCATION_NOTICE_TOKENS
            + server.count_tokens_approx(sections[0][2])
            + server.count_tokens_approx(sections[1][2][:minimum])
        ),
        minimum_chars=server.BOOT_SECTION_MINIMUM_CHARS,
    )

    content = result.split("\n\n已按 boot 预算截断：", 1)[0]
    assert ("保" * minimum) in content
    assert f"- 部分截断：{protected_name}" in result
    assert "- 未输出：feel 回声" in result


def test_fit_sections_preserves_all_core_minima_before_echo(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    sections = [("triggers", "今日触发", "触" * 20)]
    display_names = {
        "mailbox": "最新 letter",
        "todos": "todos",
        "sessions": "最近归档",
        "pinned": "钉选索引",
    }
    section_chars = {
        "mailbox": "信",
        "todos": "待",
        "sessions": "档",
        "pinned": "钉",
    }
    for key, minimum in server.BOOT_SECTION_MINIMUM_CHARS.items():
        sections.append(
            (
                key,
                display_names[key],
                section_chars[key] * (minimum + 5000) + f"OVERFLOW_END_{key}",
            )
        )
    sections.append(
        ("echo", "feel 回声", "ECHO_BODY_MUST_BE_OMITTED" + "回" * 3000)
    )

    result = server._fit_sections_to_budget(
        sections,
        max_tokens=16000,
        minimum_chars=server.BOOT_SECTION_MINIMUM_CHARS,
    )
    content = result.split("\n\n已按 boot 预算截断：", 1)[0]

    for key, minimum in server.BOOT_SECTION_MINIMUM_CHARS.items():
        assert section_chars[key] * minimum in content
        assert f"OVERFLOW_END_{key}" not in content
    assert "已按 boot 预算截断" in result
    assert "- 部分截断：最新 letter、todos、最近归档、钉选索引" in result
    assert "- 未输出：feel 回声" in result
    assert "ECHO_BODY_MUST_BE_OMITTED" not in content


def test_fit_sections_tiny_budget_reports_every_omitted_name(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    minimums = server.BOOT_SECTION_MINIMUM_CHARS
    result = server._fit_sections_to_budget(
        [
            ("triggers", "今日触发", "触" * 100),
            ("mailbox", "最新 letter", "信" * minimums["mailbox"]),
            ("todos", "todos", "待" * minimums["todos"]),
            ("sessions", "最近归档", "档" * minimums["sessions"]),
            ("pinned", "钉选索引", "钉" * minimums["pinned"]),
            ("echo", "feel 回声", "回" * 100),
        ],
        max_tokens=180,
        minimum_chars=server.BOOT_SECTION_MINIMUM_CHARS,
    )

    assert "已按 boot 预算截断" in result
    assert "- 未输出：最新 letter、todos、最近归档、钉选索引、feel 回声" in result
    assert server.count_tokens_approx(result) <= 180


@pytest.mark.asyncio
async def test_response_seal_reads_runtime_env_each_call(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch, seal="first-runtime-seal")

    first = await server.boot()
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "second-runtime-seal")
    second = await server.boot()

    assert "seal: first-runtime-seal" in first
    assert "seal: second-runtime-seal" in second
    assert "seal: first-runtime-seal" not in second


@pytest.mark.asyncio
async def test_boot_hides_sealed_archive_bucket(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    sealed_id = await server.bucket_mgr.create(
        content="sealed archive boot filter control",
        domain=["session"],
        name="sealed_archive_boot_filter",
        sealed=True,
    )
    await server.bucket_mgr.archive(sealed_id)

    boot_result = await server.boot()

    assert sealed_id not in boot_result
    assert "sealed archive boot filter control" not in boot_result


@pytest.mark.asyncio
async def test_archive_session_sealed_true_persists_sealed_bucket(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)

    result = await server.archive_session("sealed archive persist control", sealed=True)
    bucket_id = result.split("bucket_id:", 1)[1].strip()
    bucket = await server.bucket_mgr.get(bucket_id)

    assert bucket["metadata"]["sealed"] == 1
    assert "archive" in bucket["path"]


@pytest.mark.asyncio
async def test_seal_letter_hides_default_mailbox_until_included(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    letter = "letter seal by id control"
    await server.archive_session("letter carrier", letter=letter)

    before = await server.breath(mailbox=True, mailbox_limit=10)
    result = await server.seal_letter(1, sealed=1)
    hidden = await server.breath(mailbox=True, mailbox_limit=10)
    included = await server.breath(mailbox=True, mailbox_limit=10, include_sealed=True)

    assert letter in before
    assert "letter_id:1 sealed" in result
    assert letter not in hidden
    assert letter in included


@pytest.mark.asyncio
async def test_get_letter_reads_exact_id_and_hides_sealed_by_default(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    await server.archive_session("first carrier", letter="first exact letter")
    await server.archive_session(
        "sealed carrier",
        letter="sealed exact letter",
        sealed=True,
    )

    first = await server.get_letter(1)
    hidden = await server.get_letter(2)
    included = await server.get_letter(2, include_sealed=True)

    assert "letter_id:1" in first
    assert "first exact letter" in first
    assert "sealed exact letter" not in first
    assert "letter_id not found: 2" in hidden
    assert "sealed exact letter" not in hidden
    assert "letter_id:2" in included
    assert "sealed exact letter" in included
