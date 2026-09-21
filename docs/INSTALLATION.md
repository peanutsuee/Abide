# Install and first run Abide

This guide describes the currently verified **Minimal Mode** path: a local stdio MCP memory server with an empty external data directory. It does not configure Dashboard, HTTP, embeddings, Remember-Me, Telegram transport, or any provider key.

## Verified boundary

The clean-install smoke test was performed in a fresh WSL Python 3.12.14 virtual environment with only `requirements.txt`, no `.env`, no API key, no Remember-Me package, and an empty external bucket directory. It completed MCP initialization, tool discovery, a synthetic `hold`, `breath(mode="full")` retrieval, and restart persistence. A local socket-connect guard recorded no runtime connections.

This verifies that path only. It does not claim clean-install validation for Windows, macOS, every Python version, HTTP deployment, Dashboard, or every MCP client.

## Minimal Mode

### 1. Create a virtual environment

From the repository root:

```text
python3.12 -m venv .venv
```

On POSIX/WSL, use the interpreter directly:

```text
.venv/bin/python -m pip install -r requirements.txt
```

On Windows, use the equivalent `.venv\Scripts\python.exe` invocation. That platform path has not been part of the current clean-install smoke validation.

Core installation does not install Remember-Me.

### 2. Choose a data directory outside the checkout

Minimal Mode has **zero mandatory environment variables**, but explicitly set a data directory outside the repository:

```text
# POSIX/WSL example
export OMBRE_BUCKETS_DIR=/path/to/abide-data

# Optional: stdio is already the current default, but declaring it is clear.
export OMBRE_TRANSPORT=stdio
```

Use a new, writable directory. The first write creates its bucket folders and local SQLite support files automatically. If `OMBRE_BUCKETS_DIR` is omitted, the current fallback remains `./buckets` inside the checkout.

### 3. Start stdio MCP

```text
.venv/bin/python server.py
```

This process speaks MCP over stdin/stdout; it is not an interactive terminal UI. Configure an MCP client to launch the same interpreter and script. A portable shape is:

```json
{
  "mcpServers": {
    "abide": {
      "command": "/path/to/Abide/.venv/bin/python",
      "args": ["/path/to/Abide/server.py"],
      "env": {
        "OMBRE_BUCKETS_DIR": "/path/to/abide-data",
        "OMBRE_TRANSPORT": "stdio"
      }
    }
  }
}
```

Client configuration formats vary. The example is structural, not a claim that every client accepts this exact JSON.

### 4. Verify the memory loop

Using the MCP client, write only synthetic test material:

```text
hold(content="Abide first-run test memory", tags="project/abide", importance=5)
breath(query="first-run test memory", touch=False, mode="full")
```

The second call should return the test memory. Restarting the server should preserve it in the chosen data directory.

## Expected Minimal Mode behavior

### No provider key

`hold` can succeed without an API key. Auto-tagging may emit a warning and use fallback/default metadata. Treat this as a degraded analysis feature, not as a failed write.

Long `grow`, provider-backed dehydration/merge paths, configured conflict detection, semantic retrieval, and imports may need additional configuration. Do not assume they are offline-only operations.

### Retrieval modes

Use `breath(mode="full")` when verifying or consuming stored body text. Summary mode is designed for compact context and does not guarantee that it reproduces the entire original body.

### First-write initialization

The first write starts the decay engine and currently initializes local runtime/asset-related scaffolding. It can create asset, history, dehydration, embedding, and related SQLite/runtime structures. With an external `OMBRE_BUCKETS_DIR`, these stay outside the source tree. Their existence does not enable Remember-Me or embeddings by itself.

## Optional Remember-Me integration

Install it only when intentionally enabling asset/image integration:

```text
.venv/bin/python -m pip install -r requirements-remember-me.txt
```

Do not manually copy the pinned dependency URL into documentation; the requirements file is authoritative.

Compatibility applies only to this optional integration:

- Claude: **SUPPORTED / TESTED**
- ChatGPT: **NOT CURRENTLY SUPPORTED** — an earlier real connection encountered an undiagnosed compatibility problem.
- Other MCP/LLM clients: **UNVERIFIED**

Abide core is not limited to Claude by this integration boundary.

## Optional components

| Component | What it adds | Extra boundary |
| --- | --- | --- |
| Embeddings | semantic recall, related-memory support | provider configuration and local embedding index |
| Dashboard | authenticated browser/operator UI | intentional HTTP deployment and Dashboard authentication |
| Import | operator-initiated import/review workflows | private-source review and, for extraction, provider configuration |
| Remember-Me | asset/image integration and viewer | optional requirements file, explicit runtime enablement, separate data root |
| Diagnostics | developer/acceptance tools | `OMBRE_DIAG_TOOLS=true`; not ordinary client workflow |

## HTTP is advanced and currently limited

Do not use HTTP as the Minimal Mode quick start. Current known release debts are:

- HTTP bind currently uses `0.0.0.0`.
- Uvicorn is not declared as a direct core dependency.

HTTP Full Mode is therefore not clean-install validated. If intentionally using it, configure authentication and CORS deliberately; never expose a memory service publicly merely because it starts.

## Configuration reference

See [`.env.example`](../.env.example). Keep secrets and data paths local. Do not commit `.env` files, bucket data, SQLite databases, logs, exports, uploads, or generated asset data.

## Troubleshooting

| Symptom | Meaning / next action |
| --- | --- |
| `hold` warns that auto-tagging failed | Expected without provider configuration; check that the write response still reports creation. |
| No full body in recall | Retry with `breath(mode="full")`. |
| Runtime files appear under the repository | Set `OMBRE_BUCKETS_DIR` to an external writable directory before starting. |
| Remember-Me optional dependency error | Install `requirements-remember-me.txt` only if intentionally enabling the integration. |
| HTTP client cannot connect | HTTP is not part of this minimal path; review authentication/CORS and current deployment limitations before enabling it. |
