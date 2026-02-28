import os
import asyncio
from memu.notion_bridge import NotionBridge

async def smoke_test():
    api_key = os.environ.get("NOTION_API_KEY")
    task_board_id = os.environ.get("NOTION_TASK_BOARD_ID")
    
    if not api_key:
        print("❌ NOTION_API_KEY missing")
        return

    # Initialize bridge (memu_base_url is dummy for this test)
    bridge = NotionBridge(
        notion_token=api_key, 
        memu_base_url="http://localhost:8000", 
        memu_api_key="dummy"
    )
    # Manually inject the IDs since create_bridge_from_env might rely on .env file or env vars differently
    bridge.task_board_id = task_board_id
    
    print(f"🔍 Polling Task Board: {task_board_id}")
    try:
        tasks = await bridge.poll_tasks(agent_id=None) # Poll all unassigned tasks
        print(f"✅ Success! Found {len(tasks)} tasks.")
        for t in tasks:
            print(f"   - [{t.get('status', 'Unknown')}] {t.get('title', 'No Title')} (ID: {t.get('id')})")
    except Exception as e:
        print(f"❌ Failed to poll: {e}")

if __name__ == "__main__":
    asyncio.run(smoke_test())
