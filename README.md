# Abide / 长相守

Abide（中文名：**长相守**）是一个 MCP-first、local-first 的长期记忆系统。它把可持续的记忆保存为可检查的 bucket，并让 MCP 客户端能够在明确的隐私、来源、生命周期和变更边界内写入、检索、演化、关联、保护和归档这些记忆。

它既可以作为无界面的 stdio MCP 服务运行，也可以按需启用 Dashboard、语义 embedding、导入工作流与 Remember-Me 资产集成。核心不依赖某一种模型客户端、Web UI 或消息桥接。

> 实际 MCP 名称、参数与枚举以随发行版附带的 machine-readable schema 为准；操作规则见 [Abide guide](docs/ABIDE_GUIDE.md)。

## What Abide is for

Abide 面向需要长期、可追溯记忆的 MCP/LLM 工作流：不是把一段聊天摘要反复覆盖，而是让记忆在保留来源和状态的前提下逐步沉淀。bucket 可作为事实、项目上下文、关系、偏好、使用者留言或会话连续性的容器；它们是普通可检查的数据，而非不可见的模型状态。

Abide 不是 Telegram bot、私人陪伴人格、托管记忆 SaaS，也不包含任何特定消息桥或 Lan-Brain 实现。

## Core capabilities

- **Bucket memory and lifecycle** — durable bucket storage, activity/decay, dormant handling, archive/session handling, and explicit recall controls.
- **Memory safety** — sealed memory is hidden by default; destructive `trace` operations use short-lived, one-shot confirmation; `delete` has no MCP undo.
- **Truth and provenance** — supersession, provenance-aware metadata, todo provenance, source-aware summaries, and a distinction between original material, summary, inference, and system output.
- **Recall with boundaries** — `breath` supports query recall, filters, `touch=False`, historical `as_of` reads, cursors, and explicit truncation accounting.
- **Continuity tools** — `boot` profiles, notes, mailbox/letters, triggers, `dream`, `pulse`, `hold`, `grow`, `trace`, `archive_session`, and related-memory maintenance.
- **MCP-first operation** — local stdio is the default transport. Current discovery exposes 26 default tools; the diagnostic profile raises the current total to 41. Treat those counts as version-specific, not a compatibility promise.

## Design and operating model

The ordinary loop is intentionally small:

```text
boot → retrieve with breath → hold durable facts → evolve with grow/trace → inspect lifecycle with pulse/dream
```

`hold` creates a durable memory; `breath` retrieves relevant material; `grow` and `trace` make controlled changes; `superseded_by` prevents replaced material from being presented as current truth. Sensitive content can be sealed. Source and todo attribution are first-class metadata rather than inferred after the fact.

The guide documents the exact safety contract, including sealed visibility, historical recall, destructive confirmation, provenance, cursors, truncation, profile delta, and intentionally unsupported behavior.

## Surfaces and profiles

### MCP core

Abide is usable headlessly through stdio MCP. No Dashboard, Remember-Me package, embedding provider, public HTTP endpoint, or channel bridge is required for the basic local memory loop.

`talk` and `code` are core boot profiles. `tg` is an optional compact channel profile; it is a memory-profile name, not a claim that Abide ships a Telegram transport. The private Telegram bridge and Lan-Brain are outside this repository.

### Dashboard — optional

The Dashboard is an optional authenticated visual/operator surface for bucket and archive navigation, reference links, history-oriented detail views, search, import/settings routes, and optional asset views. It is not required for Minimal Mode or stdio MCP.

### Optional and experimental components

| Area | Status | Boundary |
| --- | --- | --- |
| Semantic embeddings, import workflow, conflict warnings, ordinary portable export | Optional public capability | Require intentional configuration and may use an external provider. |
| Remember-Me asset/image integration | Optional public capability | Separate external dependency; not vendored and not required by core. |
| `tg` pinned-summary refresh | Experimental channel capability | Keep stale/fresh/missing semantics explicit; no Telegram transport is included. |
| Diagnostics and Stage-0 asset probes | Experimental/developer profile | Not ordinary client workflow. |

## Remember-Me integration

