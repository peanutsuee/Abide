import importlib
import sys
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_todos_groups_only_unresolved_bucket_items(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    server.bucket_mgr.list_all = AsyncMock(
        return_value=[
            {
                "id": "high",
                "metadata": {
                    "name": "High priority",
                    "importance": 9,
                    "resolved": False,
                    "todos": ["call back", "send file"],
                },
                "content": "",
            },
            {
                "id": "resolved",
                "metadata": {
                    "name": "Finished",
                    "importance": 10,
                    "resolved": True,
                    "todos": ["must not appear"],
                },
                "content": "",
            },
            {
                "id": "low",
                "metadata": {
                    "name": "Low priority",
                    "importance": 2,
                    "todos": "- buy tea\n- write note",
                },
                "content": "",
            },
        ]
    )

    result = await server.todos()

    assert "High priority" in result
    assert "call back" in result
    assert "Low priority" in result
    assert "buy tea" in result
    assert "Finished" not in result
    assert result.index("High priority") < result.index("Low priority")
    server.bucket_mgr.list_all.assert_awaited_once_with(include_archive=True)
