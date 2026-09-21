import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from remember_me.mcp.tools import register_tools
from remember_me_adapter import EXPECTED_MCP_TOOLS


ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
OB_SNAPSHOT = FIXTURES / "stage8b-ob-rm-mcp-contract.json"
RM_SNAPSHOT = FIXTURES / "stage8b-public-rm-mcp-contract.json"


def _normalize_tool(tool):
    raw = tool.model_dump(by_alias=True, exclude_none=True)
    schema = raw["inputSchema"]
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    return {
        "name": raw["name"],
        "description": raw.get("description", ""),
        "inputSchema": schema,
        "required": required,
        "optional": [key for key in properties if key not in required],
        "defaults": {
            key: value["default"]
            for key, value in properties.items()
            if "default" in value
        },
        "annotations": raw.get("annotations", {}),
        "_meta": raw.get("_meta", {}),
        "outputSchema": raw.get("outputSchema", {}),
    }


async def _rm_tools():
    mcp = FastMCP("stage8b-rm-contract")
    register_tools(mcp, object(), object(), object())
    async with create_connected_server_and_client_session(mcp) as client:
        tools = (await client.list_tools()).tools
    return [_normalize_tool(tool) for tool in tools]


OB_PROBE = r"""
import asyncio
import json
import server
from mcp.shared.memory import create_connected_server_and_client_session

async def main():
    async with create_connected_server_and_client_session(server.mcp) as client:
        tools = (await client.list_tools()).tools
    print("STAGE8B_JSON=" + json.dumps([
        tool.model_dump(by_alias=True, exclude_none=True)
        for tool in tools if tool.name.startswith("rm_asset_")
    ], separators=(",", ":")))

asyncio.run(main())
"""


