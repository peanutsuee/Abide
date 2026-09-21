import importlib
import sys
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "test-seal")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    return server


@pytest.mark.asyncio
async def test_boot_includes_one_visible_feel_echo(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    feel_id = await server.bucket_mgr.create(
        content="visible echo body needle",
        name="Visible echo",
        bucket_type="feel",
    )

    result = await server.boot()

    assert feel_id in result
    assert "visible echo body needle" in result
    assert "seal: test-seal" in result


@pytest.mark.asyncio
async def test_breath_feels_searches_feel_channel(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    feel_id = await server.bucket_mgr.create(
        content="special feel lookup needle",
        name="Feel lookup",
        bucket_type="feel",
    )
    normal_id = await server.bucket_mgr.create(
        content="special feel lookup needle",
        name="Normal lookup",
    )

    result = await server.breath(feels=True, query="special feel lookup needle")

    assert feel_id in result
    assert normal_id not in result
    assert "seal: test-seal" in result


@pytest.mark.asyncio
async def test_sealed_feel_hidden_from_echo_and_feels_search(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    sealed_id = await server.bucket_mgr.create(
        content="sealed echo lookup needle",
        name="Sealed echo",
        bucket_type="feel",
    )
    await server.trace(sealed_id, sealed=1)

    boot_result = await server.boot()
    breath_result = await server.breath(feels=True, query="sealed echo lookup needle")

    assert sealed_id not in boot_result
    assert "sealed echo lookup needle" not in boot_result
    assert sealed_id not in breath_result
    assert "Sealed echo" not in breath_result
