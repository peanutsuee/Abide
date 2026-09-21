from datetime import datetime, timezone
import os

from remember_me_cutover_operations import acceptance_check_spec
from remember_me_cutover_transition import (
    ACCEPTANCE_ARTIFACT_SCHEMA_VERSION,
    _digest,
)


def valid_rm_acceptance_artifact(store, controller, *, passed=True, run_id="acceptance-test-1"):
    runtime = {
        "instance_id": os.environ.setdefault("RENDER_INSTANCE_ID", "test-instance"),
        "git_commit": os.environ.setdefault("RENDER_GIT_COMMIT", "test-commit"),
        "service_id": os.environ.setdefault("RENDER_SERVICE_ID", "test-service"),
    }
    snapshot = store.get_snapshot()
    record = controller._read_record()
    freeze = controller._current_freeze_metadata(snapshot)
    cutover_identity = {
        "revision": snapshot.revision,
        "cutover_state": snapshot.state.value,
        "phase": controller._current_phase(record, snapshot),
        "authority": snapshot.authority.value,
        "freeze_status": snapshot.freeze_status,
        "lease_generation": freeze["generation"],
        "lease_acquired_at": freeze["acquired_at"].isoformat(timespec="microseconds"),
        "lease_expires_at": freeze["expires_at"].isoformat(timespec="microseconds"),
    }
    checks = {name: True for name in acceptance_check_spec()["checks"]}
    if not passed:
        checks["dashboard_detail"] = False
    status = "PASS" if passed else "FAIL"
    evidence = {
        "artifact_type": "rm_acceptance_evidence",
        "schema_version": ACCEPTANCE_ARTIFACT_SCHEMA_VERSION,
        "acceptance_run_id": run_id,
        "created_at": cutover_identity["lease_acquired_at"],
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "status": status,
        "state": {"authority": "rm", "frozen": True},
        "cutover_identity": cutover_identity,
        "runtime_identity": runtime,
        "checks": {name: {"status": "PASS" if value else "FAIL"} for name, value in checks.items()},
        "summary": {
            "pass": sum(checks.values()),
            "incomplete": 0,
            "fail": len(checks) - sum(checks.values()),
            "side_effects_free": True,
        },
    }
    artifact = {
        "artifact_type": "rm_acceptance_checks",
        "schema_version": ACCEPTANCE_ARTIFACT_SCHEMA_VERSION,
        "acceptance_run_id": run_id,
        "created_at": evidence["created_at"],
        "completed_at": evidence["completed_at"],
        "status": status,
        "cutover_identity": cutover_identity,
        "runtime_identity": runtime,
        "evidence_sha256": _digest(evidence),
        "checks": checks,
    }
    return artifact, evidence
