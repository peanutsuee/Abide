from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from maintenance_write_coverage import (
    NON_PATH_CALL_ALLOWLIST,
    scan_registered_source,
    scan_registered_write_coverage,
    scan_unregistered_source,
)


ROOT = Path(__file__).parents[1]


def test_non_path_allowlist_uses_structural_identity_without_line_numbers():
    assert all(
        len(entry) == 4 and not any(isinstance(value, int) for value in entry)
        for entry in NON_PATH_CALL_ALLOWLIST
    )


def test_registered_server_source_tolerates_unrelated_statement_before_write():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    changed = source.replace(
        'logger = logging.getLogger("ombre_brain")',
        'logger = logging.getLogger("ombre_brain")\n_coverage_unrelated_marker = None',
        1,
    )
    assert scan_registered_source(changed, "server.py") == []


def test_registered_server_source_rejects_missing_structural_exemption():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    changed = source.replace(
        "await bucket_mgr.touch(bucket_id)",
        "await bucket_mgr.unregistered_touch(bucket_id)",
        1,
    )
    issues = scan_registered_source(changed, "server.py")
    assert any(
        issue.function == "breath_hook"
        and issue.primitive.startswith("allowlist_structure_mismatch:bucket_mgr.touch")
        for issue in issues
    )


def test_new_unregistered_production_write_still_fails_closed(tmp_path):
    source = "def phase4_unregistered(path):\n    path.write_text('x')\n"
    assert any(
        issue.function == "phase4_unregistered" and issue.primitive == "write_text"
        for issue in scan_unregistered_source(source, "new_production.py")
    )
    module = tmp_path / "new_production.py"
    module.write_text(source, encoding="utf-8")
    assert scan_registered_write_coverage(tmp_path)


def test_server_import_is_inert_until_lazy_asset_store_access(tmp_path):
    buckets_dir = tmp_path / "buckets"
    code = "\n".join(
        [
            "import os",
            "from pathlib import Path",
            "root = Path(os.environ['OMBRE_BUCKETS_DIR'])",
            "import server",
            "assert not root.exists()",
            "base = server.AssetStore",
            "class CountingStore(base):",
            "    count = 0",
            "    def __init__(self, *args, **kwargs):",
            "        type(self).count += 1",
            "        super().__init__(*args, **kwargs)",
            "server.AssetStore = CountingStore",
            "server.asset_store.search(kind='image')",
            "first = server._get_runtime_component('asset_store')",
            "server.asset_store.search(kind='image')",
            "assert server._get_runtime_component('asset_store') is first",
            "assert CountingStore.count == 1",
            "assert (root / 'assets.sqlite3').is_file()",
        ]
    )
    env = os.environ.copy()
    env["OMBRE_BUCKETS_DIR"] = str(buckets_dir)
    env["OMBRE_RM_RUNTIME_ENABLED"] = ""
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
