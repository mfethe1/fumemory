---
name: agent-mesh
description: Send messages, requests, and tasks directly to other agents (Winnie, Lenny, Mack, Rosie) via NATS pub/sub. Use when you need to trigger an action on another gateway, request work from another agent, check if another agent is alive, or coordinate multi-agent tasks without depending on shared files or waiting for cron jobs.
---

# Agent Mesh — Direct Agent-to-Agent Communication

## How It Works

All agents connect to the same NATS mesh. Messages are published to agent-specific subjects and consumed in real-time. No shared drive dependency. No polling. Instant delivery.

```
Agent A → NATS publish → swarm.agent.{target}.inbox → Agent B processes
Agent B → NATS publish → swarm.agent.{source}.inbox → response back
```

## Quick Reference

### Send a message to another agent
```bash
python scripts/agent_send.py --to lenny --type request --message "Build gateway image and run smoke test"
```

### Listen for incoming messages (run in background)
```bash
python scripts/agent_listen.py --agent winnie
```

### Check which agents are online
```bash
python scripts/agent_ping.py
```

## NATS Subjects

| Subject | Purpose |
|---------|---------|
| `swarm.agent.{name}.inbox` | Direct messages to a specific agent |
| `swarm.agent.{name}.response` | Responses from that agent |
| `swarm.agent.broadcast` | Broadcast to ALL agents |
| `swarm.agent.ping` | Heartbeat/presence check |
| `swarm.agent.pong.{name}` | Heartbeat response from agent |

Agent names: `winnie`, `lenny`, `mack`, `rosie`

## Message Schema

All messages MUST use this envelope:

```json
{
  "msg_id": "uuid",
  "timestamp": "ISO-8601",
  "from": "winnie",
  "to": "lenny",
  "type": "request|response|broadcast|ping|task|status",
  "priority": "normal|urgent|low",
  "payload": {
    "message": "Build the gateway Docker image",
    "context": {},
    "reply_to": null
  }
}
```

## Message Types

| Type | Purpose | Expected Action |
|------|---------|----------------|
| `request` | Ask another agent to do something | Recipient should act and respond |
| `response` | Reply to a request | Contains result of requested action |
| `task` | Assign a task with tracking | Creates a tracked task in the DAG |
| `status` | Request status update | Recipient publishes their current status |
| `broadcast` | Inform all agents | No response expected |
| `ping` | Check if agent is alive | Auto-responds with `pong` |

## Integration with Telegram

Agents can ALSO coordinate via the shared Telegram group "Self Improvment" (`-5176175603`). Use NATS for machine-to-machine coordination; use Telegram for human-visible updates.

## Integration with OpenClaw Sessions

If an agent runs on the same OpenClaw gateway, use `sessions_send` for direct session messaging. For cross-gateway (different machines), NATS is the only reliable path.

## Fallback Chain

1. **NATS direct** (fastest, preferred) — `swarm.agent.{name}.inbox`
2. **Telegram group** (human-visible) — mention `@{Agent}FetheBot`
3. **File-based** (last resort) — write to `agent-coordination/` on shared drive

## Error Handling

- If NATS publish fails, retry once with Railway fallback URL
- If no response within 30s, escalate to Telegram group
- If agent doesn't respond to 3 consecutive pings, mark as `offline` and alert group
