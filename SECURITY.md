# Security policy for Abide

## Scope

Abide stores durable memory and may be configured with provider credentials, HTTP access, optional Dashboard authentication, and optional Remember-Me asset integration. Treat its data directory and configuration as sensitive local state.

## Secrets and local configuration

- Keep API keys, MCP tokens, Dashboard credentials, hook tokens, response seals, and provider endpoints in local environment/configuration.
- Never commit `.env` files, credential-bearing configuration, tokens, or secret-bearing logs.
- Use placeholders in examples and rotate a credential if it is ever committed or disclosed.

## Memory and runtime data

- Set `OMBRE_BUCKETS_DIR` to a writable directory outside the checkout. The current fallback is `./buckets`, so explicit configuration avoids accidental source-tree runtime data.
- Do not commit bucket directories, SQLite databases, archives, uploads, generated media, caches, logs, backups, or exported private memory.
- A first write may create local asset/history/dehydration/embedding runtime structures even when optional features are not actively configured.

## Sealed-memory expectations

Sealed content is hidden by default and must not be inferred through ordinary recall, diagnostics, or existence side channels. Enable an include-sealed path only where the documented contract explicitly permits it and where the access context is authorized. If a normal result unexpectedly exposes apparently sealed material, stop expanding or citing it and investigate the deployment boundary.

## HTTP and Dashboard exposure

The shortest supported path is local stdio MCP. Do not expose a memory service to a network merely because it starts.

- Configure HTTP authentication and CORS explicitly before intentional network deployment.
- Do not use anonymous HTTP opt-in outside a deliberate local/controlled scenario.
- Keep query-token compatibility exceptional and use a separate token when it is unavoidable.
- Dashboard is optional; use its authentication/setup boundary only in a deliberately configured deployment.
- Current HTTP Full Mode has known release debts and is not clean-install validated: the bind address is currently `0.0.0.0`, and Uvicorn is not declared as a direct core dependency.

## Optional Remember-Me integration

Remember-Me is separately installed and separately licensed. Do not enable it without a distinct data root and a deliberate review of its deployment and attribution obligations. The optional integration's client compatibility status must not be treated as an Abide-core client restriction.

## Internal operator-only Raw Evidence boundary

Raw Evidence source is included for advanced/internal operator use, not as an end-user feature. It is disabled by default, is outside Minimal Mode and ordinary MCP workflows, and has no ordinary retrieval or model-context surface. Do not configure or capture it through end-user examples. An operator who deliberately enables it must use an isolated absolute evidence root and understand that its capture path uses the existing import workflow; configured provider-backed extraction therefore applies to imported chunks. This internal API may change.

## Advanced GitHub backup authority

GitHub Actions backup/export is optional advanced operator functionality. It is unavailable unless `OMBRE_BACKUP_REPOSITORY` is explicitly configured as the authorized `owner/repository`; missing or malformed configuration fails closed. The OIDC verifier still requires the configured repository, the expected workflow path, the `main` ref, and an allowed event. Do not put an access token, private repository name, or workflow credentials in the repository or in end-user examples.

## Reporting vulnerabilities

For a future public repository, use GitHub Security Advisories or the repository’s security reporting mechanism if enabled. Do not post credential material, private memory, or exploit details in public issues. This release candidate does not claim a separate private reporting channel.