Remember-Me is an **optional external runtime dependency/integration**. Abide does not vendor its source. Install it only when enabling the asset/image feature:

```text
pip install -r requirements-remember-me.txt
```

Current compatibility applies **only** to this optional integration:

- Claude: **SUPPORTED / TESTED**
- ChatGPT: **NOT CURRENTLY SUPPORTED** — a prior real connection attempt encountered an undiagnosed compatibility problem; no ChatGPT compatibility is claimed.
- Other MCP/LLM clients: **UNVERIFIED**

This does not limit Abide core MCP support to Claude.

## Minimal and fuller setups

| Mode | Includes | Does not require |
| --- | --- | --- |
| Minimal | Local bucket storage, stdio MCP, core memory lifecycle and recall | Remember-Me, embedding API, Dashboard, HTTP, Telegram bridge, private data, or API keys |
| Fuller setup | Selected optional components such as embeddings, Dashboard, import, conflict warnings, deployment support, and optional Remember-Me assets | A requirement to enable every optional component |

The currently verified clean-install Minimal path used a fresh WSL Python 3.12.14 virtual environment, core `requirements.txt`, no `.env`, no API key, no Remember-Me package, an empty external data directory, stdio MCP, a synthetic `hold`, retrieval, and restart persistence. A local outbound socket guard recorded zero runtime connection attempts. This is a verified path, not a claim of clean-install validation on every OS or Python version.

## Installation

Read the full [installation guide](docs/INSTALLATION.md) before enabling optional components. The short path is:

```text
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export OMBRE_BUCKETS_DIR=/path/to/abide-data
export OMBRE_TRANSPORT=stdio
.venv/bin/python server.py
```

For a portable client configuration, point the MCP client at the virtualenv interpreter and `server.py`, and provide `OMBRE_BUCKETS_DIR`. `stdio` is currently the default; setting it explicitly makes the chosen boundary visible.

Minimal Mode has zero mandatory environment variables. Setting `OMBRE_BUCKETS_DIR` outside the checkout is nevertheless strongly recommended because the current fallback is `./buckets`.

## Configuration and first-run behavior

Start from [`.env.example`](.env.example). Keep secrets in local environment/configuration, never in source control.

- Without a provider key, `hold` still writes the memory. Auto-tagging may warn and fall back to a default category; that warning is not a failed write.
- To retrieve stored body text with fidelity, use `breath(mode="full")`. Summary mode does not promise to reproduce the full stored body.
- The first write starts decay and currently initializes runtime/asset-related storage scaffolding, including local SQLite support files. With `OMBRE_BUCKETS_DIR` set externally, these artifacts remain outside the checkout; storage initialization alone does not mean optional assets or embeddings are active.

## HTTP and deployment

HTTP is intentionally absent from the shortest Quick Start. Current HTTP/deployment behavior is advanced and has known release debts: the HTTP bind is currently `0.0.0.0`, and Uvicorn is not declared as a direct core dependency. Do not treat HTTP Full Mode as clean-install validated.

If intentionally deploying HTTP, configure authentication and CORS explicitly. MCP HTTP is deny-by-default when no auth token is configured, unless an explicit local anonymous opt-in is used; query-token compatibility is exceptional and should use a distinct token.

## Development and testing

Use the project’s documented Python environment and test procedures for development. The full test suite is run in WSL for this codebase. Do not use private runtime data, deployment configuration, or historical source Git metadata as test inputs.

## License, provenance, and acknowledgements

- Abide-owned new and modified Covered Code: [CPAL-1.0](LICENSE)
- Required upstream MIT attribution and release notice: [NOTICE](NOTICE.md)
- Bundled third-party code notices: [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)
- Design and research acknowledgements: [ACKNOWLEDGEMENTS](ACKNOWLEDGEMENTS.md)

Abide retains substantial lineage from [P0luz/Ombre-Brain](https://github.com/P0luz/Ombre-Brain). Applicable upstream portions retain their MIT copyright and permission notice, including `Copyright (c) 2026 P0lar1zzZ`. Haven-Ombre is acknowledged as design provenance, not code lineage.
