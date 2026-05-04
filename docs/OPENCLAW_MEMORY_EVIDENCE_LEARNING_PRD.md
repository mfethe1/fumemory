# OpenClaw Memory Evidence and Learning PRD

## Problem Statement

fumemory currently has the right building blocks for durable agent memory: synchronous writes, semantic and lexical search, graph-lite relationships, temporal validity, NATS federation proof, Railway deployment checks, and an idle memory agent. The architecture is not yet strict enough for OpenClaw's next memory network evolution.

The main ambiguity is that "memory" is doing too many jobs. Raw execution proof, reusable lessons, search history, task evidence, OpenClaw hook logs, graph relationships, and background compactions can all be treated as equivalent recall material. That creates three risks:

- OpenClaw can retrieve noisy execution traces when it only needs reusable guidance.
- Evidence can be rewritten, compacted, or hidden before it has served its audit purpose.
- Async memory workflows can appear equivalent to canonical writes even when they do not preserve schema parity or immediate searchability.

The target architecture must keep OpenClaw as the top-level coordinator while making fumemory the Memory Evidence Plane: a reliable, schema-first service for durable memory writes, evidence retention, recall, federation proof, and learning distillation.

## Solution

Evolve fumemory into a schema-first Memory Evidence Plane with two first-class memory kinds:

- **Evidence Memory**: immutable, task-bound execution proof written synchronously by OpenClaw gateways. Evidence records what happened, when it happened, which gateway and agent performed it, which task/session it belongs to, and which artifacts or tool outputs prove it.
- **Learning Memory**: distilled, reusable knowledge derived from one or more Evidence Memory records. Learning is optimized for normal agent recall and should carry provenance back to source evidence.

OpenClaw remains responsible for task routing, gateway selection, agent orchestration, and completion decisions. fumemory receives canonical evidence writes, returns learning-oriented recall by default, supports forensic recall when proof is needed, and exposes deployment verification gates that prove the Railway-backed services are actually usable.

The critical behavioral contract is:

- OpenClaw canonical memory writes use synchronous `/api/v1/memu/add`.
- `/memories/async` remains optional until it accepts the same schema fields, preserves the same validation semantics, and provides equivalent idempotency and provenance guarantees.
- Canonical evidence idempotency is scoped by `(tenant_id, idempotency_key)`. An exact replay returns the original memory ID; a replay with the same key and a different canonical payload hash fails with `409 Conflict`.
- Evidence Memory is never content-hash deduped across distinct task, session, gateway, or event records. Content similarity may help identify related evidence, but it must not rewrite or merge immutable proof.
- Canonical evidence writes are classified by criticality. Completion-proof writes block task completion or gateway readiness if they fail. Low-risk telemetry writes may retry, queue, or degrade without blocking completion.
- Embedding configuration uses one canonical contract: `EMBEDDING_API_BASE`, `EMBEDDING_MODEL`, and `EMBEDDING_DIMS`. `EMBEDDING_BASE_URL` is a temporary compatibility alias only. Production defaults to an OpenAI-compatible `text-embedding-3-small` / `1536` contract unless a hosted Ollama embedding service is explicitly chosen.
- Embedding dimension changes must be versioned and non-destructive. Future migrations add a new embedding version or column and reindex forward instead of dropping existing vectors.
- Default recall returns Learning Memory.
- Explicit Forensic Recall returns Evidence Memory plus provenance for replay, debugging, audit, and completion review.
- Reflected Learning Memory enters a six-hour user review window before automatic default recall integration. During the window, the user can approve, deny, or edit the proposal. Telegram is the initial user-facing review surface because OpenClaw uses Telegram for messaging, but fumemory owns the durable review queue and state transitions. If no action is taken, the proposal is integrated automatically and remains feedback-correctable afterward.

This is an evolution of fumemory's existing API, storage, graph, temporal, NATS, and memory-agent modules. It is not a wholesale replacement with Graphiti, Zep, Hindsight, LangMem, Cognee, or Mem0. Those systems provide useful patterns, but fumemory should keep the OpenClaw-specific evidence plane contract and use external prior art selectively.

## Architecture

```text
OpenClaw Coordinator
        |
        | assigns work, owns routing, owns task state
        v
OpenClaw Gateway / Agent Runtime
        |
        | canonical sync evidence write
        v
+------------------------------+
| fumemory Memory Evidence API |
| - schema validation          |
| - idempotency                |
| - auth and tenant scope      |
| - immediate searchability    |
+---------------+--------------+
                |
                v
+------------------------------+
| Memory Store                 |
| - Evidence Memory append log |
| - Learning Memory records    |
| - source evidence links      |
| - temporal validity          |
| - vector, lexical, graph idx |
+-----+------------------+-----+
      |                  |
      |                  | background reflection
      |                  v
      |          Reflection Worker
      |          - clusters evidence
      |          - distills learning
      |          - retains sources
      |
      v
Recall Service
- default learning recall
- explicit forensic recall
- vector + lexical + graph + temporal fusion
      |
      v
OpenClaw prompt/context injection

Railway services:
API + Postgres/pgvector + NATS/JetStream + optional Temporal + optional embedding service
```

