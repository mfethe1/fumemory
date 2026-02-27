# Notion Integration Guide

This document explains how to connect the Notion Agent Task Board and Agent Memory
Log with memU.

## 1) Create a Notion integration token

1. Go to **https://www.notion.so/my-integrations**.
2. Create a new internal integration.
3. Copy the **Internal Integration Token**.
4. Put it in environment as `NOTION_API_KEY`.

## 2) Share Notion databases with the integration

For each database in this integration:

1. Open the database page in Notion.
2. Open **Share**.
3. Invite your integration.
4. Grant **Edit** permissions (required for claiming/completing tasks).

Databases expected by this integration:

- `Agent Task Board`
- `Agent Memory Log`

## 3) Create a task for agents

Create a row/page in **Agent Task Board** with:

- **Title**: concise task name
- **Status**: usually `Backlog`
- **Assigned Agent**: `Rosie`, `Lenny`, `Macklemore`, `Winnie`, or `Any`
- **Priority**: `P0`/`P1`/`P2`/`P3`
- **Project**: free text project label
- **Agent Notes**: optional notes field for progress/results
- **Task ID**: can be generated automatically by the bridge/API

The poller defaults to pulling tasks with `Status = Backlog` and matching assignment.

## 4) How agents write back results

When an agent calls `POST /notion/complete`:

- Status is set to `Done`.
- `Completed At` is stamped.
- `Agent Notes` are written/append with completion notes.
- A memory is created in memU with content containing task title + notes.

## 5) Read agent results in Notion

Filter on the Agent Task Board by:

- `Status = Done` for completed work
- `Assigned Agent` by your agent
- `Priority` to triage backlog/review

Agent notes are maintained in the `Agent Notes` column and are visible directly
on each task row.

## 6) API endpoints

All endpoints are under `/notion/*` on the memU API service.

- `GET /notion/queue?agent_id=<agent>`
  - Returns unclaimed tasks for the optional agent.
  - If omitted, returns all unassigned/`Any` backlog tasks.

- `POST /notion/claim`
  - Body: `{ "task_id": "...", "agent_id": "rosie" }`
  - Marks task as `In Progress` and sets `Claimed At`.

- `POST /notion/complete`
  - Body:
    `{ "task_id": "...", "agent_id": "rosie", "notes": "...", "memory_type": "lesson" }`
  - Marks task as `Done`, appends notes, and syncs to memU.

- `POST /notion/create`
  - Body: `{ "title": "...", "priority": "P2", "project": "", "assigned_agent": "Any" }`
  - Creates a new task in Agent Task Board.

- `GET /notion/health`
  - Returns notion and memU check status.

## 7) Troubleshooting

### 401 from memU when completing

- Verify `MEMU_API_KEY` in env.
- Verify memU is running and healthy (`GET /health`).
- In completion path, auth failures are logged and do **not** block notion status update.

### Embedding dimension mismatch

- `MEMU_EMBEDDING_DIMS`/`EMBEDDING_DIMS` should match the configured embedding
  model.
- Review memU logs for explicit mismatch messages.

### Tasks are not claimed

- Ensure `NOTION_TASK_BOARD_ID` is set.
- Ensure the Notion integration has edit access to Agent Task Board.
- Ensure task status is `Backlog` and assignment matches `agent_id` or `Any`.
- Confirm poller is running with correct `AGENT_ID` and `POLL_INTERVAL`.

## 8) One-shot DB bootstrap

After setting `NOTION_API_KEY` (and optional `MEMU_API_KEY`):

```bash
python scripts/setup_notion.py
```

The script writes `NOTION_TASK_BOARD_ID` and `NOTION_MEMORY_LOG_ID` into `.env`.
