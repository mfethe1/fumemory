from __future__ import annotations

import pytest

try:
    import respx
except ImportError:  # pragma: no cover
    respx = None

from memu.notion_bridge import NotionBridge


@pytest.mark.skipif(respx is None, reason="respx not installed")
@pytest.mark.asyncio
async def test_poll_tasks_filters_unclaimed_for_agent() -> None:
    with respx.mock:
        task_board_id = "task-board-id"
        notion_token = "token"
        bridge = NotionBridge(
            notion_token=notion_token,
            memu_base_url="http://api.internal:8000",
            memu_api_key="memu-key",
            task_board_id=task_board_id,
        )

        route = respx.post("https://api.notion.com/v1/databases/task-board-id/query").mock(
            return_value={
                "results": [
                    {
                        "id": "task-1",
                        "properties": {
                            "Title": {"title": [{"plain_text": "Test task"}]},
                            "Status": {"select": {"name": "Backlog"}},
                            "Assigned Agent": {"select": {"name": "Any"}},
                            "Priority": {"select": {"name": "P2"}},
                            "Project": {"rich_text": [{"plain_text": "Demo"}]},
                            "Agent Notes": {"rich_text": []},
                            "Claimed At": {"date": None},
                            "Completed At": {"date": None},
                            "Task ID": {"rich_text": [{"plain_text": "uuid-1"}]},
                        },
                    }
                ]
            }
        )

        result = await bridge.poll_tasks("rosie")

        assert route.called
        assert len(result) == 1
        assert result[0]["title"] == "Test task"
        assert result[0]["project"] == "Demo"
        await bridge.close()


@pytest.mark.skipif(respx is None, reason="respx not installed")
@pytest.mark.asyncio
async def test_complete_task_continues_when_memu_returns_401() -> None:
    with respx.mock:
        bridge = NotionBridge(
            notion_token="token",
            memu_base_url="http://api.internal:8000",
            memu_api_key="wrong-key",
            task_board_id="task-board-id",
        )

        respx.get("https://api.notion.com/v1/pages/task-1").mock(
            return_value={
                "id": "task-1",
                "properties": {
                    "Title": {"title": [{"plain_text": "Patch bug"}]},
                    "Agent Notes": {"rich_text": []},
                },
            }
        )

        respx.patch("https://api.notion.com/v1/pages/task-1").mock(
            return_value={
                "id": "task-1",
                "properties": {
                    "Status": {"select": {"name": "Done"}},
                },
            }
        )
        respx.post("http://api.internal:8000/memories").mock(
            status_code=401,
            return_value={"detail": "invalid key"},
        )

        await bridge.complete_task("task-1", "rosie", "done")
        await bridge.close()


@pytest.mark.skipif(respx is None, reason="respx not installed")
@pytest.mark.asyncio
async def test_claim_task_marks_task_in_progress() -> None:
    with respx.mock:
        bridge = NotionBridge(
            notion_token="token",
            memu_base_url="http://api.internal:8000",
            memu_api_key="memu-key",
            task_board_id="task-board-id",
        )

        route = respx.patch("https://api.notion.com/v1/pages/task-1").mock(
            return_value={"id": "task-1"}
        )

        await bridge.claim_task("task-1", "macklemore")

        assert route.called
        request = route.calls.last.request
        assert request.json()["properties"]["Status"]["select"]["name"] == "In Progress"
        await bridge.close()
