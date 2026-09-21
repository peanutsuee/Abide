import importlib
import sys
from unittest.mock import AsyncMock

import frontmatter
import pytest


@pytest.fixture
def server_module(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "synthetic-seal")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.dehydrator.dehydrate = AsyncMock(
        side_effect=lambda content, metadata=None, **kwargs: content
    )
    if server.embedding_engine:
        server.embedding_engine.enabled = False
    return server


async def _create_dormant(server, *, content="synthetic dormant anchor", **kwargs):
    bucket_id = await server.bucket_mgr.create(content=content, **kwargs)
    await server.bucket_mgr.set_dormant(bucket_id, True)
    return bucket_id


async def _metadata(server, bucket_id):
    return (await server.bucket_mgr.get(bucket_id))["metadata"]


@pytest.mark.asyncio
async def test_dream_details_default_does_not_wake_dormant(server_module):
    bucket_id = await _create_dormant(
        server_module,
        content="synthetic dream detail anchor",
    )

    result = await server_module.dream(detail_ids=bucket_id)
    metadata = await _metadata(server_module, bucket_id)

    assert bucket_id in result
    assert metadata["dormant"] is True
    assert metadata["activation_count"] == 1


@pytest.mark.asyncio
async def test_dream_without_details_does_not_wake_dormant(server_module):
    bucket_id = await _create_dormant(server_module)

    await server_module.dream()
    metadata = await _metadata(server_module, bucket_id)

    assert metadata["dormant"] is True
    assert metadata["activation_count"] == 0


@pytest.mark.asyncio
async def test_breath_excluding_dormant_does_not_wake(server_module):
    anchor = "synthetic-hidden-breath-anchor"
    bucket_id = await _create_dormant(server_module, content=anchor)

    result = await server_module.breath(query=anchor, include_dormant=False)
    metadata = await _metadata(server_module, bucket_id)

    assert bucket_id not in result
    assert metadata["dormant"] is True
    assert metadata["activation_count"] == 0


@pytest.mark.asyncio
async def test_breath_including_dormant_default_does_not_wake(server_module):
    anchor = "synthetic-visible-breath-anchor"
    bucket_id = await _create_dormant(server_module, content=anchor)
    bucket_path = server_module.bucket_mgr._find_bucket_file(bucket_id)
    post = frontmatter.load(bucket_path)
    old_last_active = "2000-01-01T00:00:00"
    post["last_active"] = old_last_active
    with open(bucket_path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(post))

    result = await server_module.breath(query=anchor, include_dormant=True)
    metadata = await _metadata(server_module, bucket_id)

    assert bucket_id in result
    assert metadata["dormant"] is True
    assert metadata["activation_count"] == 1
    assert metadata["last_active"] != old_last_active


@pytest.mark.asyncio
async def test_boot_does_not_wake_dormant(server_module):
    bucket_id = await _create_dormant(
        server_module,
        content="synthetic pinned boot anchor",
        pinned=True,
    )

    result = await server_module.boot()
    metadata = await _metadata(server_module, bucket_id)

    assert bucket_id in result
    assert metadata["dormant"] is True
    assert metadata["activation_count"] == 0


@pytest.mark.asyncio
async def test_pulse_does_not_wake_dormant(server_module):
    bucket_id = await _create_dormant(server_module)

    result = await server_module.pulse(show_all=True)
    metadata = await _metadata(server_module, bucket_id)

    assert bucket_id in result
    assert metadata["dormant"] is True
    assert metadata["activation_count"] == 0


@pytest.mark.asyncio
async def test_get_letter_does_not_wake_linked_dormant_bucket(server_module):
    bucket_id = await _create_dormant(server_module)
    server_module.bucket_mgr.record_letter(
        "synthetic handoff letter",
        bucket_id,
    )
    letter_id = server_module.bucket_mgr.get_letters(limit=1)[0]["id"]

    result = await server_module.get_letter(letter_id)
    metadata = await _metadata(server_module, bucket_id)

    assert "synthetic handoff letter" in result
    assert metadata["dormant"] is True
    assert metadata["activation_count"] == 0


@pytest.mark.asyncio
async def test_todos_does_not_wake_dormant(server_module):
    bucket_id = await _create_dormant(
        server_module,
        todos=["synthetic dormant todo"],
    )

    result = await server_module.todos()
    metadata = await _metadata(server_module, bucket_id)

    assert "synthetic dormant todo" in result
    assert metadata["dormant"] is True
    assert metadata["activation_count"] == 0


@pytest.mark.asyncio
async def test_trace_append_does_not_wake_dormant(server_module):
    bucket_id = await _create_dormant(server_module, content="trace append anchor")

    await server_module.trace(bucket_id, content="appended", append=True)

    assert (await _metadata(server_module, bucket_id))["dormant"] is True


@pytest.mark.asyncio
async def test_trace_name_and_tags_do_not_wake_dormant(server_module):
    bucket_id = await _create_dormant(server_module)

    await server_module.trace(bucket_id, name="renamed dormant bucket")
    assert (await _metadata(server_module, bucket_id))["dormant"] is True

    await server_module.trace(bucket_id, tags="trace,dormant")
    assert (await _metadata(server_module, bucket_id))["dormant"] is True


@pytest.mark.asyncio
async def test_trace_explicit_dormant_zero_wakes_dormant_bucket(server_module):
    bucket_id = await _create_dormant(server_module)

    await server_module.trace(bucket_id, dormant=0)

    assert (await _metadata(server_module, bucket_id))["dormant"] is False


@pytest.mark.asyncio
async def test_trace_keeps_active_bucket_active_for_common_updates(server_module):
    bucket_id = await server_module.bucket_mgr.create(content="active trace anchor")

    await server_module.trace(bucket_id, content="appended", append=True)
    await server_module.trace(bucket_id, name="renamed active bucket")
    await server_module.trace(bucket_id, tags="trace,active")

    assert (await _metadata(server_module, bucket_id))["dormant"] is False


@pytest.mark.asyncio
async def test_dream_explicit_wake_switch_clears_dormant(server_module):
    bucket_id = await _create_dormant(server_module)

    await server_module.dream(detail_ids=bucket_id, wake_dormant=True)

    assert (await _metadata(server_module, bucket_id))["dormant"] is False


@pytest.mark.asyncio
async def test_breath_explicit_wake_switch_clears_dormant(server_module):
    anchor = "synthetic-explicit-wake-anchor"
    bucket_id = await _create_dormant(server_module, content=anchor)

    await server_module.breath(
        query=anchor,
        include_dormant=True,
        wake_dormant=True,
    )

    assert (await _metadata(server_module, bucket_id))["dormant"] is False
