# Cross-Gateway Tracking Spec

_Date:_ 2026-03-23  
_Owner:_ Winnie  
_Status:_ Draft implemented with hook/client artifacts

## Goal

Every agent turn must leave behind enough durable context in memU that another gateway can:

1. detect who was working on what,
2. recover the latest checkpoint after a crash or restart,
3. understand the last successful tool step,
4. resume safely without duplicating irreversible work.

This spec defines the minimum event contract for OpenClaw ↔ memU shared memory across gateways.

---

## 1) Required event classes per agent turn

These are the minimum events that must be written for each task lifecycle:

| Event | When it fires | Required? | Why it matters |
|---|---|---:|---|
| `task_start` | Agent accepts/starts a task turn | Yes | Creates the durable task/session anchor |
| `tool_pre` | Immediately before a tool call | Yes | Records planned external action before execution |
| `tool_post` | Immediately after tool return | Yes | Captures outcome, outputs, and last known good step |
| `task_complete` | Task finishes successfully or is intentionally stopped | Yes | Declares terminal checkpoint |
| `error` | Exception, failed tool, timeout, or degraded path | Yes | Makes failure recoverable/searchable |
| `checkpoint` | Meaningful intermediate state save | Recommended | Lets another gateway resume long tasks mid-flight |

### Notes
- `tool_pre` + `tool_post` is the minimum pair needed for durable resumption.
- If only one can be emitted during an outage window, prefer `tool_post` when the action actually happened.
- For irreversible actions, `tool_pre` must carry an idempotency key or external request identifier when available.

---

## 2) Metadata that must survive gateway restarts

Every logged event must include the following metadata fields.

### Required fields

| Field | Type | Purpose |
|---|---|---|
| `agent_id` | string | Logical agent identity (`macklemore`, `winnie`, etc.) |
| `gateway_id` | string | Physical/runtime host that executed the turn |
| `session_id` | string | Runtime session/container identity |
| `task_id` | string | Stable task or request identifier |
| `event_type` | string | `task_start`, `tool_pre`, `tool_post`, `task_complete`, `error`, `checkpoint` |
| `logged_at` | ISO-8601 | Write timestamp used for ordering and recovery |
| `turn_id` | string | Stable identifier for the current agent turn |

### Required when applicable

| Field | Type | When |
|---|---|---|
| `tool_name` | string | tool hooks |
| `tool_call_id` | string | tool hooks when available |
| `status` | string | `tool_post`, `task_complete`, `error` |
| `error` | string | failures |
| `channel` | string | user-facing chat/message work |
| `conversation_id` | string | chat-bound sessions |
| `message_id` | string | message-triggered work |
| `idempotency_key` | string | external writes or any non-repeatable action |
| `checkpoint_seq` | integer | checkpoint events |
| `resume_hint` | string | optional compact human summary for handoff |

### Recommended fields
- `workspace_dir`
- `branch`
- `commit_sha`
- `model`
- `latency_ms`
- `tokens_used`
- `result_digest`
- `parent_event_id`

---

## 3) Resume query pattern

A replacement gateway should not search with vague natural language first. Recovery should use a deterministic sequence.

### Primary lookup order

1. **Latest terminal or near-terminal state by `task_id`**
   - Query: `task:<task_id> checkpoint OR task_complete OR error OR tool_post`
2. **If `task_id` missing, latest state by `session_id`**
   - Query: `session:<session_id> checkpoint OR task_complete OR error OR tool_post`
3. **Fallback by `agent_id` + recent window**
   - Query: `agent:<agent_id> checkpoint OR task_complete OR error OR tool_post`
4. **If tool ambiguity remains, inspect most recent `tool_pre` without matching `tool_post`**
   - Treat as "planned but not confirmed complete"

### Resume rule

The recovery gateway should resume from the latest successful `tool_post`, `checkpoint`, or `task_complete` event.

### Duplicate-action guard

If the most recent event is a `tool_pre` for an irreversible action and there is no matching `tool_post`, the recovery gateway must:
- check for `idempotency_key` or external request ID,
- query the external system if possible,
- only replay when the action is confirmed not to have happened.

---

## 4) Canonical event envelope

```json
{
  "content": "[tool_post] agent=winnie tool=browser status=ok",
  "agent_id": "winnie",
  "memory_type": "observation",
  "metadata": {
    "source": "openclaw_hook",
    "event_type": "tool_post",
    "gateway_id": "mac-mini-main",
    "session_id": "agent:main:subagent:1234",
    "task_id": "nats-memu-audit-2026-03-23",
    "turn_id": "turn-0007",
    "tool_name": "browser",
    "tool_call_id": "call_abc123",
    "status": "ok",
    "logged_at": "2026-03-23T17:31:00Z",
    "idempotency_key": null,
    "resume_hint": "Browser navigation succeeded; next step is fetch search results page"
  }
}
```

---

## 5) OpenClaw hook requirements

The OpenClaw hook layer must be:

1. **Fail-silent** — memU outage must never crash the agent runtime.
2. **Timeout bounded** — target 5s max per write.
3. **Retry limited** — short retries on 429/5xx/network errors only.
4. **Dual-header compatible** — send both `X-MemU-Key` and legacy `X-API-Key`.
5. **Cross-gateway aware** — always include both `agent_id` and `gateway_id`.
6. **Resume-oriented** — write enough structured metadata to reconstruct the last good step.

---

## 6) Implementation mapping in this repo

### Client resilience
- `memu/client.py`
  - `MemUClient` already has retry-capable sync transport.
  - `AsyncMemUClient` added with retry-capable async transport plus `ping()`.

### Hook logging helper
- `memu/openclaw_hooks.py`
  - writes to `/api/v1/memu/add`
  - searches via `/api/v1/memu/search`
  - exposes `log_task_start`, `log_tool_pre`, `log_tool_post`, `log_task_complete`, `log_error`
  - fail-silent by design

### OpenClaw config target
Place hook wiring in `~/.openclaw/openclaw.json` under `hooks`.

Illustrative shape:

```json
{
  "hooks": {
    "internal": {
      "enabled": true,
      "handlers": [
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
          "event": "task:start",
          "module": "./hooks/memu-logger/handler.js",
          "export": "logTaskStart"
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

---

## 7) Smoke-test acceptance

A hook implementation is considered good enough when:

- a `task_start` write lands in Railway memU,
- at least one `tool_pre` + `tool_post` pair lands with the same `tool_call_id`,
- a follow-up search by `task_id` returns the latest checkpoint,
- a memU outage does **not** break the OpenClaw run.

---

## 8) Product decision

**Decision:** Cross-gateway tracking should optimize for deterministic recovery, not exhaustive transcript storage.

That means the contract should prioritize:
- stable identifiers,
- latest-good-step checkpoints,
- idempotency markers,
- short searchable summaries,

rather than trying to mirror every token or raw tool payload in full.
