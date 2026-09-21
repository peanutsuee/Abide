"""Default-disabled backup-v2 production registration."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any, Mapping


logger = logging.getLogger("ombre_brain.backup_v2")

ENABLE_ENV = "OMBRE_BACKUP_V2_ENABLED"
REQUIRED_ENV = (
    "OMBRE_BACKUP_V2_PUBLIC_KEY_B64",
    "OMBRE_BACKUP_V2_RECIPIENT_FINGERPRINT",
    "OMBRE_BACKUP_V2_REPOSITORY_ID",
    "OMBRE_BACKUP_V2_REPOSITORY_OWNER_ID",
    "OMBRE_BACKUP_V2_WORKSPACE_ROOT",
    "OMBRE_BACKUP_V2_FREEZE_TIMEOUT_SECONDS",
    "OMBRE_BACKUP_V2_MAX_FREEZE_SECONDS",
    "OMBRE_BACKUP_V2_MAX_SOURCE_BYTES",
    "OMBRE_BACKUP_V2_MAX_BUNDLE_BYTES",
    "OMBRE_BACKUP_V2_MINIMUM_FREE_BYTES",
    "OMBRE_BACKUP_V2_READY_TTL_SECONDS",
    "RENDER_GIT_COMMIT",
)
V2_ROUTE_SIGNATURES = frozenset({
    ("POST", "/api/backup/v2/captures"),
    ("GET", "/api/backup/v2/captures/{request_id}"),
    ("GET", "/api/backup/v2/captures/{request_id}/bundle"),
    ("POST", "/api/backup/v2/captures/{request_id}/ack"),
})
NUMERIC_BOUNDS = {
    "OMBRE_BACKUP_V2_FREEZE_TIMEOUT_SECONDS": (1, 600),
    "OMBRE_BACKUP_V2_MAX_FREEZE_SECONDS": (2, 1800),
    "OMBRE_BACKUP_V2_MAX_SOURCE_BYTES": (1, 10 * 1024 * 1024 * 1024),
    "OMBRE_BACKUP_V2_MAX_BUNDLE_BYTES": (1, 10 * 1024 * 1024 * 1024),
    "OMBRE_BACKUP_V2_MINIMUM_FREE_BYTES": (1, 10 * 1024 * 1024 * 1024),
    "OMBRE_BACKUP_V2_READY_TTL_SECONDS": (1, 86_400),
}
_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_POSITIVE_ID = re.compile(r"[1-9][0-9]{0,19}")


class BackupV2RuntimeConfigError(RuntimeError):
    """Stable backup-v2 runtime configuration failure."""

    def __init__(self, code: str = "backup_v2_config_invalid") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BackupV2RegistrationResult:
    enabled: bool
    registered: bool
    route_count: int = 0


def register_backup_v2_if_enabled(
    server_module: Any,
    transport: str,
    *,
    environ: Mapping[str, str] | None = None,
    log: logging.Logger | None = None,
) -> BackupV2RegistrationResult:
    env = os.environ if environ is None else environ
    active_logger = log or logger
    flag = env.get(ENABLE_ENV)
    if flag in (None, "", "false"):
        active_logger.info("backup-v2 registration disabled")
        return BackupV2RegistrationResult(enabled=False, registered=False)
    if flag != "true":
        raise BackupV2RuntimeConfigError()
    if transport != "streamable-http":
        raise BackupV2RuntimeConfigError("backup_v2_transport_unsupported")

    config = _parse_enabled_config(server_module, env)
    _require_single_worker(env)

    from backup_v2_oidc import GitHubActionsBackupV2OidcVerifier
    from offline_backup_bundle import load_backup_workspace, prepare_backup_workspace
    from production_backup_capture import (
        CaptureLimits,
        ProductionBackupCaptureController,
        StrictBackupV2OidcPolicy,
        build_backup_v2_routes,
        parse_public_key_b64,
        public_key_fingerprint,
    )

    public_key = parse_public_key_b64(config["public_key_b64"])
    fingerprint = public_key_fingerprint(public_key)
    if fingerprint != config["recipient_fingerprint"]:
        raise BackupV2RuntimeConfigError("backup_v2_key_invalid")

    workspace_root = Path(config["workspace_root"])
    if workspace_root.exists():
        workspace = load_backup_workspace(workspace_root)
    else:
        workspace = prepare_backup_workspace(workspace_root)

    policy = StrictBackupV2OidcPolicy(
        expected_repository_id=config["repository_id"],
        expected_repository_owner_id=config["repository_owner_id"],
    )
    limits = CaptureLimits(
        freeze_timeout_seconds=config["freeze_timeout_seconds"],
        max_freeze_seconds=config["max_freeze_seconds"],
        max_source_bytes=config["max_source_bytes"],
        max_bundle_bytes=config["max_bundle_bytes"],
        minimum_free_bytes=config["minimum_free_bytes"],
        ready_ttl_seconds=config["ready_ttl_seconds"],
    )
    controller = ProductionBackupCaptureController(
        enabled=True,
        worker_count=1,
        coordinator=getattr(server_module, "bucket_mgr").write_coordinator,
        source_root=config["source_root"],
        workspace_root=workspace.root,
        recipient_public_key=public_key,
        recipient_fingerprint=fingerprint,
        runtime_commit=config["runtime_commit"],
        limits=limits,
        oidc_policy=policy,
    )
    verifier = GitHubActionsBackupV2OidcVerifier()
    routes = build_backup_v2_routes(controller, verifier.verify_request)
    _register_routes_once(server_module.mcp, routes)
    active_logger.info(
        "backup-v2 registration enabled for commit %s fingerprint %s",
        config["runtime_commit"],
        fingerprint,
    )
    return BackupV2RegistrationResult(enabled=True, registered=True, route_count=len(routes))


def _parse_enabled_config(server_module: Any, env: Mapping[str, str]) -> dict[str, Any]:
    missing = [name for name in REQUIRED_ENV if env.get(name) in (None, "")]
    if missing:
        raise BackupV2RuntimeConfigError()
    source_root = Path(str(server_module.config["buckets_dir"])).resolve(strict=True)
    workspace_root = _validate_workspace_root(env["OMBRE_BACKUP_V2_WORKSPACE_ROOT"], source_root)
    freeze_timeout = _parse_bounded_int(
        "OMBRE_BACKUP_V2_FREEZE_TIMEOUT_SECONDS", env
    )
    max_freeze = _parse_bounded_int("OMBRE_BACKUP_V2_MAX_FREEZE_SECONDS", env)
    if freeze_timeout >= max_freeze:
        raise BackupV2RuntimeConfigError()
    repository_id = _parse_repository_id(env["OMBRE_BACKUP_V2_REPOSITORY_ID"])
    owner_id = _parse_repository_id(env["OMBRE_BACKUP_V2_REPOSITORY_OWNER_ID"])
    runtime_commit = env["RENDER_GIT_COMMIT"]
    if _GIT_SHA.fullmatch(runtime_commit) is None:
        raise BackupV2RuntimeConfigError()
    return {
        "public_key_b64": env["OMBRE_BACKUP_V2_PUBLIC_KEY_B64"],
        "recipient_fingerprint": env["OMBRE_BACKUP_V2_RECIPIENT_FINGERPRINT"],
        "repository_id": repository_id,
        "repository_owner_id": owner_id,
        "workspace_root": str(workspace_root),
        "source_root": str(source_root),
        "freeze_timeout_seconds": freeze_timeout,
        "max_freeze_seconds": max_freeze,
        "max_source_bytes": _parse_bounded_int("OMBRE_BACKUP_V2_MAX_SOURCE_BYTES", env),
        "max_bundle_bytes": _parse_bounded_int("OMBRE_BACKUP_V2_MAX_BUNDLE_BYTES", env),
        "minimum_free_bytes": _parse_bounded_int(
            "OMBRE_BACKUP_V2_MINIMUM_FREE_BYTES", env
        ),
        "ready_ttl_seconds": _parse_bounded_int("OMBRE_BACKUP_V2_READY_TTL_SECONDS", env),
        "runtime_commit": runtime_commit,
    }


def _parse_bounded_int(name: str, env: Mapping[str, str]) -> int:
    value = env.get(name, "")
    if not isinstance(value, str) or value != value.strip():
        raise BackupV2RuntimeConfigError()
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise BackupV2RuntimeConfigError()
    parsed = int(value)
    lower, upper = NUMERIC_BOUNDS[name]
    if not lower <= parsed <= upper:
        raise BackupV2RuntimeConfigError()
    return parsed


def _parse_repository_id(value: str) -> str:
    if not isinstance(value, str) or _POSITIVE_ID.fullmatch(value) is None:
        raise BackupV2RuntimeConfigError()
    return value


def _require_single_worker(env: Mapping[str, str]) -> None:
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        value = env.get(name)
        if value in (None, "", "1"):
            continue
        raise BackupV2RuntimeConfigError("backup_v2_multi_worker_unsupported")


def _validate_workspace_root(value: str, source_root: Path) -> Path:
    candidate = Path(value)
    if (
        not candidate.is_absolute()
        or any(part == ".." for part in candidate.parts)
        or any(part in ("", ".") for part in candidate.parts[1:])
    ):
        raise BackupV2RuntimeConfigError()
    workspace_root = candidate.resolve(strict=False)
    if _paths_overlap(
        workspace_root,
        source_root,
        case_sensitive=os.name != "nt",
    ):
        raise BackupV2RuntimeConfigError("backup_v2_workspace_invalid")
    return workspace_root


def _paths_overlap(left: Path, right: Path, *, case_sensitive: bool) -> bool:
    left_path = _comparison_path(left, case_sensitive=case_sensitive)
    right_path = _comparison_path(right, case_sensitive=case_sensitive)
    return _is_equal_or_descendant(left_path, right_path) or _is_equal_or_descendant(
        right_path,
        left_path,
    )


def _comparison_path(path: Path, *, case_sensitive: bool) -> Path:
    if case_sensitive:
        return path
    path_text = str(path).casefold()
    if isinstance(path, PureWindowsPath) or os.name == "nt":
        return PureWindowsPath(path_text)  # type: ignore[return-value]
    return PurePosixPath(path_text)  # type: ignore[return-value]


def _is_equal_or_descendant(candidate: Path, ancestor: Path) -> bool:
    try:
        candidate.relative_to(ancestor)
    except ValueError:
        return False
    return True


def _register_routes_once(mcp: Any, routes: list[Any]) -> None:
    existing = _custom_route_signatures(mcp)
    if V2_ROUTE_SIGNATURES.issubset(existing):
        return
    if existing.intersection(V2_ROUTE_SIGNATURES):
        raise BackupV2RuntimeConfigError("backup_v2_route_conflict")
    for route in routes:
        methods = sorted(method for method in route.methods if method not in {"HEAD", "OPTIONS"})
        for method in methods:
            signature = (method, route.path)
            if signature not in V2_ROUTE_SIGNATURES:
                raise BackupV2RuntimeConfigError("backup_v2_route_conflict")
        decorator = mcp.custom_route(
            route.path,
            methods=methods,
            name=getattr(route, "name", None),
            include_in_schema=False,
        )
        decorator(route.endpoint)


def _custom_route_signatures(mcp: Any) -> set[tuple[str, str]]:
    signatures: set[tuple[str, str]] = set()
    for route in getattr(mcp, "_custom_starlette_routes", ()):
        for method in getattr(route, "methods", ()) or ():
            if method not in {"HEAD", "OPTIONS"}:
                signatures.add((method, route.path))
    return signatures
