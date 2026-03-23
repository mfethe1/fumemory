# OpenClaw → memU Hook Design

_Date:_ 2026-03-23  
_Owner:_ Winnie

## Purpose

This document turns the cross-gateway tracking spec into a concrete hook logging architecture.
The requirement is simple: OpenClaw tool/task hooks must write durable recovery breadcrumbs to memU without ever breaking the live agent runtime.

## What changed

The previous helper in `memu/openclaw_hooks.py` pointed at `/memories/async`, which is not a live API contract in this repo.
The implementation now targets the stable compatibility endpoints already exposed by `memu/api.py`:

- `POST /api/v1/memu/add`
- `POST /api/v1/memu/search`

It also sends both auth headers:
- `X-MemU-Key`
- `X-API-Key`

That keeps local, Railway, and compatibility callers aligned.

## Event model

The hook layer writes five primary event types:

- `task_start`
- `tool_pre`
- `tool_post`
- `task_complete`
- `error`

Each write includes:
- `agent_id`
- `gateway_id`
- `session_id`
- `task_id`
- `event_type`
- `logged_at`
- tool identifiers and status when applicable

## Python helper

Reference implementation: `memu/openclaw_hooks.py`

Exports:
- `OpenClawHookLogger`
- `log_action()`
- `log_search()`
- `recall()`

Primary methods:
- `log_task_start(...)`
- `log_tool_pre(...)`
- `log_tool_post(...)`
- `log_task_complete(...)`
- `log_error(...)`
- `search_resume_context(...)`

## Failure behavior

The logger is intentionally fail-silent.

Rules:
- missing API key => no-op
- timeout/network issue => bounded retry, then warn only
- 429/5xx => retry briefly, then warn only
- hook failure must never raise into OpenClaw runtime

## Retry policy

- timeout budget: 5s
- default retries: 2
- retryable statuses: `408, 409, 425, 429, 500, 502, 503, 504`
- exponential backoff capped at 2s

## Resume query contract

Recovery should query memU in this order:

1. by `task_id`
2. by `session_id`
3. by `agent_id`

Recommended query shape:

```json
{
  "query": "agent:winnie task:nats-memu-audit-2026-03-23 checkpoint OR task_complete OR error OR tool_post",
  "agent_id": "winnie",
  "limit": 10,
  "entity_weight": 0.2
}
```

## OpenClaw config shape

Put this in `~/.openclaw/openclaw.json` under `hooks`:

```json
{
  "hooks": {
    "internal": {
      "enabled": true,
      "handlers": [
        {
          "event": "task:start",
          "module": "./hooks/memu-logger/handler.js",
          "export": "logTaskStart"
        },
        {
          "event": "tool:pre",
          "module": "./hooks/memu-logger/handler.js",
          "export": "logToolPre"
        },
        {
          "event": "tool:post",
          "module": "./hooks/memu-logger/handler.js",
          "export": "logToolPost"
        },
        {
          "event": "task:complete",
          "module": "./hooks/memu-logger/handler.js",
          "export": "logTaskComplete"
        },
        {
          "event": "task:error",
          "module": "./hooks/memu-logger/handler.js",
          "export": "logError"
        }
      ]
    }
  }
}
```

## JS handler note

The repo now defines the event contract and Python reference implementation.
If OpenClaw runtime wiring is performed from the workspace side, the JS handler should be a thin adapter that:

1. extracts event context,
2. maps it into the above envelope,
3. calls the same memU endpoints,
4. never throws.

## Smoke test

Minimum smoke path:

1. invoke one hook write against Railway memU,
2. search by `task_id`,
3. confirm the event returns,
4. temporarily break the memU URL and confirm the agent run still continues.
