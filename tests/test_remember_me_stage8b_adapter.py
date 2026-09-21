import importlib
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

import remember_me_adapter as adapter_module
from remember_me.core import (
    AssetBlobVerificationResult,
    AssetVerificationCompletion,
    AssetVerificationPage,
    AssetVerificationSnapshot,
    BeginAssetVerificationRequest,
    CompleteAssetVerificationRequest,
    ListAssetVerificationPageRequest,
    RememberMeService,
    VerifyAssetBlobRequest,
)
from remember_me_adapter import (
    EXPECTED_MCP_TOOLS,
    RememberMeAdapter,
    RememberMeAdapterError,
    inspect_remember_me_contract,
    validate_remember_me_contract,
)


ROOT = Path(__file__).resolve().parent.parent
EXPECTED_VERSION = "0.1.0.dev7"
EXPECTED_TAG = "v0.1.0-dev.7-public.1"
EXPECTED_COMMIT = "a00ea991442d7581a3856b178525a8e77da833fe"
EXPECTED_TREE = "a958d995421c97ccc572b127cb859797aa7a415f"
EXPECTED_ARCHIVE_SHA256 = (
    "80a0b334f08db19c95c053537dec484be645f29fcf67898037e6641224012214"
)
EXPECTED_ARCHIVE_URL = (
    "https://github.com/peanutsuee/Remember-Me/releases/download/"
    "v0.1.0-dev.7-public.1/Remember-Me-0.1.0.dev7-public.1-"
    "a00ea991442d7581a3856b178525a8e77da833fe.tar.gz"
)
OLD_COMMIT = "184e223c6392fd14dd5cfa73227d41f46d90e3c8"


def _requirement_line():
    return next(
        line.strip()
        for line in (ROOT / "requirements-remember-me.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip().startswith("remember-me @")
    )


def test_dependency_is_immutable_release_asset_and_digest_pinned():
    line = _requirement_line()
    integration_text = (ROOT / "docs" / "remember-me-integration.md").read_text(
        encoding="utf-8"
    )

    assert line == "remember-me @ {}#sha256={}".format(
        EXPECTED_ARCHIVE_URL,
        EXPECTED_ARCHIVE_SHA256,
    )
    assert len(EXPECTED_COMMIT) == 40
    assert EXPECTED_VERSION in EXPECTED_ARCHIVE_URL
    assert EXPECTED_TAG in EXPECTED_ARCHIVE_URL
    assert EXPECTED_COMMIT in EXPECTED_ARCHIVE_URL
    assert EXPECTED_TREE in integration_text
    assert "git+" not in line
    assert "remember-me[" not in line
    assert "/main" not in line
    assert "/tarball/" not in line
    assert "/archive/refs/" not in line
    assert OLD_COMMIT not in line


def test_adapter_import_has_no_storage_or_protocol_side_effects(tmp_path):
    script = """
import json
import sys
from pathlib import Path
before = sorted(item.name for item in Path.cwd().iterdir())
import remember_me_adapter
after = sorted(item.name for item in Path.cwd().iterdir())
print(json.dumps({
    "before": before,
    "after": after,
    "remember_me_loaded": any(
        name == "remember_me" or name.startswith("remember_me.")
        for name in sys.modules
    ),
    "server_loaded": "server" in sys.modules,
}))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["OMBRE_BUCKETS_DIR"] = str(tmp_path / "must-not-be-read")
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = __import__("json").loads(completed.stdout)

    assert payload["before"] == payload["after"]
    assert payload["remember_me_loaded"] is False
    assert payload["server_loaded"] is False
    assert not (tmp_path / "must-not-be-read").exists()
    assert not list(tmp_path.rglob("assets.sqlite3"))


def test_installed_contract_matches_pinned_public_package():
    contract = inspect_remember_me_contract()

    assert validate_remember_me_contract(contract) is contract
    assert contract.mcp_tools == EXPECTED_MCP_TOOLS
    assert "remember_me.standalone" not in sys.modules


def test_public_asset_verification_contract_is_available():
    assert all(
        item is not None
        for item in (
            BeginAssetVerificationRequest,
            ListAssetVerificationPageRequest,
            VerifyAssetBlobRequest,
            CompleteAssetVerificationRequest,
            AssetVerificationSnapshot,
            AssetVerificationPage,
            AssetBlobVerificationResult,
            AssetVerificationCompletion,
        )
    )
    assert all(
        callable(getattr(RememberMeService, method_name, None))
        for method_name in (
            "begin_asset_verification",
            "list_asset_verification_page",
            "verify_asset_blob",
            "complete_asset_verification",
        )
    )


def test_contract_inspection_failure_is_redacted():
    private_value = "D:\\private\\site-packages\\secret"
    with patch.object(
        adapter_module.metadata,
        "distribution",
        side_effect=RuntimeError(private_value),
    ):
        with pytest.raises(RememberMeAdapterError) as captured:
            inspect_remember_me_contract()

    assert str(captured.value) == "remember_me_contract_unavailable"
    assert private_value not in str(captured.value)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("distribution_name", "wrong"),
        ("package_version", "0.1.0.dev4"),
        ("data_compatibility", "wrong"),
        ("sanitizer_id", "wrong"),
        ("pillow_range", "Pillow>=10.4,<11"),
        ("mcp_tools", EXPECTED_MCP_TOOLS[:-1]),
        ("mcp_tools", EXPECTED_MCP_TOOLS + ("extra",)),
        ("mcp_tools", tuple(reversed(EXPECTED_MCP_TOOLS))),
    ],
)
def test_contract_mismatches_fail_closed_without_values(field, bad_value):
    contract = inspect_remember_me_contract()

    with pytest.raises(RememberMeAdapterError) as captured:
        validate_remember_me_contract(replace(contract, **{field: bad_value}))

    message = str(captured.value)
    assert message == "remember_me_contract_mismatch:{}".format(field)
    assert str(bad_value) not in message


def test_runtime_is_created_only_explicitly_and_reused_for_same_root(tmp_path):
    instance = RememberMeAdapter()
    root = tmp_path / "runtime"

    assert instance.runtime_created is False
    assert not root.exists()
    runtime = instance.create_runtime(root)

    assert instance.runtime_created is True
    assert (root / "assets.sqlite3").is_file()
    assert instance.create_runtime(root) is runtime

    with pytest.raises(
        RememberMeAdapterError,
        match="^remember_me_runtime_already_created$",
    ):
        instance.create_runtime(tmp_path / "other")


def test_second_adapter_cannot_create_writer_for_same_root(tmp_path):
    root = tmp_path / "runtime"
    first = RememberMeAdapter()
    second = RememberMeAdapter()
    first.create_runtime(root)

    with pytest.raises(
        RememberMeAdapterError,
        match="^remember_me_data_root_already_owned$",
    ):
        second.create_runtime(root)


def test_runtime_requires_explicit_path_without_leaking_value(tmp_path):
    value = str(tmp_path / "private")
    with pytest.raises(RememberMeAdapterError) as captured:
        RememberMeAdapter().create_runtime(value)

    assert str(captured.value) == "remember_me_data_root_must_be_path"
    assert value not in str(captured.value)


def test_adapter_module_does_not_import_standalone_or_server():
    sys.modules.pop("remember_me.standalone", None)
    sys.modules.pop("server", None)
    importlib.reload(adapter_module)

    assert "remember_me.standalone" not in sys.modules
    assert "server" not in sys.modules
