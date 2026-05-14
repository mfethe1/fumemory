# fumemory

fumemory is the shared memory and evidence layer for OpenClaw-driven agent work. It records durable recall, coordination evidence, and federation proof while OpenClaw remains the top-level coordinator.

## Language

**OpenClaw Coordinator**:
The top-level system that decides which work should happen and which gateway or agent owns it.
_Avoid_: memU control plane, memory orchestrator

**Memory Evidence Plane**:
The fumemory role responsible for durable memory writes, recall, audit evidence, leases, and federation proof.
_Avoid_: authoritative control plane

**OpenClaw Gateway**:
A stable execution node that can run agents, publish to the shared NATS federation, and write evidence to fumemory.
_Avoid_: random worker, transient host

**Federation Proof**:
A verifiable record that a gateway can connect, publish, consume, and write searchable memory through the shared Railway-backed system.
_Avoid_: readiness vibes, smoke only

**Canonical Memory Write**:
The synchronous OpenClaw-to-fumemory write path that persists evidence and makes it immediately searchable.
_Avoid_: async hook write, best-effort logging

**Completion-Proof Write**:
A Canonical Memory Write required to prove task completion or gateway readiness.
_Avoid_: optional telemetry

**Telemetry Memory Write**:
A non-blocking memory write for low-risk activity context that may retry or degrade without blocking task completion.
_Avoid_: completion proof

**Embedding Contract**:
The canonical provider and dimension configuration used to create searchable memory vectors.
_Avoid_: mixed embedding env vars, destructive dimension rewrites

**Core API Readiness**:
The deployment gate proving fumemory API, Postgres, auth, canonical write, and recall work.
_Avoid_: full federation readiness

**Federation Readiness**:
The deployment gate proving Railway NATS/JetStream, gateway smoke, and searchable memory proof work in addition to core API readiness.
_Avoid_: core health check

**Memory Action Eval**:
A multi-session evaluation proving that stored evidence becomes learning and changes later OpenClaw behavior.
_Avoid_: recall-only benchmark

**Async Memory Workflow**:
An optional durable background path for long-running memory processing once it preserves the same contract as a canonical write.
_Avoid_: default OpenClaw write path

**Evidence Memory**:
An immutable, task-bound record of what happened during OpenClaw execution.
_Avoid_: lesson, reusable knowledge

**Learning Memory**:
A distilled reusable insight derived from evidence after review or consolidation.
_Avoid_: raw tool log, execution trace

**Reflection Review Window**:
A six-hour period where the user can approve, deny, or edit a proposed Learning Memory before automatic integration.
_Avoid_: permanent pending review

**Reflection Review Surface**:
The user-facing channel where proposed Learning Memory is shown for approval, denial, or editing.
_Avoid_: source of truth

**Reflection Cadence**:
The schedule for generating and delivering Learning Memory proposals from evidence.
_Avoid_: message-per-event spam

**Compact Reflection Notice**:
A Telegram review message that summarizes proposed learning and links to evidence without dumping raw forensic content by default.
_Avoid_: full evidence dump

**Forensic Recall**:
An explicit recall mode for proof, replay, debugging, and task audit that includes evidence memory.
_Avoid_: default recall

## Relationships

- The **OpenClaw Coordinator** assigns work to one or more **OpenClaw Gateways**.
- An **OpenClaw Gateway** writes durable memory and audit records to the **Memory Evidence Plane**.
- **Federation Proof** is required before an **OpenClaw Gateway** is considered available for shared swarm work.
- A **Canonical Memory Write** must be immediately queryable by the **Memory Evidence Plane**.
- A **Completion-Proof Write** blocks completion if it fails.
- A **Telemetry Memory Write** may retry or degrade without blocking completion.
- The **Embedding Contract** uses `EMBEDDING_API_BASE`, `EMBEDDING_MODEL`, and `EMBEDDING_DIMS` as canonical env vars.
- **Core API Readiness** does not require NATS, Temporal, or a hosted embedding service.
- **Federation Readiness** requires **Core API Readiness** plus Railway NATS/JetStream and searchable gateway proof.
- A **Memory Action Eval** must prove that recall changes later behavior, not just that memory can answer a question.
- An **Async Memory Workflow** may enrich, compact, or audit memory, but must not replace a **Canonical Memory Write** until it preserves the same schema.
- **Evidence Memory** records execution proof before interpretation.
- **Learning Memory** is distilled from one or more **Evidence Memories** after review or consolidation.
- A **Learning Memory** proposed by reflection enters the **Reflection Review Window** before default recall integration.
- The initial **Reflection Review Surface** is Telegram through OpenClaw; canonical review state remains in fumemory.
- **Reflection Cadence** has task-close reflection for meaningful completions and idle/dream reflection for cross-task patterns.
- Telegram uses a **Compact Reflection Notice** with actions to inspect full evidence through fumemory.
- Default recall prioritizes **Learning Memory**; **Forensic Recall** includes **Evidence Memory** when proof or replay is needed.

