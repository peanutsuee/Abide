"""Injection boundary for the future shared asset mutation gate."""

from __future__ import annotations

from asset_cutover_state import (
    CutoverSnapshot,
    CutoverStateError,
    CutoverStateStore,
    FreezeLease,
    MutationCapability,
)


class AssetMutationGate:
    """Small adapter that later AssetStore and RM Core can receive.

    Persistence remains owned by ``CutoverStateStore``.  This class contains
    no independent process-local truth and therefore cannot accidentally
    declare a stale freeze open after restart.
    """

    def __init__(self, state_store: CutoverStateStore) -> None:
        if not isinstance(state_store, CutoverStateStore):
            raise TypeError("state_store_invalid")
        self.state_store = state_store

    def inspect(self) -> CutoverSnapshot:
        return self.state_store.get_snapshot()

    def assert_public_mutation_allowed(self) -> None:
        self.state_store.assert_public_mutation_allowed()

    def issue_migration_capability(
        self,
        lease: FreezeLease,
        *,
        purpose: str,
    ) -> MutationCapability:
        return self.state_store.issue_privileged_capability(
            lease,
            purpose=purpose,
        )

    def assert_migration_capability(
        self,
        capability: MutationCapability,
        *,
        purpose: str,
    ) -> None:
        self.state_store.assert_privileged_capability(
            capability,
            purpose=purpose,
        )

    def public_mutations_allowed(self) -> bool:
        try:
            self.assert_public_mutation_allowed()
        except CutoverStateError:
            return False
        return True


__all__ = ["AssetMutationGate"]
