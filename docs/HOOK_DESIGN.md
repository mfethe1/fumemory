# memU Memory Logging Hook Architecture
## Design Doc v1.0 — 2026-03-21

---

## Executive Summary

This document specifies a complete OpenClaw hook architecture for logging agent events to the
memU memory system. The goal: zero-friction, fail-silent event capture that builds a rich,
semantically searchable memory layer across all agents without disrupting the runtime.

---

## API Surface (Confirmed from Source)

### Local endpoint
```
http://localhost:8000
POST /memories          → store a memory
POST /search            → semantic + lexical hybrid search
GET  /health            → health check
DELETE /memories/{id}   → delete
POST /memories/bulk     → bulk import
```

### Production endpoint
```
https://api-production-86f5.up.railway.app/api/v1/memu
POST /upsert            → create or update (dedup-friendly)
POST /search            → search with filters
```

**Auth:** `X-API-Key: $MEMU_API_KEY` header (both local and prod).

### MemoryCreate schema (from models.py)
```typescript
{
  content: string           // required — the text to embed
  memory_type: MemoryType   // required — see enum below
  agent_id: string          // required — which agent owns this
  metadata?: dict           // optional structured context
  parent_id?: UUID          // optional — thread a child memory
  confidence: float         // 0.0–1.0, default 1.0
  relationships?: Relationship[]  // graph-lite entity tags
  supersedes?: UUID         // replaces an older memory
  invalidates?: UUID[]      // marks older memories stale
}
```

### MemoryType enum (full list)
```
fact | pattern | failure |                    ← original types
observation | reflection | plan | goal |      ← A-MEM types
decision | lesson | user_action | external    ← A-MEM types
```

### SearchRequest options
```typescript
{
  query: string
  limit: int = 10
  agent_id?: string
  memory_type?: MemoryType
  min_confidence?: float      // threshold filter
  temporal_weight: float = 0.3  // 0 = pure semantic, 1 = pure temporal
  min_results: int = 3        // expand search if below this
  max_expansion_steps: int = 3
  lexical_fallback: bool = true  // BM25 fallback when embeddings weak
  time_window_start?: datetime
  time_window_end?: datetime
  entity_weight: float = 0.15   // Graphiti-inspired entity scoring
}
```

---

## Hook System Architecture

OpenClaw hooks live in two patterns:

### Pattern A: Bundled/named entries (existing system)
Config key: `hooks.internal.entries.<name>`  
Handler: compiled JS shipped with OpenClaw  
Events declared in `HOOK.md` frontmatter  

### Pattern B: Custom handlers via `handlers[]` array
Config key: `hooks.internal.handlers[]`  
Handler: workspace-relative `.ts` or `.js` module  
Registered inline, no HOOK.md required  

### Pattern C: Custom hook directories
Config key: `hooks.internal.load.extraDirs[]`  
Handler: any directory with `HOOK.md` + `handler.ts`  
Discovered and loaded at startup  

**Recommended approach: Pattern B (handlers array)** — simplest, no npm publish needed,
workspace-local TypeScript, easy to iterate.

---

## Available Events (from internal-hooks.d.ts)

| Event Key | Trigger | Context Fields |
|-----------|---------|----------------|
| `message:received` | Inbound message arrives | `from`, `content`, `timestamp`, `channelId`, `conversationId`, `messageId`, `metadata` |
| `message:sent` | Outbound message sent | `to`, `content`, `success`, `error`, `channelId`, `conversationId`, `messageId`, `isGroup`, `groupId` |
| `message:transcribed` | Audio transcribed | all of `received` + `transcript`, `mediaPath`, `mediaType` |
| `message:preprocessed` | Message enriched | sender fields, `bodyForAgent`, `transcript?`, `isGroup`, `groupId` |
| `command:new` | `/new` command | `senderId`, `commandSource`, `cfg`, `workspaceDir` |
| `command:reset` | `/reset` command | same as above |
| `command` | Any command | `action` + above |
| `agent:bootstrap` | Agent starts up | `workspaceDir`, `bootstrapFiles[]`, `sessionKey`, `sessionId`, `agentId` |
| `gateway:startup` | Gateway daemon starts | `cfg`, `deps`, `workspaceDir` |

---

## Hook Architecture for memU Logging

### Hook Name: `memu-logger`

