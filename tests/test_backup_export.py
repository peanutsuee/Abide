import base64
import hashlib
import json
from pathlib import Path

import pytest

from backup_export import (
    _validate_claims,
    backup_payload_json,
    configured_backup_repository,
    verify_github_oidc,
)


def _write(root: Path, relative_path: str, content: bytes) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_backup_contains_all_memory_categories_and_emotion_data(tmp_path):
    root = tmp_path / "buckets"
    _write(root, "permanent/core.md", b"core")
    _write(root, "dynamic/day.md", b"dynamic")
    _write(root, "feel/feeling.md", b"feel")
    _write(root, "archive/session.md", b"archive")
    _write(root, ".emotion_timeline.json", b'[{"valence":0.7}]')
    _write(root, "embeddings.db", b"\x00\xffindex")
    _write(root, ".dashboard_auth.json", b'{"password_hash":"secret"}')

    payload = json.loads(backup_payload_json(str(root)))
    exported = {item["path"]: item for item in payload["files"]}

    assert payload["schema_version"] == 1
    assert set(exported) == {
        "permanent/core.md",
        "dynamic/day.md",
        "feel/feeling.md",
        "archive/session.md",
        ".emotion_timeline.json",
        "embeddings.db",
    }
    assert exported[".emotion_timeline.json"]["category"] == "emotion"
    assert exported["archive/session.md"]["category"] == "archive"
    assert exported["embeddings.db"]["encoding"] == "base64"
    assert base64.b64decode(exported["embeddings.db"]["content"]) == b"\x00\xffindex"
    assert ".dashboard_auth.json" not in exported


def test_backup_records_file_hashes(tmp_path):
    root = tmp_path / "buckets"
    content = b"production memory"
    _write(root, "dynamic/item.md", content)

    payload = json.loads(backup_payload_json(str(root)))

    assert payload["files"][0]["sha256"] == hashlib.sha256(content).hexdigest()


def test_oidc_claims_only_allow_explicit_synthetic_backup_workflow(monkeypatch):
    repository = "example-owner/example-backup"
    monkeypatch.setenv("OMBRE_BACKUP_REPOSITORY", repository)
    assert configured_backup_repository() == repository
    claims = {
        "repository": repository,
        "ref": "refs/heads/main",
        "workflow_ref": (
            f"{repository}/.github/workflows/backup.yml@refs/heads/main"
        ),
        "event_name": "workflow_dispatch",
    }

    _validate_claims(claims, repository)
    _validate_claims(
        {
            **claims,
            "workflow_ref": (
                f"{repository}/.github/workflows/"
                "backfill.yml@refs/heads/main"
            ),
        },
        repository,
    )
    _validate_claims(
        {
            **claims,
            "workflow_ref": (
                f"{repository}/.github/workflows/"
                "alias-clean.yml@refs/heads/main"
            ),
        },
        repository,
    )

    for key, invalid_value in [
        ("repository", "someone/else"),
        ("ref", "refs/heads/feature"),
        (
            "workflow_ref",
            f"{repository}/.github/workflows/other.yml@refs/heads/main",
        ),
        ("event_name", "pull_request"),
    ]:
        invalid_claims = {**claims, key: invalid_value}
        with pytest.raises(ValueError):
            _validate_claims(invalid_claims, repository)


def test_github_backup_authority_is_optional_and_fails_closed(monkeypatch):
    monkeypatch.delenv("OMBRE_BACKUP_REPOSITORY", raising=False)
    with pytest.raises(ValueError, match="not configured"):
        configured_backup_repository()

    monkeypatch.setenv("OMBRE_BACKUP_REPOSITORY", "not a repository")
    with pytest.raises(ValueError, match="repository is invalid"):
        configured_backup_repository()


@pytest.mark.asyncio
async def test_github_oidc_without_authority_fails_before_token_validation(monkeypatch):
    monkeypatch.delenv("OMBRE_BACKUP_REPOSITORY", raising=False)
    with pytest.raises(ValueError, match="not configured"):
        await verify_github_oidc("synthetic-token")
