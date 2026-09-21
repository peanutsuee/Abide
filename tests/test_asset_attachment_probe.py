import json
import importlib
import sys

import pytest

import server


class _FakeMeta:
    def __init__(self, extra=None):
        self.model_extra = extra or {}


class _FakeRequestContext:
    def __init__(self, meta=None, experimental=None):
        self.meta = meta
        self.experimental = experimental


class _FakeContext:
    def __init__(self, meta=None, experimental=None):
        self.request_context = _FakeRequestContext(meta, experimental)


@pytest.mark.asyncio
async def test_attachment_probe_protocol_schema_and_empty_context(
    tmp_path,
    monkeypatch,
):
    from mcp.shared.memory import create_connected_server_and_client_session

    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_DIAG_TOOLS", "true")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    diagnostic_server = importlib.import_module("server")

    async with create_connected_server_and_client_session(
        diagnostic_server.mcp
    ) as client:
        tools = (await client.list_tools()).tools
        tool = next(
            item for item in tools
            if item.name == "asset_attachment_context_probe"
        )
        assert set(tool.inputSchema["properties"]) == {
            "attachment_reference",
            "attachment_mime_type",
        }
        assert "ctx" not in tool.inputSchema["properties"]
        assert "Do not" in tool.description
        result = await client.call_tool("asset_attachment_context_probe", {})

    assert not result.isError
    payload = json.loads(result.content[0].text)
    assert payload == {
        "ok": True,
        "attachment_reference_available": False,
        "attachment_bytes_available": False,
        "mime_type_available": False,
        "source_kind": "none",
        "received_parameter_names": [],
        "original_attachment_identity_verified": False,
    }


@pytest.mark.asyncio
async def test_attachment_probe_reports_parameter_names_without_values(
    caplog,
    monkeypatch,
):
    secret_reference = "opaque-reference-that-must-not-be-returned"

    def fail_persistence(*args, **kwargs):
        raise AssertionError("attachment probe must not persist files")

    monkeypatch.setattr(server.asset_store, "create_temp_path", fail_persistence)
    monkeypatch.setattr(server.asset_store, "persist_upload", fail_persistence)
    payload = json.loads(
        await server.asset_attachment_context_probe(
            _FakeContext(),
            attachment_reference=secret_reference,
            attachment_mime_type="image/png",
        )
    )

    assert payload["attachment_reference_available"] is True
    assert payload["attachment_bytes_available"] is False
    assert payload["mime_type_available"] is True
    assert payload["source_kind"] == "explicit_reference_parameter"
    assert payload["received_parameter_names"] == [
        "attachment_reference",
        "attachment_mime_type",
    ]
    assert payload["original_attachment_identity_verified"] is False
    serialized = json.dumps(payload, sort_keys=True)
    assert secret_reference not in serialized
    assert secret_reference not in caplog.text


@pytest.mark.asyncio
async def test_attachment_probe_detects_only_safe_context_signal_types(caplog):
    secret_reference = "context-reference-that-must-not-be-returned"
    secret_bytes = b"synthetic-bytes-that-must-not-be-returned"
    ctx = _FakeContext(
        meta=_FakeMeta(
            {
                "attachments": [
                    {
                        "attachment_id": secret_reference,
                        "mimeType": "image/jpeg",
                        "data": secret_bytes,
                    }
                ]
            }
        )
    )

    payload = json.loads(await server.asset_attachment_context_probe(ctx))

    assert payload["attachment_reference_available"] is True
    assert payload["attachment_bytes_available"] is True
    assert payload["mime_type_available"] is True
    assert payload["source_kind"] == "request_context_bytes"
    assert payload["received_parameter_names"] == []
    assert payload["original_attachment_identity_verified"] is False
    serialized = json.dumps(payload, sort_keys=True)
    assert secret_reference not in serialized
    assert secret_bytes.decode() not in serialized
    assert secret_reference not in caplog.text
    assert secret_bytes.decode() not in caplog.text


def test_attachment_probe_ignores_unscoped_generic_data():
    ctx = _FakeContext(
        experimental={
            "data": "not-an-attachment",
            "url": "not-an-attachment-reference",
            "mime_type": "text/plain",
        }
    )

    assert server._attachment_probe_context_signals(ctx) == (
        False,
        False,
        False,
    )