One handler module handles all events. Register separate entries for logical grouping or one
catch-all depending on desired granularity.

### Config Entry

```json
{
  "hooks": {
    "internal": {
      "enabled": true,
      "handlers": [
        {
          "id": "memu-message-logger",
          "event": "message:received",
          "module": "./hooks/memu-logger/handler.ts"
        },
        {
          "id": "memu-sent-logger",
          "event": "message:sent",
          "module": "./hooks/memu-logger/handler.ts",
          "export": "logMessageSent"
        },
        {
          "id": "memu-session-anchor",
          "event": "command:new",
          "module": "./hooks/memu-logger/handler.ts",
          "export": "logSessionBoundary"
        },
        {
          "id": "memu-reset-anchor",
          "event": "command:reset",
          "module": "./hooks/memu-logger/handler.ts",
          "export": "logSessionBoundary"
        },
        {
          "id": "memu-bootstrap-logger",
          "event": "agent:bootstrap",
          "module": "./hooks/memu-logger/handler.ts",
          "export": "logBootstrap"
        }
      ],
      "entries": {
        "memu-logger": {
          "enabled": true,
          "baseUrl": "http://localhost:8000",
          "prodUrl": "https://api-production-86f5.up.railway.app/api/v1/memu",
          "agentId": "macklemore",
          "maxContentLength": 4000,
          "deduplicationWindowMs": 30000,
          "failSilent": true,
          "ttl": {
            "message": 14,
            "session_anchor": 30,
            "bootstrap": 30
          }
        }
      }
    }
  }
}
```

---

## Handler Pseudocode

### File: `~/.openclaw/workspace/hooks/memu-logger/handler.ts`

