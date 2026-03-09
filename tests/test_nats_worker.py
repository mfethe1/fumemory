import pytest
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from memu.nats_worker import process_msg

@pytest.mark.asyncio
async def test_process_msg_started():
    # Mock NotionBridge
    bridge = AsyncMock()
    
    # Mock NATS Message
    msg = AsyncMock()
    msg.subject = "swarm.task.started"
    msg.data = json.dumps({
        "task_id": "task-123",
        "agent_id": "winnie"
    }).encode()
    
    # Run processor
    await process_msg(msg, bridge)
    
    # Assert bridge called
    bridge.claim_task.assert_called_once_with("task-123", "winnie")
    msg.ack.assert_called_once()

@pytest.mark.asyncio
async def test_process_msg_completed():
    bridge = AsyncMock()
    msg = AsyncMock()
    msg.subject = "swarm.task.completed"
    msg.data = json.dumps({
        "task_id": "task-456",
        "agent_id": "winnie",
        "notes": "Done!"
    }).encode()
    
    await process_msg(msg, bridge)
    
    bridge.complete_task.assert_called_once_with("task-456", "winnie", "Done!")
    msg.ack.assert_called_once()

@pytest.mark.asyncio
async def test_process_msg_failed():
    bridge = AsyncMock()
    msg = AsyncMock()
    msg.subject = "swarm.task.failed"
    msg.data = json.dumps({
        "task_id": "task-789",
        "agent_id": "winnie",
        "error": "Timeout"
    }).encode()
    
    await process_msg(msg, bridge)
    
    bridge.block_task.assert_called_once_with("task-789", "winnie", "Timeout")
    msg.ack.assert_called_once()
