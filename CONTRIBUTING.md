# Contributing to Snows of Yesteryear

Thank you for considering a contribution.

## Before opening a change

- Read the current MCP schema and [Snows of Yesteryear guide](docs/SNOWS_OF_YESTERYEAR_GUIDE.md); behavior and safety boundaries are compatibility-sensitive.
- Keep changes focused. Do not mix product refactors, generated data, dependency upgrades, or documentation rewrites into an unrelated fix.
- Do not add memory databases, `.env` files, credentials, private conversations, private deployment configuration, or generated runtime artifacts.
- Preserve sealed-data boundaries, provenance distinctions, destructive confirmation, and read-only behavior such as `touch=False` unless a change explicitly revises the public contract.

## Tests and documentation

- Add or update focused tests for behavior changes.
- Run the documented test suite in the supported WSL development environment before proposing a change.
- Update public documentation and notices whenever a public tool, parameter, bundled dependency, or license-relevant artifact changes.
- Re-audit bundled asset-viewer notices whenever its source, lockfile, or build output changes.

## Licensing and provenance

Contributions to project-owned Covered Code are expected to be compatible with the repository’s CPAL-1.0 licensing and existing third-party notices. Do not copy code or assets without recording the applicable provenance and license obligations.

This document intentionally does not promise review timing, a release cadence, or a maintainer hierarchy.
