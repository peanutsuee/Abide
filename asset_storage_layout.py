"""Owned-subpath validation for the accepted Design A storage layout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class AssetStorageLayoutError(ValueError):
    """Stable path-ownership validation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# These are names proven by the released code to be rooted directly below the
# configured legacy bucket root.  The validator protects them from becoming
# RM or state descendants.  ``remember-me`` and ``state`` are reserved for the
# two new namespaces and therefore are checked separately.
LEGACY_OWNED_ROOT_NAMES = frozenset(
    {
        "permanent",
        "dynamic",
        "archive",
        "feel",
        "assets",
        "assets.sqlite3",
        "bucket_history.sqlite3",
        "embeddings.db",
        "dehydration_cache.db",
        ".dashboard_auth.json",
        ".emotion_timeline.json",
        "import_state.json",
    }
)
RM_NAMESPACE_NAME = "remember-me"
STATE_NAMESPACE_NAME = "state"


@dataclass(frozen=True)
class AssetStorageLayout:
    """Canonical roots after owned-subpath validation."""

    legacy_root: Path
    rm_root: Path
    state_root: Path

    @property
    def rm_assets_root(self) -> Path:
        return self.rm_root / "assets"

    @property
    def state_db_path(self) -> Path:
        return self.state_root / "migration.sqlite3"


def validate_asset_storage_layout(
    legacy_root: str | Path,
    rm_root: str | Path,
    state_root: str | Path,
) -> AssetStorageLayout:
    """Validate Design A without requiring the roots to exist.

    A descendant of the broad legacy bucket root is allowed only when it is
    exactly the dedicated ``remember-me`` or ``state`` namespace.  This is
    intentionally narrower than blanket root overlap rejection while still
    protecting every legacy-owned path proven by the released code.
    """

    legacy = _canonical_absolute(legacy_root, "legacy_root")
    rm = _canonical_absolute(rm_root, "rm_root")
    state = _canonical_absolute(state_root, "state_root")

    for name, path in (("legacy_root", legacy), ("rm_root", rm), ("state_root", state)):
        if _contains_symlink_component(path):
            raise AssetStorageLayoutError(f"{name}_symlink_unsupported")

    if _same_or_within(rm, legacy):
        relative = _relative_parts(rm, legacy)
        if relative != (RM_NAMESPACE_NAME,):
            raise AssetStorageLayoutError("rm_root_legacy_namespace_collision")
    elif _same_or_within(legacy, rm):
        raise AssetStorageLayoutError("rm_root_legacy_ancestor_collision")

    if _same_or_within(state, legacy):
        relative = _relative_parts(state, legacy)
        if relative != (STATE_NAMESPACE_NAME,):
            raise AssetStorageLayoutError("state_root_legacy_namespace_collision")
    elif _same_or_within(legacy, state):
        raise AssetStorageLayoutError("state_root_legacy_ancestor_collision")

    if _same_or_within(rm, state) or _same_or_within(state, rm):
        raise AssetStorageLayoutError("state_rm_overlap")

    for reserved in LEGACY_OWNED_ROOT_NAMES:
        owned_path = legacy / reserved
        if _same_or_within(rm, owned_path) or _same_or_within(owned_path, rm):
            raise AssetStorageLayoutError("rm_root_legacy_owned_path_collision")
        if _same_or_within(state, owned_path) or _same_or_within(owned_path, state):
            raise AssetStorageLayoutError("state_root_legacy_owned_path_collision")

    return AssetStorageLayout(
        legacy_root=legacy,
        rm_root=rm,
        state_root=state,
    )


def _canonical_absolute(value: str | Path, label: str) -> Path:
    if isinstance(value, bool):
        raise AssetStorageLayoutError(f"{label}_invalid")
    try:
        candidate = Path(value).expanduser()
    except (TypeError, ValueError, OSError) as exc:
        raise AssetStorageLayoutError(f"{label}_invalid") from exc
    if not candidate.is_absolute():
        raise AssetStorageLayoutError(f"{label}_not_absolute")
    if _contains_symlink_component(candidate):
        raise AssetStorageLayoutError(f"{label}_symlink_unsupported")
    try:
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise AssetStorageLayoutError(f"{label}_unresolvable") from exc


def _same_or_within(candidate: Path, ancestor: Path) -> bool:
    try:
        candidate.relative_to(ancestor)
        return True
    except ValueError:
        return False


def _relative_parts(candidate: Path, ancestor: Path) -> tuple[str, ...]:
    try:
        return tuple(candidate.relative_to(ancestor).parts)
    except ValueError:
        return ()


def _contains_symlink_component(path: Path) -> bool:
    """Reject ordinary symlink components where pathlib can observe them."""

    current = path.anchor and Path(path.anchor) or Path()
    for part in path.parts[1:] if path.anchor else path.parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError as exc:
            raise AssetStorageLayoutError("path_inspection_failed") from exc
    return False


__all__ = [
    "AssetStorageLayout",
    "AssetStorageLayoutError",
    "LEGACY_OWNED_ROOT_NAMES",
    "RM_NAMESPACE_NAME",
    "STATE_NAMESPACE_NAME",
    "validate_asset_storage_layout",
]