Railway service contracts:

- **api**: required public HTTP service. Depends on Postgres and `MEMU_API_KEY`. Provides health, canonical write, recall, forensic recall, and review queue APIs.
- **postgres-pgvector**: required private database service. Stores memories, evidence, learning, review queue, vector indexes, lexical indexes, and migration state.
- **nats-jetstream**: required only for federation readiness. Private inside Railway, with TCP proxy only when external OpenClaw gateways need to connect.
- **temporal-worker**: optional service for Async Memory Workflow. Missing Temporal must not fail Core API Readiness.
- **embedding-service**: optional service when not using an external OpenAI-compatible embedding provider. Missing embeddings may degrade semantic recall but must not hide lexical recall status.

Readiness gates:

- **Core API Readiness**: API + Postgres + auth + migration status + canonical write + immediate recall.
- **Federation Readiness**: Core API Readiness + Railway NATS/JetStream + gateway publish/consume + directed response + idempotency replay + searchable memory proof.

The deep modules should be:

- **Memory Schema Contract**: the stable public shape for evidence writes, learning records, recall requests, and recall responses.
- **Canonical Write Service**: one synchronous path that validates, deduplicates, persists, indexes, and returns a stored memory ID before OpenClaw proceeds.
- **Evidence Criticality Policy**: one small policy surface that classifies writes as completion-proof or telemetry and defines blocking, retry, and degradation behavior.
- **Recall Fusion Service**: one interface for default learning recall and forensic recall, hiding vector, lexical, graph, and temporal scoring internals.
- **Embedding Provider Contract**: one configuration and migration boundary that resolves providers, validates vector dimensions, exposes compatibility aliases, and prevents destructive dimension rewrites.
- **Reflection Worker**: one background interface that reads immutable evidence, emits derived learning, and records source evidence IDs.
- **Reflection Scheduler**: one cadence policy that runs task-close reflection after meaningful completions and idle/dream reflection for cross-task patterns, while batching routine Telegram delivery.
- **Reflection Review Queue**: one lifecycle interface that stores proposed Learning Memory summaries, tracks the six-hour review window, applies approve/deny/edit decisions, auto-integrates expired proposals, and records later feedback as superseding learning.
- **Telegram Review Notifier**: one OpenClaw-facing notification adapter that sends proposed Learning Memory summaries to Telegram and maps Telegram actions back to the Reflection Review Queue.
- **Railway Verification Gate**: one deployable verifier that proves health, sync write, immediate retrieval, NATS federation, and optional async workflows only when enabled.
- **Legacy Memory Classifier**: one migration/backfill interface that assigns `memory_kind`, review state, and recall eligibility to existing rows without hiding useful historical memories or leaking raw evidence into default recall.
- **Memory Action Eval Harness**: one multi-session evaluation harness that proves evidence becomes learning and later OpenClaw behavior changes because recall surfaced that learning.

## User Stories