## Example dialogue

> **Dev:** "Should memU decide which gateway owns this task?"
> **Domain expert:** "No. OpenClaw owns task coordination. fumemory stores recall, leases, and proof that the gateway executed or can join the federation."

> **Dev:** "Can the OpenClaw hook write through Temporal by default?"
> **Domain expert:** "No. The hook should use the canonical synchronous write so evidence is searchable immediately. Temporal can process memory later if it preserves the same fields."

> **Dev:** "Should a failed memory write block task completion?"
> **Domain expert:** "Only if it is completion proof. Required evidence writes block completion; low-risk telemetry writes can retry or degrade."

> **Dev:** "Can we switch embedding dimensions by dropping and recreating vector columns?"
> **Domain expert:** "No. Production embeddings are versioned. Dimension changes add a new embedding version or column and reindex forward without destructive rewrites."

> **Dev:** "Should missing NATS or Temporal fail the API deploy?"
> **Domain expert:** "No. Core API readiness proves API, Postgres, auth, canonical write, and recall. Federation readiness separately proves Railway NATS/JetStream and gateway memory proof."

> **Dev:** "What should the first memory benchmark prove?"
> **Domain expert:** "It should prove behavior change across sessions. Session one produces evidence from a real fumemory mistake, reflection turns it into learning, and session two avoids the same class of mistake because recall surfaced that learning."

> **Dev:** "Should every tool call become a lesson?"
> **Domain expert:** "No. Tool calls are evidence. A lesson is created only when those records reveal reusable guidance."

> **Dev:** "Should normal recall include raw execution evidence?"
> **Domain expert:** "No. Normal recall should surface learning. Use forensic recall when the agent needs proof, replay, debugging, or audit history."

> **Dev:** "Does every reflected lesson wait forever for manual approval?"
> **Domain expert:** "No. The user gets a six-hour review window to approve, deny, or edit it. If no action is taken, it is integrated automatically, and later feedback can still supersede it."

> **Dev:** "Where should the user see reflection proposals?"
> **Domain expert:** "Telegram is the initial surface because OpenClaw uses Telegram for messaging, but fumemory still owns the durable review queue."

> **Dev:** "Should the Telegram message include all source evidence?"
> **Domain expert:** "No. Telegram gets a compact notice with summary, confidence, risk flags, source task/session, expiry, and approve/deny/edit/inspect actions. Full evidence stays behind forensic recall."

> **Dev:** "Should every small reflection send a Telegram message?"
> **Domain expert:** "No. High-value learning can notify immediately, but routine proposals are batched into Telegram digests. Each digest item still keeps its own six-hour review window."

## Flagged ambiguities

- "control plane" was used for both OpenClaw and fumemory. Resolved: OpenClaw is the coordinator; fumemory is the memory/evidence plane until its Railway, migration, and schema contracts are stable.
- "memory write" was used for both immediate evidence writes and background workflows. Resolved: OpenClaw defaults to **Canonical Memory Write**; **Async Memory Workflow** is optional enrichment until schema parity is proven.
- "evidence write" was used as if all writes had the same criticality. Resolved: **Completion-Proof Write** is blocking; **Telemetry Memory Write** is non-blocking.
- "embedding base URL" was used inconsistently. Resolved: `EMBEDDING_API_BASE` is canonical; `EMBEDDING_BASE_URL` is a temporary compatibility alias only.
- "readiness" was used for both API health and swarm federation. Resolved: **Core API Readiness** and **Federation Readiness** are separate gates.
- "memory eval" was used as if recall Q&A were enough. Resolved: **Memory Action Eval** measures whether memory changes later OpenClaw behavior across sessions.
- "memory" was used for both audit proof and reusable knowledge. Resolved: **Evidence Memory** is immutable execution proof; **Learning Memory** is distilled reusable knowledge.
- "recall" was used for both reusable guidance and audit replay. Resolved: default recall returns **Learning Memory**; **Forensic Recall** explicitly includes **Evidence Memory**.
- "review" was used as if learning promotion were always manual and indefinite. Resolved: reflected **Learning Memory** has a six-hour **Reflection Review Window** before automatic integration unless the user approves, denies, or edits it sooner.
- "review surface" was used as if the notification channel owned the workflow. Resolved: Telegram is the initial **Reflection Review Surface** through OpenClaw, while fumemory remains the source of truth for review state.
- "notification" was used as if Telegram should contain the whole proof bundle. Resolved: Telegram sends a **Compact Reflection Notice** and delegates full proof inspection to **Forensic Recall**.
- "reflection" was used as if every event creates immediate user noise. Resolved: **Reflection Cadence** batches routine proposals and only sends immediate notices for high-risk or high-value learning.
