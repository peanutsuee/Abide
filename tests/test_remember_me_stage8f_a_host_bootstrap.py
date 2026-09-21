import gc
import importlib
import json
import os
import subprocess
import sys
import threading
from collections.abc import MutableMapping
from pathlib import Path

import pytest

from remember_me_host_runtime import (
    RememberMeHostRuntimeError,
    create_remember_me_host_bundle,
)


ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_CODE = "remember_me_host_bootstrap_failed"
ASSET_ID = "a" * 32


class CountingLock:
    def __init__(self):
        self.enter_count = 0
        self._lock = threading.Lock()

    def __enter__(self):
        self.enter_count += 1
        return self._lock.__enter__()

    def __exit__(self, exc_type, exc, traceback):
        return self._lock.__exit__(exc_type, exc, traceback)


def _env(tmp_path, **values):
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH")
    paths = [str(ROOT), *(pythonpath.split(os.pathsep) if pythonpath else [])]
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["OMBRE_BUCKETS_DIR"] = str(tmp_path / "buckets")
    env.pop("OMBRE_RM_RUNTIME_ENABLED", None)
    env.pop("OMBRE_RM_DATA_ROOT", None)
    env.pop("OMBRE_API_KEY", None)
    env.update({key: str(value) for key, value in values.items()})
    return env


def _run_python(script, tmp_path, *, env=None, check=True):
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env or _env(tmp_path),
        check=check,
        capture_output=True,
        text=True,
    )
    return completed


def _assert_stable_error(exc, tmp_path):
    assert str(exc.value) == BOOTSTRAP_CODE
    assert str(tmp_path) not in str(exc.value)
    assert "Traceback" not in str(exc.value)


def test_host_runtime_import_has_no_side_effects(tmp_path):
    script = """
import json
import os
import sys
from pathlib import Path

before = sorted(item.name for item in Path.cwd().iterdir())
os.environ["OMBRE_RM_DATA_ROOT"] = str(Path.cwd() / "must-not-be-read")
import remember_me_host_runtime
after = sorted(item.name for item in Path.cwd().iterdir())
print(json.dumps({
    "before": before,
    "after": after,
    "server_loaded": "server" in sys.modules,
    "mcp_loaded": any(name == "mcp" or name.startswith("mcp.") for name in sys.modules),
    "remember_me_core_loaded": "remember_me.core" in sys.modules,
    "remember_me_factory_loaded": "remember_me.factory" in sys.modules,
    "presenter_loaded": "remember_me_mcp_presenter" in sys.modules,
    "network_loaded": any(name in sys.modules for name in ("socket", "httpx")),
    "data_root_exists": (Path.cwd() / "must-not-be-read").exists(),
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=_env(tmp_path),
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["before"] == payload["after"]
    assert payload["server_loaded"] is False
    assert payload["mcp_loaded"] is False
    assert payload["remember_me_core_loaded"] is False
    assert payload["remember_me_factory_loaded"] is False
    assert payload["presenter_loaded"] is False
    assert payload["network_loaded"] is False
    assert payload["data_root_exists"] is False
    assert not list(tmp_path.rglob("assets.sqlite3"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"data_root": "not-a-path"},
        {"data_root": Path("relative")},
        {"token_store": []},
        {"download_lock": object()},
        {"public_base_url": object()},
        {"ttl_seconds": True},
        {"ttl_seconds": False},
        {"ttl_seconds": 0},
        {"ttl_seconds": -1},
        {"max_tokens": True},
        {"max_tokens": False},
        {"max_tokens": 0},
        {"max_tokens": -1},
    ],
)
def test_factory_validation_fails_closed_without_details(tmp_path, kwargs):
    params = {
        "data_root": tmp_path / "runtime",
        "token_store": {},
        "ticket_source_store": {},
        "download_lock": threading.Lock(),
        "public_base_url": "",
        "ttl_seconds": 300,
        "max_tokens": 100,
    }
    params.update(kwargs)

    with pytest.raises(RememberMeHostRuntimeError) as captured:
        create_remember_me_host_bundle(**params)

    _assert_stable_error(captured, tmp_path)


def test_factory_creates_bundle_and_shares_ticket_store_and_lock(tmp_path):
    token_store = {}
    source_store = {}
    lock = CountingLock()
    bundle = create_remember_me_host_bundle(
        data_root=tmp_path / "runtime",
        token_store=token_store,
        ticket_source_store=source_store,
        download_lock=lock,
        public_base_url="",
        ttl_seconds=300,
        max_tokens=100,
    )

    assert bundle.host_adapter.runtime_created is True
    assert bundle.core_adapter is not None
    assert bundle.download_links is not None
    assert bundle.presenter is not None
    assert bundle.host_adapter is bundle.core_adapter._host_adapter
    assert bundle.download_links._token_store is token_store
    assert bundle.download_links._ticket_source_store is source_store
    assert bundle.download_links._lock is lock
    assert bundle.download_links._ttl_seconds == 300
    assert bundle.download_links._max_tokens == 100

    ticket = bundle.download_links.create_download_link(
        {
            "asset_id": ASSET_ID,
            "mime_type": "image/png",
            "stored_bytes": 123,
            "stored_sha256": "stored",
            "filename": "image.png",
        }
    )
    token = ticket["download_path"].rsplit("/", 1)[-1]

    assert token in token_store
    assert source_store[token] == "remember_me"
    assert set(token_store[token]) == {"asset_id", "expires_at", "get_count"}
    assert token_store[token]["asset_id"] == ASSET_ID
    assert token_store[token]["get_count"] == 0
    assert lock.enter_count == 1

    with pytest.raises(RememberMeHostRuntimeError) as captured:
        create_remember_me_host_bundle(
            data_root=tmp_path / "runtime",
            token_store={},
            ticket_source_store={},
            download_lock=threading.Lock(),
            public_base_url="",
            ttl_seconds=300,
            max_tokens=100,
        )
    _assert_stable_error(captured, tmp_path)

    del bundle
    gc.collect()


@pytest.mark.parametrize("value", [None, "0", "false", "no", "off", ""])
def test_server_default_disabled_skips_host_import_and_data_root(tmp_path, value):
    data_root = tmp_path / "illegal-rm-root"
    env = _env(
        tmp_path,
        OMBRE_RM_DATA_ROOT=data_root,
    )
    if value is None:
        env.pop("OMBRE_RM_RUNTIME_ENABLED", None)
    else:
        env["OMBRE_RM_RUNTIME_ENABLED"] = value
    script = """