1. As an OpenClaw coordinator, I want fumemory to store execution proof without taking over task routing, so that coordination authority stays in OpenClaw.
2. As an OpenClaw gateway, I want a synchronous canonical write endpoint, so that evidence is persisted and searchable before I claim work is complete.
3. As an OpenClaw gateway, I want canonical writes to be idempotent, so that retries do not create duplicate evidence.
4. As an OpenClaw gateway, I want evidence writes to include gateway ID, agent ID, task ID, session ID, event type, timestamps, and source references, so that later audits can reconstruct what happened.
5. As an OpenClaw gateway, I want canonical writes to fail loudly on schema mismatch, so that bad evidence does not silently enter the memory network.
6. As an OpenClaw gateway, I want completion-proof evidence writes to block completion when they fail, so that no task is marked done without durable proof.
7. As an OpenClaw gateway, I want low-risk telemetry writes to retry or degrade without blocking completion, so that observability gaps do not halt unrelated work.
8. As an OpenClaw gateway, I want `/memories/async` to be optional, so that core evidence capture does not depend on Temporal being deployed.
9. As an OpenClaw gateway, I want async memory workflows to preserve canonical schema parity before becoming trusted, so that background processing cannot drop provenance.
10. As an OpenClaw agent, I want default recall to return distilled learning, so that prompt context is concise and reusable.
11. As an OpenClaw agent, I want default recall to avoid raw execution traces, so that ordinary reasoning is not polluted by noisy logs.
12. As an OpenClaw agent, I want learning recall injected before task execution, so that I can avoid repeating known mistakes.
13. As an OpenClaw agent, I want recall results to include compact provenance summaries, so that I can judge confidence without reading full evidence.
14. As an OpenClaw reviewer, I want Forensic Recall to include Evidence Memory, so that I can verify task completion claims.
15. As an OpenClaw reviewer, I want forensic results linked to task IDs and artifacts, so that review can follow proof instead of relying on summaries.
16. As an OpenClaw operator, I want evidence memory to be immutable, so that execution proof remains trustworthy after learning distillation.
17. As an OpenClaw operator, I want corrections to be append-only records, so that mistaken evidence is superseded without being erased.
18. As an OpenClaw operator, I want learning memory to link to source evidence IDs, so that every lesson can be traced back to real execution.
19. As an OpenClaw operator, I want learning memory to carry temporal validity, so that outdated guidance can be superseded without deleting history.
20. As an OpenClaw operator, I want memory kind to be separate from semantic memory type, so that "evidence versus learning" is not confused with "decision, failure, lesson, action, or external."
21. As a Railway deployer, I want explicit service contracts for API, Postgres, NATS, Temporal, and embeddings, so that each service shape can be verified independently.
22. As a Railway deployer, I want private service networking for internal dependencies, so that Postgres, NATS, Temporal, and embeddings are not unnecessarily exposed.
23. As a Railway deployer, I want verification gates for health, sync write, immediate recall, and NATS federation, so that deployment readiness is based on proof.
24. As a Railway deployer, I want async verification to run only when Temporal is part of the release contract, so that optional services do not create false deployment failures.
25. As a memory engineer, I want one canonical embedding env contract, so that local, Railway, API, worker, and docs use the same provider settings.
26. As a memory engineer, I want embedding dimensions to be versioned, so that model changes do not destroy existing searchable memory.
27. As a memory engineer, I want retrieval to fuse vector, lexical, graph, and temporal signals, so that recall works when any single retrieval mode is weak.
28. As a memory engineer, I want lexical fallback to remain available, so that embeddings outages degrade quality instead of breaking recall.
29. As a memory engineer, I want graph relationships to improve recall ranking, so that related evidence and learning can be found through entity and task links.
30. As a memory engineer, I want temporal validity to affect ranking and filtering, so that current learning is preferred but historical proof remains available.
31. As a memory engineer, I want search mode to be explicit, so that default learning recall and forensic recall have different filters and output shapes.
32. As a reflection worker, I want to read immutable evidence and write derived learning, so that reusable knowledge is created outside the hot execution path.
33. As a reflection worker, I want to retain source evidence IDs on every learning record, so that distillation remains auditable.
34. As a reflection worker, I want to mark generated learning as proposed, accepted, accepted by timeout, superseded, or rejected, so that low-quality lessons have a visible lifecycle before and after default recall integration.
35. As a reflection scheduler, I want task-close reflection to run after meaningful completions, so that important task evidence becomes learning promptly.
36. As a reflection scheduler, I want idle/dream reflection to find cross-task patterns, so that repeated successes and failures become broader learning.
37. As a Telegram-based OpenClaw user, I want routine reflection proposals batched into digests, so that memory review does not flood the messaging channel.
38. As a Telegram-based OpenClaw user, I want high-risk or high-value learning to notify immediately, so that urgent corrections do not wait for a digest.
39. As a user, I want a summary of proposed reflected learning before it enters default recall, so that I can approve, deny, or improve the system's learning.
40. As a user, I want six hours to respond to a proposed learning item, so that routine reflections can integrate automatically without blocking the memory system forever.
41. As a user, I want to provide feedback after auto-integration, so that late corrections can supersede or refine learning that was accepted by timeout.
42. As a Telegram-based OpenClaw user, I want reflection proposals to arrive in Telegram with approve, deny, and edit actions, so that I can review memory without leaving my normal command channel.
43. As a Telegram-based OpenClaw user, I want Telegram actions to update fumemory's review queue, so that the message channel is convenient but not the source of truth.
44. As a completion reviewer, I want task completion review to require memory proof, so that "done" means deliverable plus evidence plus evaluation.
45. As a federation operator, I want each gateway to prove Railway NATS publish and consume, so that shared swarm readiness is verifiable.
46. As a federation operator, I want each gateway to write searchable memory proof after federation smoke, so that NATS readiness and memory readiness are tied together.
47. As a security reviewer, I want memory writes to be sanitized and scoped, so that prompt injection content is not promoted into trusted learning.
48. As a security reviewer, I want evidence retention separate from learning injection, so that untrusted raw tool output can be audited without being automatically placed into prompt context.
49. As an evaluator, I want a multi-session benchmark where a real fumemory failure becomes later learning, so that memory quality is judged by changed behavior instead of recall trivia.
50. As an evaluator, I want the first benchmark to use embedding env mismatch or Railway readiness confusion, so that the eval protects against known project risks.
51. As an evaluator, I want the second session trace to prove recall changed the agent's actions, so that passing the benchmark requires useful memory, not just a stored note.
52. As a future maintainer, I want the architecture to use existing fumemory modules where possible, so that the refactor improves contracts without replacing working infrastructure.

