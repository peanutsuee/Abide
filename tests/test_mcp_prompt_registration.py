import pytest

import server
from mcp.shared.memory import create_connected_server_and_client_session


@pytest.mark.asyncio
async def test_start_ombre_brain_prompt_is_zero_argument_and_static():
    async with create_connected_server_and_client_session(server.mcp) as client:
        prompts = (await client.list_prompts()).prompts
        assert [prompt.name for prompt in prompts] == ["start_ombre_brain"]
        assert prompts[0].arguments in (None, [])
        result = await client.get_prompt("start_ombre_brain")

    assert result.description == (
        "Optional onboarding guidance for starting or resuming an Ombre Brain session."
    )
    assert len(result.messages) == 1
    message = result.messages[0]
    assert message.role == "user"
    assert message.content.type == "text"
    text = message.content.text
    assert "boot()" in text
    assert "breath(query=\"...\")" in text
    assert "dream()" in text
    assert "optional" in text.lower()
    assert "recommended" in text.lower()
    assert "no exceptions" not in text.lower()
    assert "must call breath first" not in text.lower()
    assert "D:\\" not in text
    assert "C:\\" not in text
    assert "OMBRE_AUTH_TOKEN" not in text


@pytest.mark.asyncio
async def test_prompt_does_not_invoke_tools_or_depend_on_server_state(monkeypatch):
    async def fail(*args, **kwargs):
        raise AssertionError("prompt invoked a tool")

    for name in ("boot", "breath", "dream", "hold", "grow", "trace"):
        monkeypatch.setattr(server, name, fail)
    monkeypatch.setattr(server, "bucket_mgr", None)

    async with create_connected_server_and_client_session(server.mcp) as client:
        result = await client.get_prompt("start_ombre_brain")

    assert result.messages[0].content.type == "text"