import json
import logging
import sys
from pathlib import Path

messages = []
class Handler(logging.Handler):
    def emit(self, record):
        messages.append(record.getMessage())

logging.getLogger("ombre_brain").addHandler(Handler())
import server
print(json.dumps({
    "runtime_components_initialized": server._runtime_components is not None,
    "host_module_loaded": "remember_me_host_runtime" in sys.modules,
    "data_root_exists": Path(__import__("os").environ["OMBRE_RM_DATA_ROOT"]).exists(),
    "messages": [item for item in messages if item.startswith("remember-me runtime")],
}))
"""
    completed = _run_python(script, tmp_path, env=env)
    payload = json.loads(completed.stdout)

    assert payload["runtime_components_initialized"] is False
    assert payload["host_module_loaded"] is False
    assert payload["data_root_exists"] is False
    assert payload["messages"] == []


def test_server_enabled_creates_bundle_with_shared_download_objects(tmp_path):
    env = _env(
        tmp_path,
        OMBRE_RM_RUNTIME_ENABLED=" true ",
        OMBRE_RM_DATA_ROOT=tmp_path / "rm-runtime",
    )
    script = """
import json
import logging

messages = []
class Handler(logging.Handler):
    def emit(self, record):
        messages.append(record.getMessage())

logging.getLogger("ombre_brain").addHandler(Handler())
import server
bundle = server._get_runtime_component("remember_me_host_bundle")
links = bundle.download_links
print(json.dumps({
    "bundle_created": bundle is not None,
    "runtime_created": bundle.host_adapter.runtime_created,
    "same_store": links._token_store is server._rm_asset_download_tokens,
    "same_source_store": links._ticket_source_store is server._rm_asset_download_sources,
    "same_lock": links._lock is server._rm_asset_download_lock,
    "ttl": links._ttl_seconds,
    "max_tokens": links._max_tokens,
    "server_ttl": server.RM_ASSET_DOWNLOAD_TTL_SECONDS,
    "server_max_tokens": server.RM_ASSET_DOWNLOAD_MAX_TOKENS,
    "messages": [item for item in messages if item.startswith("remember-me runtime")],
}))
"""
    completed = _run_python(script, tmp_path, env=env)
    payload = json.loads(completed.stdout)

    assert payload["bundle_created"] is True
    assert payload["runtime_created"] is True
    assert payload["same_store"] is True
    assert payload["same_source_store"] is True
    assert payload["same_lock"] is True
    assert payload["ttl"] == payload["server_ttl"] == 300
    assert payload["max_tokens"] == payload["server_max_tokens"] == 100
    assert payload["messages"] == ["remember-me runtime enabled"]


@pytest.mark.parametrize(
    ("env_values", "forbidden"),
    [
        ({"OMBRE_RM_RUNTIME_ENABLED": "true"}, "missing-root"),
        (
            {
                "OMBRE_RM_RUNTIME_ENABLED": "true",
                "OMBRE_RM_DATA_ROOT": "relative-root",
            },
            "relative-root",
        ),
    ],
)
def test_server_enabled_invalid_config_fails_closed(
    tmp_path,
    env_values,
    forbidden,
):
    completed = _run_python(
        "import server; server._bootstrap_remember_me_host()",
        tmp_path,
        env=_env(tmp_path, **env_values),
        check=False,
    )

    assert completed.returncode != 0
    combined = completed.stdout + completed.stderr
    assert "remember-me runtime bootstrap failed" in combined
    assert BOOTSTRAP_CODE in combined
    assert forbidden not in combined
    assert "AssetStore" not in combined


def test_server_enabled_runtime_exception_fails_closed_without_details(tmp_path, monkeypatch):
    import logging
    import types

    import server

    fake = types.ModuleType("remember_me_host_runtime")

    def fail(**kwargs):

        raise RuntimeError("private internal detail")

    fake.create_remember_me_host_bundle = fail

    messages = []

    class Handler(logging.Handler):

        def emit(self, record):

            messages.append(record.getMessage())

    handler = Handler()

    logging.getLogger("ombre_brain").addHandler(handler)

    monkeypatch.setitem(sys.modules, "remember_me_host_runtime", fake)

    monkeypatch.setenv("OMBRE_RM_RUNTIME_ENABLED", "true")

    monkeypatch.setenv("OMBRE_RM_DATA_ROOT", str(tmp_path / "rm-runtime"))


    try:

        with pytest.raises(RuntimeError) as captured:

            server._bootstrap_remember_me_host()

    finally:

        logging.getLogger("ombre_brain").removeHandler(handler)


    assert str(captured.value) == BOOTSTRAP_CODE

    assert "remember-me runtime bootstrap failed" in messages

    combined = "\n".join(messages) + str(captured.value)

    assert "private internal detail" not in combined

    assert str(tmp_path) not in combined


def test_stage8f_a_bootstrap_surface_remains_compatible_after_later_wiring():
    server_text = (ROOT / "server.py").read_text(encoding="utf-8")
    production_modules = [
        ROOT / "asset_dashboard.py",
        ROOT / "asset_viewer.py",
        ROOT / "asset_embedding_index.py",
    ]
    forbidden_functions = [
        "rm_asset_download_route",
    ]

    for name in forbidden_functions:
        start = server_text.index(f"async def {name}")
        end = server_text.find("\nasync def ", start + 1)
        next_route = server_text.find("\n@mcp.", start + 1)
        candidates = [item for item in (end, next_route) if item != -1]
        stop = min(candidates) if candidates else len(server_text)
        assert "remember_me_host_bundle" not in server_text[start:stop]

    reindex_start = server_text.index("async def rm_asset_reindex_embeddings")
    reindex_stop = server_text.find("\n@mcp.", reindex_start + 1)
    reindex_block = server_text[reindex_start:reindex_stop]
    assert "await backend.mcp_reindex(" in reindex_block
    assert "await backend.reindex(" in reindex_block

    assert server_text.count("@mcp.custom_route") == 37
    assert "store = AssetStore(config[\"buckets_dir\"])" in server_text
    assert 'asset_store = _LazyRuntimeComponent("asset_store")' in server_text
    assert "def _get_remember_me_host_bundle" in server_text
    assert "def __getattr__(name: str):" in server_text
    assert "remember_me_host_runtime" in server_text

    for path in production_modules:
        text = path.read_text(encoding="utf-8")
        assert "remember_me_host_bundle" not in text
        assert "remember_me_host_runtime" not in text

    assert "expected_sha256" not in (
        ROOT / "remember_me_host_runtime.py"
    ).read_text(encoding="utf-8")


def test_mcp_tool_counts_remain_unchanged(tmp_path):
    script = """