## Implementation Decisions

- Add `memory_kind` as the primary schema discriminator. Initial values are `evidence` and `learning`. Existing `memory_type` remains a semantic taxonomy for values such as `decision`, `lesson`, `failure`, `user_action`, `external`, and `procedural`.
- Treat Evidence Memory as append-only. Updates to evidence content are not allowed. Corrections, invalidations, or supersession are represented as new records with explicit relationships to the original evidence.
- Treat Learning Memory as derived. A learning record must carry source evidence references, derivation metadata, creation timestamp, review status, and optional validity window.
- Add a canonical payload hash for evidence writes. The hash is computed from normalized evidence fields, excluding transport-only fields. It is used only to validate idempotent replay, not to merge different evidence records.
- Enforce a unique partial index on `(tenant_id, idempotency_key)` for records with an idempotency key. Exact replay returns the existing row; same key with a different canonical payload hash returns `409 Conflict`.
- Disable content-hash dedupe for Evidence Memory unless the duplicate has the same idempotency key and canonical payload hash. Keep content-hash or semantic dedupe available for Learning Memory where merging is explicitly allowed.
- Backfill legacy rows with deterministic rules. Existing `lesson`, `decision`, `pattern`, `procedural`, `fact`, `reflection`, `plan`, and `goal` records become `learning` with `review_status=legacy`. Existing `user_action`, `external`, search/tool records, and records with OpenClaw task/session/gateway/event metadata become `evidence`. Ambiguous `observation` rows become `learning` only when they lack OpenClaw execution metadata; otherwise they become `evidence`.
- Default recall includes `accepted` and `legacy` Learning Memory during migration so historical useful memory does not disappear. Operators can later require only `accepted` learning after enough legacy review coverage exists.
- Extend canonical write validation around OpenClaw evidence fields: `task_id`, `session_id`, `gateway_id`, `agent_id`, `event_type`, `event_at`, `ingested_at`, `source`, `source_ref`, `idempotency_key`, `artifact_refs`, `criticality`, and structured metadata.
- Define evidence write criticality values. `completion_proof` is required for task completion, review approval, federation proof, and gateway readiness. `telemetry` is non-blocking and may be retried or queued. Unknown criticality defaults to `completion_proof` when the write is attached to a completion/review/federation event and `telemetry` only for explicitly low-risk activity logs.
- Completion-proof write failure must return a visible error to OpenClaw and prevent OpenClaw from marking the related operation complete until proof is written or explicitly waived by a human/operator override. Waivers are evidence records too.
- Telemetry write failure must be recorded in local logs or a retry queue with bounded retry/backoff. It must not silently pretend the write succeeded.
- Keep `/api/v1/memu/add` as the OpenClaw canonical write path. The endpoint should create immediately queryable evidence and return a durable ID only after persistence succeeds.
- Keep `/memories` as the native API shape if useful, but align compatibility behavior so `/api/v1/memu/add` is not a lossy adapter for OpenClaw.
- Move OpenClaw hook writes from async-first behavior to canonical synchronous evidence writes. Hook failures should be visible to the caller or operator when the write is required for completion proof.
- Keep `/memories/async` and Temporal routes optional until they accept all canonical fields, preserve idempotency keys, retain source evidence linkage, and expose equivalent validation errors.
- Implement recall modes explicitly. `learning` is the default mode. `forensic` must be requested and must return evidence records with replay-grade provenance.
- Default learning recall should filter to current `accepted` Learning Memory and migration-safe `legacy` Learning Memory. Any looser policy requires explicit operator configuration and visible diagnostics.
- Forensic recall should support task/session/gateway/agent filters, event type filters, time windows, and source artifact references.
- Define Forensic Recall as a distinct request/response contract. Requests must support `task_id`, `session_id`, `gateway_id`, `agent_id`, `event_type`, `time_window_start`, `time_window_end`, `artifact_ref`, `limit`, `cursor`, and `include_content`. Responses must include evidence ID, memory kind, memory type, content or redacted content marker, event metadata, source references, artifact refs, tenant/role visibility metadata, provenance links, pagination cursor, and redaction reason when content is withheld.
- Forensic Recall may redact raw content for tenant, role, or safety reasons, but it must still return enough replay proof to audit the event: evidence ID, event type, actor, task/session/gateway, timestamp, source ref, artifact refs, and redaction reason.
- Keep tenant and role filtering mandatory for both learning and forensic recall. Filtering happens before ranking and before prompt/context injection.
- Retrieval ranking should combine semantic vector similarity, lexical matching, graph proximity, temporal validity, salience, access reinforcement, and role or tenant constraints.
- Existing Graph-Lite relationships should be used as the near-term graph layer. Apache AGE or a richer graph engine can remain an optimization path, not a prerequisite for the PRD.
- Use Graphiti/Zep as design inspiration for temporal knowledge graph behavior: preserve history, model entity relationships, and make time first-class. Do not replace fumemory with Graphiti or Zep.
- Use Hindsight as design inspiration for separating raw experiences, summaries, world facts, and evolving beliefs. Do not import its memory network wholesale without proving it maps to OpenClaw evidence and learning kinds.
- Use LangMem/LangGraph as design inspiration for long-term memory as explicit workflow state and memory operations. Do not make LangGraph the required orchestrator because OpenClaw remains the coordinator.
- Use Cognee OpenClaw as design inspiration for plugin lifecycle hooks, auto-index, auto-recall, graph search, and hash-based change detection. Prefer fumemory's canonical API over file-sync as the source of truth for OpenClaw execution proof.
- Use Mem0 OpenClaw as design inspiration for turn-level auto-capture and auto-recall outside the context window. Do not adopt opaque extraction as the canonical evidence write because evidence must be schema-first and immutable.
- Use MemoryArena-style evaluation as design inspiration: multi-session tasks must require earlier actions and feedback to be distilled into memory and used in later sessions.
- Preserve tenant and role filtering in recall. Access control must be applied before retrieval results are injected into OpenClaw context.
- Make prompt injection handling part of evidence-to-learning promotion. Raw evidence may be retained for audit while unsafe or unreviewed content is excluded from default recall.
- Reflection Worker output starts as `proposed` and enters the Reflection Review Queue. The queue sends a concise summary, source evidence links, confidence, risk flags, and proposed canonical text to the user.
- Reflection runs in two modes. Task-close reflection runs after meaningful task completion and emits at most 1-3 proposed Learning Memories from that task's evidence. Idle/dream reflection runs on a schedule and looks for cross-task patterns, repeated failures, repeated wins, and stale learning candidates.
- Reflection delivery uses digests by default. High-risk or high-value proposals can notify immediately; routine proposals are batched every 2-4 hours and capped by a configurable daily maximum unless the user requests more.
- Each proposal in a digest keeps its own six-hour review window based on when the digest item is delivered or made visible in the review queue.
- Reflection Scheduler settings must include `task_close_enabled`, `idle_enabled`, `digest_interval_hours`, `max_digest_items`, `max_daily_notifications`, and thresholds for immediate high-risk/high-value delivery.
- The first Reflection Review Surface is Telegram via OpenClaw. Telegram messages must include the proposal summary, confidence, risk flags, source evidence references, expiry time, and actions for approve, deny, and edit.
- Telegram review messages must be compact by default. They include proposed learning summary, confidence, risk flags, source task/session, expiry timestamp, and actions for approve, deny, edit, and inspect evidence. They do not include raw forensic evidence content unless the user explicitly requests inspection.
- The inspect evidence action should route through fumemory Forensic Recall or an equivalent OpenClaw command that fetches full source evidence with tenant/role filtering and redaction.
- Telegram is a notification and action surface, not the canonical state store. All actions are persisted through the Reflection Review Queue before any proposal state changes.
- If Telegram delivery fails, the proposal remains pending in the Reflection Review Queue and still follows the six-hour timeout policy. Delivery failure is recorded as operational evidence and surfaced in diagnostics.
- The Reflection Review Queue keeps each proposal in a six-hour review window. User actions are `approve`, `deny`, or `edit`. Approval integrates the proposed Learning Memory immediately. Denial marks it `rejected`. Edit stores the user-edited text as the accepted Learning Memory while preserving the original proposal and evidence links.
- If no user action occurs within six hours, the proposal is auto-integrated as `accepted_by_timeout`. This status is eligible for default recall but remains distinguishable from explicit user approval.
- Late feedback after auto-integration creates a superseding Learning Memory or rejection record rather than mutating the integrated memory in place.
- Trusted reviewer roles, explicit OpenClaw review decisions, or configured policy workers may still promote or reject high-confidence proposals early, but those actions must write the same audit trail as a user action.
- Looser default recall policies require operator configuration and must be visible in health/diagnostic output.
- Every learning promotion, rejection, supersession, or rollback writes an audit event that references the learning ID, source evidence IDs, actor or policy worker ID, decision reason, and timestamp.
- Keep Railway service contracts explicit: API requires Postgres and API key; NATS is required for federation proof; Temporal is required only for async workflows; embeddings may be hosted or OpenAI-compatible but must match configured dimensions.
- Split Railway topology into explicit service contracts: `api`, `postgres-pgvector`, `nats-jetstream`, optional `temporal-worker`, and optional `embedding-service`.
- `api` is the only required public HTTP surface. Postgres, Temporal, and embedding services should use Railway private networking. NATS should use Railway private networking for in-project consumers and TCP proxy only when external OpenClaw gateways must connect.
- Core API Readiness must not require NATS, Temporal, or an embedding service. It must prove API health, DB connectivity, auth, migration status, canonical evidence write, and immediate lexical or semantic recall.
- Federation Readiness must require Core API Readiness plus Railway NATS/JetStream, gateway publish/consume, directed response, idempotency replay, and searchable memory proof.
- Async workflow readiness is a separate optional check. It runs only when `TEMPORAL_HOST` or an explicit async flag says Temporal is in scope.
- Standardize embedding environment variables on `EMBEDDING_API_BASE`, `EMBEDDING_MODEL`, and `EMBEDDING_DIMS`. Keep `EMBEDDING_BASE_URL` as a logged compatibility alias for one migration window, then remove it from docs and runtime startup examples.
- Production Railway defaults should use an OpenAI-compatible embedding endpoint with `text-embedding-3-small` and `1536` dimensions unless the deployment explicitly provisions a hosted Ollama embedding service and selects the matching model/dimensions.
- Store embedding provider, model, dimensions, and embedding version with each vector-producing record or index version. Retrieval must filter or route by compatible embedding version instead of casting incompatible vectors.
- Future embedding model or dimension changes require additive migrations: new vector column, vector table, or embedding version plus background reindex. Migrations must not drop or recreate production embedding columns as the primary migration path.
- Startup verification should fail loud when configured `EMBEDDING_DIMS` does not match the active vector schema or embedding version. If embeddings are unavailable, lexical recall may degrade but semantic recall must report degraded status.
- Use Railway private networking for API-to-Postgres, API-to-NATS, API-to-Temporal, and API-to-embedding service calls where available. Public endpoints are for intended external API access only.
- Require deployment verification to prove health, sync write, immediate lexical or semantic retrieval, default recall behavior, and optional async behavior only when enabled.
- Require federation verification to prove Railway NATS connection, publish, consume, directed response, idempotency, and searchable memory proof before a gateway is marked swarm-ready.
- Preserve the control-plane boundary in all verification flows. fumemory emits proof artifacts and readiness facts; OpenClaw or a human operator decides whether a task is complete or a gateway is available for shared swarm work.
- Split verification into two named gates. Core API readiness requires health, authenticated canonical write, immediate retrieval, and migration status. Federation readiness additionally requires Railway NATS config, JetStream connection, publish/consume, directed response, idempotency replay proof, searchable memory proof, and OPA/subject-policy proof when policy enforcement is enabled.
- Verification tools must read `MEMU_API_URL`, `MEMU_API_KEY`, `NATS_RAILWAY_URL`, `NATS_AUTH_TOKEN`, `GATEWAY_ID`, optional `TEMPORAL_HOST`, and optional embedding provider settings from environment or explicit flags. They must output a machine-readable proof artifact with timestamp, target URLs with secrets redacted, check statuses, and evidence memory IDs.
- The first Memory Action Eval should use a real fumemory failure mode: embedding environment mismatch or Railway readiness confusion. The eval must run across at least two sessions and fail if the second session does not change behavior because of recalled Learning Memory.
- Memory Action Eval flow: session one observes or makes the mistake, writes evidence, reflection proposes learning, review accepts or timeout-integrates it, session two faces a similar task, default recall surfaces the learning, and the agent avoids the same class of mistake.
- Memory Action Eval pass criteria must include evidence IDs from session one, learning ID and source links, review state, recall result in session two, and behavioral trace showing the agent took a different action because of recall.

