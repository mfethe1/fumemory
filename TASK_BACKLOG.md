# memU Task Backlog (local fallback)

Updated: 2026-03-05
Reason: production `/tasks` endpoint returned 404; captured here for later API sync.

## P0

### 1) Temporal graph decay engine
- **Task:** Implement temporal graph decay for memU memories (event-time + access-time half-life scoring) with tunable lane profiles and audit trail.
- **Owner:** lenny
- **Lane:** memory-core
- **Why now:** stale memories are likely diluting retrieval quality as corpus grows.
- **Acceptance criteria:**
  - configurable half-life policy by memory lane
  - retrieval score includes temporal decay factor
  - stale-hit rate metric in dashboard

### 2) Neuro-symbolic memory router
- **Task:** Prototype neuro-symbolic memory router: vector ANN candidate retrieval + symbolic constraint/filter pass + explainability traces.
- **Owner:** lenny
- **Lane:** retrieval
- **Why now:** improves correctness and trust while preserving semantic recall.
- **Acceptance criteria:**
  - rule-filter stage after vector retrieval
  - explanation trace for final memories
  - A/B report with precision@5 and contradiction-rate deltas

## P1

### 3) Local-first CRDT sync layer
- **Task:** Add local-first CRDT sync layer for offline agents (append-only op log + merge/replay + conflict metrics dashboard).
- **Owner:** lenny
- **Lane:** sync
- **Why now:** enables resilient offline-first operation with deterministic convergence.
- **Acceptance criteria:**
  - peer merge semantics documented and tested
  - replay endpoint + conflict counter metrics
  - deterministic convergence test suite

### 4) Vector compression + reranking pipeline
- **Task:** Introduce vector compression pipeline (int8/binary quantization + rerank on full-float vectors) and benchmark recall/latency/cost tradeoffs.
- **Owner:** lenny
- **Lane:** vector-db
- **Why now:** reduce infra cost and improve query latency at rising memory scale.
- **Acceptance criteria:**
  - benchmark harness for recall@k / p95 latency / RAM usage
  - quantized index profile toggles
  - production recommendation doc with guardrails

### 5) Hybrid retrieval fusion baseline (BM25 + ANN + RRF)
- **Task:** Add a first-class hybrid retrieval path combining keyword BM25 and vector ANN, merged with Reciprocal Rank Fusion.
- **Owner:** lenny
- **Lane:** retrieval
- **Why now:** deep-research evidence shows better recall on exact entities/terms while preserving semantic retrieval.
- **Acceptance criteria:**
  - dual retrieval execution path in API
  - configurable fusion weights / RRF mode
  - benchmark showing precision@5 and miss-rate improvements on hard queries

### 6) Tiered memory architecture (L0/L1/L2)
- **Task:** Implement memory tiers: L0 working set (hot RAM), L1 episodic vectors, L2 symbolic graph facts with policy gating.
- **Owner:** lenny
- **Lane:** memory-core
- **Why now:** separates fast-context operations from durable long-term reasoning and improves debuggability.
- **Acceptance criteria:**
  - explicit routing policy for each tier
  - migration plan for existing memories
  - observability dashboard showing tier hit ratios and latency

### 7) Retrieval-triggered reinforcement decay
- **Task:** Upgrade temporal decay to a reinforcement model where successful retrieval/access boosts edge/node vitality while inactive memory decays.
- **Owner:** lenny
- **Lane:** memory-core
- **Why now:** prevents over-pruning high-value memories and suppresses stale noise automatically.
- **Acceptance criteria:**
  - decay + reinforcement formula documented and configurable
  - audit log for score changes
  - offline replay test proving stability across 30+ day simulations

### 8) Local-first CRDT sync MVP (SQLite WAL + op-log + peer merge)
- **Task:** Build a local-first sync MVP for offline-capable agents with CRDT merge semantics and opportunistic peer synchronization.
- **Owner:** lenny
- **Lane:** sync
- **Why now:** supports intermittent connectivity and reduces central API dependency for short outages.
- **Acceptance criteria:**
  - append-only operation log persisted locally
  - merge/replay API and conflict telemetry
  - deterministic convergence test across 3 simulated peers

## P2

### 9) Adaptive vector index maintenance (IVF/HNSW drift repair)
- **Task:** Implement adaptive index maintenance that monitors partition recall/latency drift and triggers targeted re-clustering only for degraded regions.
- **Owner:** lenny
- **Lane:** vector-db
- **Why now:** avoids full index rebuilds while preserving retrieval quality under continuous ingest.
- **Acceptance criteria:**
  - drift detector with configurable thresholds
  - targeted reindex jobs + audit logs
  - throughput/latency benchmark vs full rebuild baseline

### 10) Provenance-weighted contradiction guardrail
- **Task:** Add provenance/confidence metadata to memories and enforce a symbolic contradiction veto during final retrieval ranking.
- **Owner:** lenny
- **Lane:** retrieval
- **Why now:** improves trustworthiness by reducing high-similarity but low-trust conflicting recalls.
- **Acceptance criteria:**
  - confidence score stored per memory/edge
  - contradiction checker in retrieval pipeline
  - measurable contradiction-rate reduction on regression set

### 11) Two-stage context compression (masking → structured summary)
- **Task:** Introduce staged context compression where old context is first masked, then summarized only when token pressure persists.
- **Owner:** lenny
- **Lane:** memory-core
- **Why now:** minimizes lost-in-the-middle failures while controlling token spend.
- **Acceptance criteria:**
  - policy engine for compression stage transitions
  - regression suite for context retention quality
  - token-cost and completion-quality dashboard deltas

### 12) Event-driven multi-device memory coherence
- **Task:** Implement delta-push sync notifications with CRDT merge acknowledgements to tighten cross-device memory convergence.
- **Owner:** lenny
- **Lane:** sync
- **Why now:** reduces stale state windows when multiple devices/agents update shared memory concurrently.
- **Acceptance criteria:**
  - changefeed/delta event channel
  - merge ack + retry tracking
  - deterministic convergence tests under concurrent write storms