import asyncio
import json
import server
from mcp.shared.memory import create_connected_server_and_client_session

async def main():
    async with create_connected_server_and_client_session(server.mcp) as client:
        tools = (await client.list_tools()).tools
    print(json.dumps([tool.name for tool in tools]))

asyncio.run(main())
"""

    default = _run_python(script, tmp_path)
    diagnostic = _run_python(
        script,
        tmp_path,
        env=_env(tmp_path, OMBRE_DIAG_TOOLS="true"),
    )

    assert len(json.loads(default.stdout)) == 26
    assert len(json.loads(diagnostic.stdout)) == 41


def test_stage8b_ob_schema_snapshot_file_remains_unchanged():
    snapshot = json.loads(
        (ROOT / "tests/fixtures/stage8b-ob-rm-mcp-contract.json").read_text(
            encoding="utf-8"
        )
    )
    tools = snapshot["tools"]

    assert len(tools) == 9
    assert [tool["name"] for tool in tools] == [
        "rm_asset_upload_link",
        "rm_asset_upload_status",
        "rm_asset_get",
        "rm_asset_update_metadata",
        "rm_asset_search",
        "rm_asset_reindex_embeddings",
        "rm_asset_download_link",
        "rm_asset_view",
        "rm_asset_inspect",
    ]
