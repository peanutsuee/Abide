# Remember-Me integration

Remember-Me is an optional external runtime integration. It is not vendored into Snows of Yesteryear and is not required for Minimal Mode.

Install it only when enabling the asset/image integration:

```text
pip install -r requirements-remember-me.txt
```

The optional requirements file pins the public `peanutsuee/Remember-Me` release asset `v0.1.0-dev.7-public.1` at source commit `a00ea991442d7581a3856b178525a8e77da833fe`, Git tree `a958d995421c97ccc572b127cb859797aa7a415f`, and its audited SHA-256. Do not replace that pin with a branch, editable checkout, vendored copy, or an unverified archive.

The integration remains disabled unless explicitly enabled by its documented runtime setting and given its own data root. Its client compatibility status applies only to this optional integration: Claude is supported/tested; ChatGPT is not currently supported because a prior compatibility issue remains undiagnosed; other MCP/LLM clients are unverified.

Remember-Me is separately licensed and governed by its own notices. See Snows of Yesteryear's `NOTICE.md` and `THIRD_PARTY_NOTICES.md` for distribution attribution.