## Testing Decisions

- Tests should verify external behavior and service contracts, not internal scoring formulas or implementation details.
- Add schema contract tests for canonical evidence writes. Good tests should reject missing required OpenClaw fields, reject legacy or ambiguous payloads where the contract requires canonical fields, and accept valid evidence with a stable memory ID.
- Add idempotency tests for Evidence Memory. Good tests should prove exact replay returns the same ID, same key with different payload returns `409`, and two distinct task/session/gateway events with identical content remain separate evidence records.
- Add evidence criticality tests. Good tests should prove completion-proof write failure blocks completion, telemetry write failure queues/retries without marking proof present, unknown criticality resolves safely, and human/operator waivers create audit evidence.
- Add migration/backfill tests for `memory_kind`. Good tests should classify legacy lessons and decisions as learning, OpenClaw tool/search/action rows as evidence, ambiguous observations according to metadata, and verify default recall does not hide legacy learning or leak evidence.
- Add immutability tests for Evidence Memory. Good tests should prove evidence content cannot be overwritten and that corrections create linked records instead.
- Add learning derivation tests for the reflection worker. Good tests should seed evidence records, run a bounded reflection pass, and assert that learning records link back to source evidence.
- Add reflection review lifecycle tests. Good tests should prove reflection output starts as `proposed`, the user can approve/deny/edit within six hours, timeout auto-integrates as `accepted_by_timeout`, rejected proposals do not enter default recall, edited proposals preserve source evidence, and late feedback creates a superseding record.
- Add reflection cadence tests. Good tests should prove task-close reflection caps proposals per task, idle/dream reflection can find cross-task patterns, high-risk proposals bypass digest delay, routine proposals batch into digests, daily notification caps are respected, and each digest item keeps an independent six-hour review window.
- Add Telegram review notifier tests. Good tests should prove a proposal creates a compact Telegram-ready message payload, approve/deny/edit/inspect actions route correctly, raw evidence is omitted by default, delivery failure does not lose the proposal, and timeout behavior remains queue-driven.
- Add default recall tests. Good tests should seed both evidence and learning, call recall without forensic mode, and assert that only Learning Memory is returned.
- Add forensic recall tests. Good tests should request forensic mode and assert that relevant evidence is returned with task/session/gateway provenance, pagination, role filtering, artifact refs, and redaction reasons when content is withheld.
- Add retrieval fusion tests at the behavior level. Good tests should cover semantic hits, lexical fallback when embeddings are unavailable, graph relationship boosting, and temporal filtering without asserting exact private weights.
- Add embedding contract tests. Good tests should prove `EMBEDDING_API_BASE` is canonical, `EMBEDDING_BASE_URL` is only a compatibility alias with a warning, model/dimension mismatch fails verification, and additive embedding-version migration preserves existing vectors.
- Add OpenClaw hook contract tests. Good tests should verify that hooks call synchronous `/api/v1/memu/add` for canonical evidence and fail visibly when required evidence cannot be stored.
- Add async parity tests before promoting `/memories/async`. Good tests should send the same canonical payload to sync and async paths and compare persisted fields, idempotency behavior, and recall visibility.
- Add Railway deployment smoke tests that extend the existing verifier: health, sync write, immediate search, default learning recall, forensic recall, and optional async checks only when Temporal is deployed.
- Add NATS federation proof tests that bind gateway readiness to memory proof: publish/consume succeeds and the related marker is immediately searchable.
- Add verification artifact tests. Good tests should assert that core readiness can pass without NATS, federation readiness fails without Railway NATS, and proof output redacts secrets while preserving evidence IDs and check statuses.
- Add Railway topology tests or config validation. Good tests should prove root API config does not silently conflate the API, NATS, Temporal, and embedding service contracts, and that optional services are checked by their own gates.
- Add MemoryArena-style multi-session action tests. Good tests should require one session to produce evidence, a background pass to distill learning, and a later session to solve or avoid a repeated failure by recalling that learning.
- Add the first concrete Memory Action Eval for either embedding env mismatch or Railway readiness confusion. Good tests should prove the second session reads the accepted learning and avoids the original failure path.
- Keep unit coverage around deep modules: schema contract, canonical write service, recall fusion service, reflection worker, and verification gate.
- Keep integration coverage around public endpoints, database migrations, Railway verification scripts, NATS federation, and OpenClaw hook behavior.

## Out of Scope

- Replacing OpenClaw as the top-level coordinator.
- Turning fumemory into an authoritative task control plane.
- Replacing fumemory with Graphiti, Zep, Hindsight, LangMem, Cognee, Mem0, or another memory product.
- Making `/memories/async` the default OpenClaw write path before schema parity is proven.
- Deleting or compacting away source Evidence Memory.
- Exposing raw Evidence Memory in default recall.
- Building a full graph database migration as a prerequisite for the first implementation.
- Reworking all existing memory types beyond separating `memory_kind` from `memory_type`.
- Building a new UI for forensic exploration in this PRD.
- Changing OpenClaw task routing, gateway ownership, or agent execution policy.
- Deploying public unauthenticated Railway NATS, Postgres, Temporal, or embedding services.
- Making fumemory's verification result itself the authority for task completion or gateway scheduling. It only emits proof for OpenClaw or an operator to decide against.

## Acceptance Criteria

- Canonical OpenClaw evidence writes are synchronous, schema-validated, idempotent by `(tenant_id, idempotency_key)`, and immediately retrievable.
- Completion-proof evidence write failures block task completion, review approval, federation proof, or gateway readiness until proof exists or a human/operator waiver is recorded.
- Telemetry write failures are retried, queued, or reported as degraded without being confused with completion proof.
- Embedding configuration uses `EMBEDDING_API_BASE`, `EMBEDDING_MODEL`, and `EMBEDDING_DIMS` consistently across API, workers, Railway docs, and local examples.
- Embedding dimension changes are additive and versioned; no production migration drops existing embedding columns as its primary strategy.
- Evidence Memory is append-only and cannot be content-deduped across distinct task/session/gateway events.
- Legacy memories are backfilled into `learning` or `evidence` by documented deterministic rules, and default recall continues to surface useful legacy learning.
- Default recall returns Learning Memory only, except for explicitly configured legacy migration policy; Forensic Recall returns Evidence Memory plus replay-grade provenance.
- Reflection Worker output cannot enter default recall until it is `accepted` or explicitly allowed by a visible operator policy.
- Reflected Learning Memory sends a summary to the user and remains editable or rejectable for six hours before automatic integration.
- Reflection runs after meaningful task completion and on idle/dream schedule; routine Telegram delivery is batched into digests, while high-risk or high-value learning can notify immediately.
- Telegram is the initial review notification surface, while fumemory remains the canonical review queue and source of state transitions.
- Telegram reflection notices are compact by default and provide an inspect action for full forensic evidence rather than dumping raw proof into chat.
- Timeout-integrated Learning Memory is marked `accepted_by_timeout` and can be superseded by later feedback without mutating original records.
- Learning Memory carries source evidence links and promotion audit history.
- Core Railway readiness can pass without NATS or Temporal; federation readiness cannot pass without Railway NATS proof and searchable memory proof.
- Railway topology is represented as five explicit service contracts: required `api`, required `postgres-pgvector`, federation-only `nats-jetstream`, optional `temporal-worker`, and optional `embedding-service`.
- Verification emits machine-readable proof artifacts without secrets and includes evidence memory IDs for audit.
- fumemory never claims task completion or gateway availability authority; it emits proof consumed by OpenClaw or operators.
- The first Memory Action Eval passes only when session two behavior changes because default recall surfaced accepted Learning Memory derived from session one evidence.

## Further Notes

- `CONTEXT.md` is the source of truth for domain language: OpenClaw Coordinator, Memory Evidence Plane, Canonical Memory Write, Async Memory Workflow, Evidence Memory, Learning Memory, and Forensic Recall.
- Existing repo docs that should guide implementation include `docs/CROSS_GATEWAY_NATS_FEDERATION.md`, `docs/GATEWAY_FEDERATION_ROLLOUT_CHECKLIST.md`, `docs/railway-readiness.md`, `docs/railway-deploy.md`, and `docs/GRAPH_LITE_RELATIONSHIPS.md`.
- Current code already has useful foundations: the compatibility add endpoint, typed memory models, bi-temporal columns, graph-lite links, search fallback, salience, idempotency metadata, memory agent compaction, OpenClaw hooks, Railway verification, and NATS federation smoke.
- The highest-risk current mismatch is that OpenClaw hook logging uses async memory writes while the resolved domain contract requires synchronous canonical evidence writes.
- External prior art supports the direction but should be treated as evidence, not as a mandate:
  - Graphiti/Zep emphasizes temporal knowledge graphs, evolving facts, provenance, and hybrid semantic, keyword, and graph retrieval.
  - Hindsight emphasizes structured memory networks that distinguish raw experience from synthesized facts and beliefs.
  - LangMem/LangGraph emphasizes explicit long-term memory operations across sessions and agent workflows.
  - Cognee OpenClaw shows an OpenClaw plugin lifecycle with auto-index, auto-recall, graph search, and hash-based change tracking.
  - Mem0 OpenClaw shows turn-level auto-capture and auto-recall outside the context window.
  - MemoryArena emphasizes evaluation through interdependent multi-session tasks where agents must learn from earlier actions and reuse that memory later.
- A practical rollout should start with schema and recall mode changes, then hook contract migration, then reflection worker promotion rules, then Railway and federation verification gates, then multi-session evaluation.