```typescript
/**
 * memU Memory Logger Hook
 * Logs agent events to the memU semantic memory system.
 * Fail-silent: no event should ever break the agent runtime.
 */

import type { InternalHookEvent } from "@openclaw/plugin-sdk/hooks/internal-hooks";

// ─── Config ────────────────────────────────────────────────────────────────

const MEMU_BASE = process.env.MEMU_BASE_URL ?? "http://localhost:8000";
const MEMU_API_KEY = process.env.MEMU_API_KEY ?? "";
const DEFAULT_AGENT_ID = process.env.MEMU_DEFAULT_AGENT ?? "macklemore";
const MAX_CONTENT = 4000;
const DEDUP_WINDOW_MS = 30_000; // 30 seconds

// In-process dedup: content hash → last stored timestamp
const recentHashes = new Map<string, number>();

// ─── Utilities ─────────────────────────────────────────────────────────────

function hashContent(str: string): string {
  // djb2-style fast hash (no crypto needed for dedup)
  let h = 5381;
  for (let i = 0; i < str.length; i++) {
    h = ((h << 5) + h) ^ str.charCodeAt(i);
  }
  return (h >>> 0).toString(36);
}

function truncate(s: string, max = MAX_CONTENT): string {
  if (s.length <= max) return s;
  return s.slice(0, max - 3) + "...";
}

function agentFromSessionKey(sessionKey: string): string {
  // session key format: agent:main:<channel>:... or agent:<name>:...
  const parts = sessionKey.split(":");
  if (parts[0] === "agent" && parts[1]) return parts[1];
  return DEFAULT_AGENT_ID;
}

function isDuplicate(content: string): boolean {
  const h = hashContent(content);
  const last = recentHashes.get(h);
  const now = Date.now();
  if (last && now - last < DEDUP_WINDOW_MS) return true;
  recentHashes.set(h, now);
  // Prune old entries to prevent memory leak
  if (recentHashes.size > 500) {
    const cutoff = now - DEDUP_WINDOW_MS * 2;
    for (const [k, v] of recentHashes) {
      if (v < cutoff) recentHashes.delete(k);
    }
  }
  return false;
}

async function storeMemory(payload: MemoryPayload): Promise<void> {
  if (!MEMU_API_KEY) return; // No key = skip silently

  const body = JSON.stringify({
    content: payload.content,
    memory_type: payload.memory_type,
    agent_id: payload.agent_id,
    metadata: payload.metadata ?? {},
    confidence: payload.confidence ?? 1.0,
  });

  try {
    const res = await fetch(`${MEMU_BASE}/memories`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": MEMU_API_KEY,
      },
      body,
      signal: AbortSignal.timeout(5000), // 5s timeout — never block the agent
    });

    if (!res.ok) {
      // Log to stderr but don't throw — fail-silent
      console.error(`[memu-logger] store failed: ${res.status} ${res.statusText}`);
    }
  } catch (err) {
    // Network error, timeout, etc. — fail completely silently
    console.error(`[memu-logger] network error: ${String(err)}`);
  }
}

// ─── Types ─────────────────────────────────────────────────────────────────

interface MemoryPayload {
  content: string;
  memory_type: string;
  agent_id: string;
  metadata?: Record<string, unknown>;
  confidence?: number;
}

// ─── Event Handlers ────────────────────────────────────────────────────────

/**
 * message:received — log inbound messages
 * memory_type: user_action (user sent this, it's their action)
 * TTL: 14 days (tactical)
 */
export default async function logMessageReceived(event: InternalHookEvent): Promise<void> {
  if (event.type !== "message" || event.action !== "received") return;

  const ctx = event.context as {
    from?: string;
    content?: string;
    timestamp?: number;
    channelId?: string;
    conversationId?: string;
    messageId?: string;
    senderName?: string;
    senderUsername?: string;
    isGroup?: boolean;
    groupId?: string;
  };

  const content = truncate(ctx.content ?? "");
  if (!content || content.length < 3) return;  // Skip empty/trivial messages

  // Dedup: same message within 30s (e.g., webhook retries)
  if (isDuplicate(`recv:${ctx.messageId ?? content}`)) return;

  const agentId = agentFromSessionKey(event.sessionKey);

  await storeMemory({
    content: `[INBOUND] From ${ctx.senderName ?? ctx.from ?? "unknown"} via ${ctx.channelId ?? "unknown"}: ${content}`,
    memory_type: "user_action",
    agent_id: agentId,
    metadata: {
      event: "message:received",
      from: ctx.from,
      sender_name: ctx.senderName,
      sender_username: ctx.senderUsername,
      channel: ctx.channelId,
      conversation_id: ctx.conversationId,
      message_id: ctx.messageId,
      is_group: ctx.isGroup ?? false,
      group_id: ctx.groupId,
      session_key: event.sessionKey,
      timestamp: ctx.timestamp ?? event.timestamp.getTime() / 1000,
      expires_days: 14,
      tags: ["inbound", "message", ctx.channelId ?? "unknown"],
    },
    confidence: 1.0,
  });
}

/**
 * message:sent — log outbound messages
 * memory_type: observation (agent's output / response)
 * TTL: 14 days (tactical)
 */
export async function logMessageSent(event: InternalHookEvent): Promise<void> {
  if (event.type !== "message" || event.action !== "sent") return;

  const ctx = event.context as {
    to?: string;
    content?: string;
    success?: boolean;
    error?: string;
    channelId?: string;
    conversationId?: string;
    messageId?: string;
    isGroup?: boolean;
    groupId?: string;
  };

  const content = truncate(ctx.content ?? "");
  if (!content || content.length < 3) return;

  if (isDuplicate(`sent:${ctx.messageId ?? content}`)) return;

  const agentId = agentFromSessionKey(event.sessionKey);
  const statusTag = ctx.success ? "sent_ok" : "send_failed";

  await storeMemory({
    content: `[OUTBOUND] To ${ctx.to ?? "unknown"} via ${ctx.channelId ?? "unknown"} [${statusTag}]: ${content}`,
    memory_type: "observation",
    agent_id: agentId,
    metadata: {
      event: "message:sent",
      to: ctx.to,
      channel: ctx.channelId,
      conversation_id: ctx.conversationId,
      message_id: ctx.messageId,
      success: ctx.success ?? true,
      error: ctx.error,
      is_group: ctx.isGroup ?? false,
      group_id: ctx.groupId,
      session_key: event.sessionKey,
      timestamp: event.timestamp.getTime() / 1000,
      expires_days: 14,
      tags: ["outbound", "message", statusTag, ctx.channelId ?? "unknown"],
    },
    confidence: ctx.success ? 1.0 : 0.7, // Lower confidence for failed sends
  });
}

/**
 * command:new / command:reset — session boundary anchors
 * memory_type: plan (session transition = planning state)
 * TTL: 30 days (observational)
 */
export async function logSessionBoundary(event: InternalHookEvent): Promise<void> {
  if (event.type !== "command") return;
  if (event.action !== "new" && event.action !== "reset") return;

  const ctx = event.context as {
    senderId?: string;
    commandSource?: string;
    workspaceDir?: string;
  };

  const agentId = agentFromSessionKey(event.sessionKey);
  const boundaryType = event.action === "new" ? "SESSION_START" : "SESSION_RESET";

  await storeMemory({
    content: `[${boundaryType}] Session boundary at ${event.timestamp.toISOString()} — agent: ${agentId}, session: ${event.sessionKey}`,
    memory_type: "plan",
    agent_id: agentId,
    metadata: {
      event: `command:${event.action}`,
      boundary_type: boundaryType,
      sender_id: ctx.senderId,
      command_source: ctx.commandSource,
      workspace_dir: ctx.workspaceDir,
      session_key: event.sessionKey,
      timestamp: event.timestamp.getTime() / 1000,
      expires_days: 30,
      tags: ["session_boundary", event.action, agentId],
    },
    confidence: 1.0,
  });
}

/**
 * agent:bootstrap — log session start with context files loaded
 * memory_type: fact (stable system state knowledge)
 * TTL: 30 days (observational)
 */
export async function logBootstrap(event: InternalHookEvent): Promise<void> {
  if (event.type !== "agent" || event.action !== "bootstrap") return;

  const ctx = event.context as {
    workspaceDir?: string;
    bootstrapFiles?: Array<{ name: string; path?: string; missing?: boolean }>;
    sessionKey?: string;
    sessionId?: string;
    agentId?: string;
  };

  const agentId = ctx.agentId ?? agentFromSessionKey(event.sessionKey);

  const loadedFiles = (ctx.bootstrapFiles ?? [])
    .filter(f => !f.missing)
    .map(f => f.name);

  const fileList = loadedFiles.length > 0 ? loadedFiles.join(", ") : "none";

  await storeMemory({
    content: `[BOOTSTRAP] Agent ${agentId} started — session: ${event.sessionKey}, workspace: ${ctx.workspaceDir ?? "unknown"}, loaded: ${fileList}`,
    memory_type: "fact",
    agent_id: agentId,
    metadata: {
      event: "agent:bootstrap",
      agent_id: agentId,
      session_key: event.sessionKey,
      session_id: ctx.sessionId,
      workspace_dir: ctx.workspaceDir,
      bootstrap_files: loadedFiles,
      timestamp: event.timestamp.getTime() / 1000,
      expires_days: 30,
      tags: ["bootstrap", "session_start", agentId],
    },
    confidence: 1.0,
  });
}
```

