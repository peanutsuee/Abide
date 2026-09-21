import builtins
import importlib
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RM_PIN = (
    "remember-me @ https://github.com/peanutsuee/Remember-Me/releases/download/"
    "v0.1.0-dev.7-public.1/Remember-Me-0.1.0.dev7-public.1-"
    "a00ea991442d7581a3856b178525a8e77da833fe.tar.gz"
    "#sha256=80a0b334f08db19c95c053537dec484be645f29fcf67898037e6641224012214"
)


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.delenv("OMBRE_RM_RUNTIME_ENABLED", raising=False)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


def test_core_and_optional_requirements_keep_remember_me_separate():
    core = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    optional = (ROOT / "requirements-remember-me.txt").read_text(encoding="utf-8")

    assert "remember-me @" not in core
    assert optional.splitlines()[0] == "-r requirements.txt"
    assert RM_PIN in optional


def test_disabled_remember_me_bootstrap_does_not_import_optional_runtime(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    original_import = builtins.__import__

    def forbid_optional_runtime(name, *args, **kwargs):
        if name == "remember_me_host_runtime":
            raise AssertionError("disabled Remember-Me bootstrap imported optional runtime")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", forbid_optional_runtime)
    assert server._bootstrap_remember_me_host() is None


def test_enabled_missing_remember_me_dependency_has_clear_install_error(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    monkeypatch.setenv("OMBRE_RM_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("OMBRE_RM_DATA_ROOT", str(tmp_path / "remember-me-data"))
    sys.modules.pop("remember_me_host_runtime", None)
    original_import = builtins.__import__

    def missing_optional_dependency(name, *args, **kwargs):
        if name == "remember_me_host_runtime":
            raise ModuleNotFoundError("No module named 'remember_me'", name="remember_me")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_optional_dependency)
    with pytest.raises(RuntimeError, match="remember_me_optional_dependency_missing"):
        server._bootstrap_remember_me_host()
