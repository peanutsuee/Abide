"""Static, optional MCP prompt guidance for Ombre Brain clients."""

from __future__ import annotations

from typing import Any, Callable


START_OMBRE_BRAIN_DESCRIPTION = (
    "Optional onboarding guidance for starting or resuming an Ombre Brain session."
)

START_OMBRE_BRAIN_PROMPT = """Ombre Brain provides persistent episodic and contextual memory through MCP tools. Use this as optional onboarding when starting or resuming a conversation:

- When startup context is useful, `boot()` is the recommended one-shot context.
- When the current conversation needs specific past context, use `breath(query=\"...\")` for targeted retrieval.
- `dream()` is optional reflection/digestion; use it when reflection is useful, not as a ritual. Feel retrieval is also optional when prior reflections would help.
- Do not persist ordinary chatter or duplicate memory merely because tools exist. Use mutating tools such as `hold()` and `grow()` intentionally; `trace()` can perform destructive mutations such as deletion, so use those modes only when explicitly appropriate. Maintenance tools such as `digest()` and `related_backfill()` are for controlled maintenance, and diagnostic `asset_*` tools are not part of the normal Remember-Me workflow.
- Clients without MCP prompt support can use the same tools directly; prompt support is not required for normal Ombre Brain use.
"""


def start_ombre_brain() -> str:
    """Return deterministic onboarding guidance without reading or writing state."""
    return START_OMBRE_BRAIN_PROMPT


def register_prompts(mcp: Any) -> Callable[[], str]:
    """Register the one public prompt on the repository's FastMCP instance."""
    return mcp.prompt(
        name="start_ombre_brain",
        description=START_OMBRE_BRAIN_DESCRIPTION,
    )(start_ombre_brain)
