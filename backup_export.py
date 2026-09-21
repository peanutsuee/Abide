"""Read-only, authenticated exports of the Ombre Brain data directory."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jwt
from jwt import PyJWKClient


OIDC_ISSUER = "https://token.actions.githubusercontent.com"
OIDC_JWKS_URL = f"{OIDC_ISSUER}/.well-known/jwks"
OIDC_AUDIENCE = "ombre-brain-backup"
BACKUP_WORKFLOW_PATH = ".github/workflows/backup.yml"
BACKFILL_WORKFLOW_PATH = ".github/workflows/backfill.yml"
ALIAS_CLEAN_WORKFLOW_PATH = ".github/workflows/alias-clean.yml"

_GITHUB_REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$"
)

_jwk_client = PyJWKClient(OIDC_JWKS_URL, cache_keys=True)
_EXCLUDED_NAMES = {
    ".dashboard_auth.json",
    ".backup_state.json",
}


def _category(relative_path: str) -> str:
    first = relative_path.split("/", 1)[0]
    if first in {"permanent", "dynamic", "archive", "feel"}:
        return first
    if relative_path == ".emotion_timeline.json":
        return "emotion"
    return "supporting"


def _serialize_file(path: Path, relative_path: str) -> dict[str, Any]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        content = raw.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        content = base64.b64encode(raw).decode("ascii")
        encoding = "base64"
    return {
        "path": relative_path,
        "category": _category(relative_path),
        "encoding": encoding,
        "sha256": digest,
        "size_bytes": len(raw),
        "content": content,
    }


def build_backup_payload(buckets_dir: str) -> dict[str, Any]:
    """Return a complete, read-only JSON-serializable snapshot."""
    root = Path(buckets_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Buckets directory does not exist: {root}")

    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in _EXCLUDED_NAMES:
            continue
        relative_path = path.relative_to(root).as_posix()
        files.append(_serialize_file(path, relative_path))

    category_counts: dict[str, int] = {}
    for item in files:
        category = item["category"]
        category_counts[category] = category_counts.get(category, 0) + 1

    return {
        "schema_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source": "Ombre Brain",
        "file_count": len(files),
        "category_counts": category_counts,
        "files": files,
    }


def backup_payload_json(buckets_dir: str) -> str:
    return json.dumps(
        build_backup_payload(buckets_dir),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _validate_claims(claims: dict[str, Any], allowed_repository: str) -> None:
    expected_workflow_refs = {
        f"{allowed_repository}/{path}@refs/heads/main"
        for path in (
            BACKUP_WORKFLOW_PATH,
            BACKFILL_WORKFLOW_PATH,
            ALIAS_CLEAN_WORKFLOW_PATH,
        )
    }
    if claims.get("repository") != allowed_repository:
        raise ValueError("Unexpected GitHub repository")
    if claims.get("ref") != "refs/heads/main":
        raise ValueError("Backup workflow must run from main")
    if claims.get("workflow_ref") not in expected_workflow_refs:
        raise ValueError("Unexpected GitHub workflow")
    if claims.get("event_name") not in {"schedule", "workflow_dispatch"}:
        raise ValueError("Unsupported GitHub Actions event")


def configured_backup_repository() -> str:
    """Return the explicit GitHub Actions backup authority, or fail closed."""

    repository = os.environ.get("OMBRE_BACKUP_REPOSITORY", "").strip()
    if not repository:
        raise ValueError("GitHub backup authority is not configured")
    if _GITHUB_REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError("GitHub backup repository is invalid")
    return repository


def _decode_and_validate(token: str, allowed_repository: str) -> dict[str, Any]:
    signing_key = _jwk_client.get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=OIDC_AUDIENCE,
        issuer=OIDC_ISSUER,
    )
    _validate_claims(claims, allowed_repository)
    return claims


async def verify_github_oidc(token: str) -> dict[str, Any]:
    """Verify an explicitly configured GitHub Actions backup workflow."""
    allowed_repository = configured_backup_repository()
    if not token:
        raise ValueError("Missing bearer token")
    return await asyncio.to_thread(
        _decode_and_validate,
        token,
        allowed_repository,
    )