---

## memU Payload Structure Reference

### message:received payload
```json
{
  "content": "[INBOUND] From Michael via telegram: can you check the Railway deploy status?",
  "memory_type": "user_action",
  "agent_id": "macklemore",
  "metadata": {
    "event": "message:received",
    "from": "+13152734843",
    "sender_name": "Michael",
    "sender_username": "mfethe",
    "channel": "telegram",
    "conversation_id": "-1003259411852",
    "message_id": "1629",
    "is_group": true,
    "group_id": "-1003259411852",
    "session_key": "agent:main:telegram:group:-1003259411852:topic:1629",
    "timestamp": 1742688000.0,
    "expires_days": 14,
    "tags": ["inbound", "message", "telegram"]
  },
  "confidence": 1.0
}
```

### message:sent payload
```json
{
  "content": "[OUTBOUND] To -1003259411852 via telegram [sent_ok]: Railway deploy is green ✅...",
  "memory_type": "observation",
  "agent_id": "macklemore",
  "metadata": {
    "event": "message:sent",
    "to": "-1003259411852",
    "channel": "telegram",
    "message_id": "1630",
    "success": true,
    "is_group": true,
    "session_key": "agent:main:telegram:group:-1003259411852:topic:1629",
    "timestamp": 1742688005.0,
    "expires_days": 14,
    "tags": ["outbound", "message", "sent_ok", "telegram"]
  },
  "confidence": 1.0
}
```