def _ob_tools(tmp_path):
    env = os.environ.copy()
    env["OMBRE_BUCKETS_DIR"] = str(tmp_path / "buckets")
    env.pop("OMBRE_API_KEY", None)
    env.pop("OMBRE_DIAG_TOOLS", None)
    completed = subprocess.run(
        [sys.executable, "-c", OB_PROBE],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    line = next(
        item for item in completed.stdout.splitlines()
        if item.startswith("STAGE8B_JSON=")
    )
    tools = json.loads(line.partition("=")[2])

    class ToolRecord:
        def __init__(self, payload):
            self.payload = payload

        def model_dump(self, **_kwargs):
            return self.payload

    return [_normalize_tool(ToolRecord(tool)) for tool in tools]


def _safe_output_shapes(profile):
    common_error = (
        {"ok": False, "error": "<error_code>"}
        if profile == "ob"
        else {
            "content": [{"type": "text", "text": "<safe_error_json>"}],
            "structuredContent": {
                "ok": False,
                "error": {"code": "<error_code>", "message": "<safe_message>"},
            },
            "isError": True,
        }
    )
    if profile == "ob":
        success = {
            "rm_asset_upload_link": {
                "ok": True, "upload_id": "<id>", "upload_path": "<path>",
                "upload_url": "<signed_url>", "status_path": "<path>",
                "expires_in_seconds": "<integer>",
            },
            "rm_asset_upload_status": {
                "ok": True, "upload_id": "<id>", "state": "<state>",
                "asset_id": "<id>", "source_sha256": "<hash>",
            },
            "rm_asset_get": {"ok": True, "<asset_metadata>": "<fields>"},
            "rm_asset_update_metadata": {
                "ok": True, "<asset_metadata>": "<fields>"
            },
            "rm_asset_reindex_embeddings": {
                "ok": True, "scanned": "<integer>", "indexed": "<integer>",
                "skipped": "<integer>", "failed": "<integer>",
            },
            "rm_asset_search": {
                "ok": True, "total": "<integer>", "offset": "<integer>",
                "limit": "<integer>", "results": "<asset_list>",
            },
            "rm_asset_download_link": {
                "ok": True, "asset_id": "<id>", "download_path": "<path>",
                "download_url": "<signed_url>",
            },
            "rm_asset_view": {
                "content": [{"type": "text", "text": "<safe_text>"}],
                "structuredContent": "<flat_asset_view>",
                "_meta": {"rememberMe": "<viewer_payload_without_bytes>"},
            },
            "rm_asset_inspect": {
                "content": [
                    {"type": "text", "text": "<safe_text>"},
                    {"type": "image", "data": "<base64>", "mimeType": "<mime>"},
                ],
                "structuredContent": "<flat_asset_view>",
            },
        }
    else:
        success = {
            "rm_asset_upload_link": {
                "ok": True, "upload_id": "<id>", "upload_url": "<signed_url>",
                "expires_at": "<timestamp>", "max_bytes": "<integer>",
            },
            "rm_asset_upload_status": {
                "ok": True, "upload_id": "<id>", "status": "<state>",
                "asset": "<nested_asset>", "deduplicated": "<boolean>",
            },
            "rm_asset_get": {"ok": True, "asset": "<nested_asset>"},
            "rm_asset_update_metadata": {
                "ok": True, "asset": "<nested_asset>"
            },
            "rm_asset_reindex_embeddings": {
                "ok": True, "enabled": "<boolean>", "model_id": "<model>",
                "selected": "<integer>", "indexed": "<integer>",
                "failed": "<integer>",
            },
            "rm_asset_search": {
                "ok": True, "total": "<integer>", "offset": "<integer>",
                "limit": "<integer>", "items": "<asset_list>",
            },
            "rm_asset_download_link": {
                "ok": True, "asset_id": "<id>", "download_url": "<signed_url>",
                "expires_at": "<timestamp>",
                "max_successful_gets": "<integer>",
            },
            "rm_asset_view": {
                "content": [{"type": "text", "text": "<safe_text>"}],
                "structuredContent": {"ok": True, "asset": "<nested_asset>"},
                "_meta": {"remember_me": "<viewer_payload_without_bytes>"},
            },
            "rm_asset_inspect": {
                "content": [
                    {"type": "text", "text": "<safe_text>"},
                    {"type": "image", "data": "<base64>", "mimeType": "<mime>"},
                ],
                "structuredContent": {"ok": True, "asset": "<nested_asset>"},
            },
        }
    return {
        name: {"success": success[name], "error": common_error}
        for name in EXPECTED_MCP_TOOLS
    }


def _snapshot(tools, profile):
    return {
        "profile": profile,
        "tools": tools,
        "safeOutputShapes": _safe_output_shapes(profile),
        "redactions": [
            "token", "signed_url", "complete_hash", "base64",
            "local_path", "user_data", "real_timestamp",
        ],
    }


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_ob_mcp_contract_matches_unchanged_stage8b_snapshot(tmp_path):
    assert _snapshot(_ob_tools(tmp_path), "ob") == _load(OB_SNAPSHOT)


def test_public_rm_mcp_contract_matches_pinned_stage8b_snapshot():
    assert _snapshot(asyncio.run(_rm_tools()), "public-rm") == _load(
        RM_SNAPSHOT
    )


def test_both_surfaces_have_exact_names_and_hash_free_upload_contract(tmp_path):
    ob_tools = _ob_tools(tmp_path)
    rm_tools = asyncio.run(_rm_tools())

    for tools in (ob_tools, rm_tools):
        assert len(tools) == len(EXPECTED_MCP_TOOLS)
        assert {item["name"] for item in tools} == set(EXPECTED_MCP_TOOLS)
        upload = next(
            item for item in tools
            if item["name"] == "rm_asset_upload_link"
        )
        assert tuple(upload["inputSchema"]["properties"]) == (
            "expected_bytes",
            "filename",
            "mime_type",
        )
        assert upload["required"] == ["expected_bytes"]
        assert "expected_sha256" not in json.dumps(upload)
    assert tuple(item["name"] for item in rm_tools) == EXPECTED_MCP_TOOLS


if __name__ == "__main__":
    import tempfile

    FIXTURES.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        ob = _snapshot(_ob_tools(Path(directory)), "ob")
    rm = _snapshot(asyncio.run(_rm_tools()), "public-rm")
    OB_SNAPSHOT.write_text(
        json.dumps(ob, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    RM_SNAPSHOT.write_text(
        json.dumps(rm, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )
