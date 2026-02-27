"""Background poller for Notion tasks.

The process periodically fetches pending tasks for a configured agent and claims
one task at a time to provide simple in-process queuing semantics.
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

from memu.notion_bridge import NotionBridge, create_bridge_from_env


class NotionPoller:
    """Poll and auto-claim tasks from Notion for a single agent."""

    def __init__(self, bridge: NotionBridge, agent_id: str, poll_interval: int = 60) -> None:
        """Initialize poller state.

        Args:
            bridge: bridge instance to query notion and claim tasks.
            agent_id: agent identity used for filtering claims.
            poll_interval: polling cadence in seconds.
        """
        self.bridge = bridge
        self.agent_id = agent_id
        self.poll_interval = poll_interval
        self._pending: list[dict[str, Any]] = []
        self._running = True

    def get_pending_tasks(self) -> list[dict[str, Any]]:
        """Return a copy of pending tasks currently claimed by this poller."""
        return list(self._pending)

    def mark_claimed(self, task_id: str) -> None:
        """Remove a task from queue after it has been claimed/handled."""
        self._pending = [task for task in self._pending if task.get("id") != task_id]

    def stop(self) -> None:
        """Stop the poll loop gracefully."""
        self._running = False

    async def run_once(self) -> list[dict[str, Any]]:
        """Poll for pending tasks once, claim them, and return newly claimed tasks."""
        tasks = await self.bridge.poll_tasks(agent_id=self.agent_id)
        claimed = []
        for task in tasks:
            task_id = task.get("id")
            if not task_id:
                continue
            if any(item.get("id") == task_id for item in self._pending):
                continue
            await self.bridge.claim_task(task_id, self.agent_id)
            print(f"[notion-poller] Claimed task {task_id}: {task.get('title', '').strip()}")
            self._pending.append(task)
            claimed.append(task)
        return claimed

    async def run(self) -> None:
        """Run the polling loop until stop() or shutdown signal is received."""
        while self._running:
            try:
                await self.run_once()
            except Exception as exc:
                print(f"[notion-poller] Poll cycle failed: {exc}")
            await asyncio.sleep(self.poll_interval)


def _install_signal_handlers(poller: NotionPoller) -> None:
    """Register SIGTERM/SIGINT handlers for graceful shutdown."""

    def _handle(_signum: int, _frame: object | None = None) -> None:
        poller.stop()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


async def main() -> None:
    """Start a poller process in standalone execution mode."""
    agent_id = os.environ.get("AGENT_ID", "any")
    poll_interval = int(os.environ.get("POLL_INTERVAL", "60"))

    bridge = await create_bridge_from_env()
    poller = NotionPoller(bridge=bridge, agent_id=agent_id, poll_interval=poll_interval)
    _install_signal_handlers(poller)

    try:
        await poller.run()
    finally:
        await bridge.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
