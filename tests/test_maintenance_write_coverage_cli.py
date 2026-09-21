from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

import maintenance_write_coverage as coverage


ROOT = Path(__file__).resolve().parents[1]


def test_main_returns_zero_and_reports_passed_when_scan_is_clean(monkeypatch, capsys):
    monkeypatch.setattr(coverage, "scan_registered_write_coverage", lambda root: [])

    assert coverage.main() == 0
    captured = capsys.readouterr()
    assert captured.out == "Write coverage audit passed.\n"
    assert captured.err == ""


def test_main_returns_nonzero_and_reports_each_coverage_issue(monkeypatch, capsys):
    issue = coverage.WriteCoverageIssue(
        filename="new_production.py",
        function="mutate",
        line=7,
        primitive="write_bytes",
    )
    monkeypatch.setattr(
        coverage,
        "scan_registered_write_coverage",
        lambda root: [issue],
    )

    assert coverage.main() != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "Write coverage audit failed:\n"
        "new_production.py:7: mutate: write_bytes\n"
    )


def test_main_fails_closed_when_scan_raises(monkeypatch, capsys):
    def raise_scan_error(root):
        raise OSError("synthetic scan failure")

    monkeypatch.setattr(coverage, "scan_registered_write_coverage", raise_scan_error)

    assert coverage.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Write coverage audit failed to scan: synthetic scan failure\n"


def test_direct_script_execution_runs_the_real_coverage_audit(tmp_path):
    script_path = tmp_path / "maintenance_write_coverage.py"
    shutil.copy2(ROOT / "maintenance_write_coverage.py", script_path)
    completed = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == "Write coverage audit passed.\n"
    assert completed.stderr == ""