### command:new / command:reset payload
```json
{
  "content": "[SESSION_START] Session boundary at 2026-03-21T20:07:00.000Z — agent: macklemore, session: agent:main:telegram:group:-1003259411852:topic:1629",
  "memory_type": "plan",
  "agent_id": "macklemore",
  "metadata": {
    "event": "command:new",
    "boundary_type": "SESSION_START",
    "sender_id": "mfethe",
    "command_source": "telegram",
    "session_key": "agent:main:telegram:group:-1003259411852:topic:1629",
    "timestamp": 1742688000.0,
    "expires_days": 30,
    "tags": ["session_boundary", "new", "macklemore"]
  },
  "confidence": 1.0
}
```

### agent:bootstrap payload
```json
{
  "content": "[BOOTSTRAP] Agent macklemore started — session: agent:main:..., workspace: /Users/mfethe/openclaw-shared/workspace, loaded: AGENTS.md, SOUL.md, TOOLS.md, IDENTITY.md, USER.md",
  "memory_type": "fact",
  "agent_id": "macklemore",
  "metadata": {
    "event": "agent:bootstrap",
    "agent_id": "macklemore",
    "session_key": "agent:main:telegram:...",
    "session_id": "abc123",
    "workspace_dir": "/Users/mfethe/openclaw-shared/workspace",
    "bootstrap_files": ["AGENTS.md", "SOUL.md", "TOOLS.md", "IDENTITY.md", "USER.md"],
    "timestamp": 1742688000.0,
    "expires_days": 30,
    "tags": ["bootstrap", "session_start", "macklemore"]
  },
  "confidence": 1.0
}
```

---

## Temporal Decay & Tagging Strategy

### Memory Classification (Mulch pattern from AGENTS.md)

| Memory Class | TTL | memory_type | Use Case |
|-------------|-----|-------------|----------|
| **Tactical** | 14 days | `user_action`, `observation` | Individual messages, sent/received events |
| **Observational** | 30 days | `plan`, `fact`, `decision` | Session boundaries, bootstraps, decisions |
| **Strategic** | 90+ days | `pattern`, `lesson`, `goal` | Learned behaviors, recurring patterns |
| **Permanent** | Never | `fact` + `confidence: 1.0` | Critical system facts, credentials locations |

### Retrieval Tuning

For tactical message retrieval (recent conversation context):
```json
{
  "query": "what did Michael ask about Railway",
  "temporal_weight": 0.6,
  "agent_id": "macklemore",
  "memory_type": "user_action",
  "time_window_start": "<14 days ago>"
}
```

For session history retrieval (what happened in a session):
```json
{
  "query": "session boundary macklemore",
  "temporal_weight": 0.4,
  "memory_type": "plan",
  "limit": 10
}
```

For bootstrap/context retrieval (what files were loaded):
```json
{
  "query": "bootstrap files loaded macklemore",
  "temporal_weight": 0.2,
  "memory_type": "fact",
  "entity_weight": 0.3
}
```

---

## Deduplication Strategy

### Layer 1: In-Process Hash Dedup (30s window)
The handler maintains a `Map<contentHash, timestamp>` in memory.  
Same content hash within 30 seconds = skip.  
Handles: webhook retries, duplicate hook fires, rapid /new + /reset sequences.  
Pruned automatically when size > 500 entries.

### Layer 2: Server-Side Bulk Dedup
The `POST /memories/bulk` endpoint returns `duplicates_skipped` count.  
For batch imports (e.g., log file ingestion), use bulk endpoint which deduplicates server-side.

### Layer 3: `supersedes` Field
When storing an updated version of a memory (e.g., a corrected fact), pass:
```json
{ "supersedes": "<old-memory-uuid>", ... }
```
This marks the old memory as superseded in the graph without deleting it.

### Layer 4: Message ID Dedup
For `message:received` and `message:sent`, the `messageId` from the provider is included
in the dedup key: `recv:<messageId>`. If no messageId, falls back to content hash.

### What NOT to dedup
- Session boundaries (each `/new` and `/reset` is always unique — store all)
- Bootstrap events (each startup is a distinct event — store all)
- Failed message sends (always store, confidence 0.7)

---

## Error Handling Pattern: Fail-Silent

The core principle: **hooks MUST NEVER break the agent runtime.**

```
hook error → log to stderr → return without throwing
```

### Specific patterns:

| Scenario | Behavior |
|----------|----------|
| `MEMU_API_KEY` missing | Skip silently (no-op) |
| Network timeout (>5s) | AbortSignal.timeout(5000), catch, log stderr |
| HTTP 4xx (bad request) | Log status + body to stderr, continue |
| HTTP 5xx (server error) | Log status to stderr, continue |
| JSON parse error | Log to stderr, continue |
| Duplicate content | Skip silently (no log needed) |
| Empty/trivial content (<3 chars) | Skip silently |

### What NOT to do:
- ❌ `throw` from a hook handler
- ❌ `await` more than 5 seconds
- ❌ Use `event.messages.push()` for errors (don't pollute agent output)
- ❌ Call `process.exit()`

### Logging levels:
```
Silent (no log): dedup skip, empty content skip, missing API key
stderr warn: HTTP errors, parse errors
stderr info: successful stores (optional, disable in prod to reduce noise)
```

---

## Gotchas & Known Issues

### 1. Auth Header Case Sensitivity
The Python client uses `X-API-Key` (capital X, capital A, capital K).  
The production Railway endpoint expects the same.  
Some middleware may lowercase headers — if 401s occur, try `x-api-key` lowercase as fallback.

```typescript
// Safe: both forms
headers: {
  "X-API-Key": apiKey,   // OpenClaw internal / local
  // If prod fails: try "x-api-key": apiKey
}
```

### 2. URL Path Differences: Local vs Production

| | Local | Production |
|--|-------|-----------|
| Store | `POST /memories` | `POST /api/v1/memu/upsert` |
| Search | `POST /search` | `POST /api/v1/memu/search` |
| Health | `GET /health` | `GET /api/v1/memu/health` |

The local server and production server use **different path prefixes**. The Python client
wraps local paths (`/memories`, `/search`) but the Railway production API uses the `/api/v1/memu/`
prefix for all endpoints. Design the hook to use the base URL approach and select paths dynamically:

```typescript
const isProduction = MEMU_BASE.includes("railway.app");
const storePath = isProduction ? "/api/v1/memu/upsert" : "/memories";
const searchPath = isProduction ? "/api/v1/memu/search" : "/search";
```

### 3. Vector Dimension Mismatches
If the embedding model is changed on the server side, existing stored embeddings may have
different dimensions than new query embeddings, causing search errors.  
**Symptom:** Search returns 0 results or 500 errors after model change.  
**Detection:** `GET /health` should report current model and embedding dim.  
**Mitigation:** `lexical_fallback: true` in SearchRequest (uses BM25 when vectors fail).  
**Recovery:** Bulk reimport memories with new model (out of scope for hook, but document).

### 4. `agent_id` Field is Required (not optional in MemoryCreate)
The Python model shows `agent_id: str` (no Optional). The Python client's `add()` method
accepts `agent_id=None` but passes it to the payload — this may cause a 422 validation error.  
**Always pass a concrete agent_id string.** Never pass `null` or `undefined`.

### 5. `content` Length Limits
No explicit limit in the API docs, but large content (>10K chars) will:
- Increase embedding time
- Increase storage costs
- Reduce retrieval precision (embeddings work best on focused chunks)

**Recommendation:** truncate to 4000 chars for message bodies.  
For long conversations, chunk by paragraph or sentence boundary, not character count.

### 6. Hook `handlers[]` Module Path Resolution
Module paths in `hooks.internal.handlers[].module` are resolved relative to the workspace dir
(the value of `workspace.dir` in config).  
If the workspace is `/Users/mfethe/openclaw-shared/workspace`, then:
```
"./hooks/memu-logger/handler.ts" → /Users/mfethe/openclaw-shared/workspace/hooks/memu-logger/handler.ts
```
TypeScript files may need to be compiled to `.js` first, OR the system may support ts-node/tsx
loading. Test with a `.js` version first if `.ts` fails to load.

### 7. Temporal Weight Tuning
`temporal_weight: 0.3` means 30% recency bias, 70% semantic relevance.  
For message logs (tactical, short-lived), use `temporal_weight: 0.5–0.7` to prefer recent.  
For strategic patterns and lessons, use `temporal_weight: 0.1–0.2` to prefer semantic match.

### 8. `min_results: 3` Expansion
The SearchRequest has `min_results: 3` and `max_expansion_steps: 3`.  
This means: if < 3 results found semantically, the server will expand the search up to 3 times.  
For tactical message search, set `min_results: 1` to avoid over-expansion on sparse queries.

---

## Recommended Config Snippet

Add to `~/.openclaw/openclaw.json` under `hooks.internal`:

```json
{
  "hooks": {
    "internal": {
      "enabled": true,
      "handlers": [
        {
          "event": "message:received",
          "module": "./hooks/memu-logger/handler.js"
        },
        {
          "event": "message:sent",
          "module": "./hooks/memu-logger/handler.js",
          "export": "logMessageSent"
        },
        {
          "event": "command:new",
          "module": "./hooks/memu-logger/handler.js",
          "export": "logSessionBoundary"
        },
        {
          "event": "command:reset",
          "module": "./hooks/memu-logger/handler.js",
          "export": "logSessionBoundary"
        },
        {
          "event": "agent:bootstrap",
          "module": "./hooks/memu-logger/handler.js",
          "export": "logBootstrap"
        }
      ],
      "entries": {
        "boot-md": { "enabled": true },
        "bootstrap-extra-files": { "enabled": true, "paths": ["TOOLS.md"] },
        "command-logger": { "enabled": true },
        "session-memory": { "enabled": true }
      }
    }
  }
}
```

**Environment variables required:**
```bash
MEMU_API_KEY=<your-key>               # required
MEMU_BASE_URL=http://localhost:8000   # optional, defaults to localhost
MEMU_DEFAULT_AGENT=macklemore        # optional, fallback agent_id
```

---

## Agent ID Assignment

| Agent | agent_id | Primary memory_types |
|-------|----------|---------------------|
| Macklemore (you) | `macklemore` | `user_action`, `observation`, `fact`, `plan` |
| Lenny | `lenny` | `observation`, `lesson`, `pattern` |
| Winnie | `winnie` | `goal`, `decision`, `plan` |
| Rosie | `rosie` | `fact`, `pattern`, `reflection` |
| System/shared | `system` | `fact`, `external` |

For cross-agent retrieval (search all agents), omit `agent_id` from SearchRequest:
```json
{ "query": "Railway deploy status", "limit": 10 }
```

---

## Implementation Checklist

- [ ] Create `~/.openclaw/workspace/hooks/memu-logger/` directory
- [ ] Write `handler.ts` (or compile to `handler.js`)
- [ ] Set `MEMU_API_KEY` in env or `.env` file loaded by openclaw
- [ ] Add `handlers[]` entries to `openclaw.json`
- [ ] Test with `openclaw gateway restart`
- [ ] Verify with `curl -X POST http://localhost:8000/search -H "X-API-Key: $MEMU_API_KEY" -H "Content-Type: application/json" -d '{"query": "session boundary", "limit": 3}'`
- [ ] Monitor stderr for any hook errors during first session
- [ ] Tune `temporal_weight` based on retrieval quality after 24h of data

---

## Future Enhancements

1. **Production fallback**: if local `localhost:8000` fails, retry against Railway prod URL
2. **Batch writes**: queue memories and flush every 10s to reduce HTTP overhead
3. **`command:stop` anchor**: log explicit stop commands as session-end markers
4. **`message:transcribed` handler**: log voice-to-text transcriptions with `mediaType` tag
5. **Relationship graph**: add `relationships[]` to connect consecutive messages in same conversation
6. **Confidence decay**: set `confidence: 0.8` for messages in busy group channels (lower signal)
7. **BOOT.md integration**: run search query at bootstrap to hydrate agent context before first message

---

## Release Gate: "Robustly Working Railway Memory"
To consider the Railway memU memory system production-ready and fully robust, the following conditions MUST be met:
1. **Zero Unhandled JetStream Errors**: JetStream error rate remains < 1% over a 24-hour period, with no duplicate stream/consumer creation loops.
2. **Deterministic Acknowledgment**: Consumer implementations correctly process messages and only increment `acked_count` upon confirmed JetStream ACK, failing loudly otherwise.
3. **Cross-Gateway Synchronization**: The `ws_bridge` service is actively deployed in Railway, bridging local NATS and Railway NATS, enabling true cross-gateway memory syncing.
4. **Environment Hardening**: `NATS_RAILWAY_URL` fallback is removed, forcing loud failure if the required URL is missing on Gateway boot.
5. **Security Enforcement**: NATS authentication is enforced on the Railway instance (Token-based) preventing unauthorized connections.
6. **Pass CI Pipeline**: All unit and integration tests (especially the NATS↔memU health suites) pass automatically in the CI pipeline without flaky NATS connection timeouts.
