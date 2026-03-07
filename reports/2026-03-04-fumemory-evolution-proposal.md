# FuMemory Evolution Proposal — 2026-03-04

## Run Evidence
- `routine_memu_evolution.sh`: ✅ MD sync ran, health check passed (`ok=true`, `db=schema=vector=ok`, `total_memories=2700`)
- Deep research: ✅ Perplexity `sonar-deep-research` completed (`29 in / 9450 out tokens`)
- `/tasks` API write: ❌ unavailable in prod for this route (`POST /api/v1/memu/tasks` -> 405, `POST /tasks` -> 404)
- Fallback applied: ✅ backlog updated in `memu-oss/TASK_BACKLOG.md`

## Most Promising Architecture Moves

### 1) Hybrid Neuro-Symbolic Memory Tiers (highest impact)
**Concept:** split memory into three tiers:
- **L0 (Working):** short-window, high-churn context in RAM
- **L1 (Episodic):** quantized vector store for semantic retrieval
- **L2 (Semantic/Rules):** graph facts + symbolic constraints + policy checks

**Why this wins:**
- Keeps latency low for active reasoning
- Preserves long-horizon recall
- Adds explainability and contradiction resistance through symbolic checks

### 2) Reinforcement Temporal Decay (not just time decay)
**Concept:** memory strength decays with inactivity but gets reinforced on successful retrieval and task outcomes.

**Why this wins:**
- Prevents useful memories from being pruned
- Naturally suppresses stale/unhelpful memory
- Creates quality drift control without manual curation

### 3) Local-First CRDT Sync
**Concept:** agent writes first to local SQLite WAL + op-log, then merges with peers/backend using CRDT semantics when connectivity returns.

**Why this wins:**
- Offline resilience
- Lower dependency on centralized uptime
- Deterministic convergence in distributed multi-agent setups

### 4) Vector DB Efficiency Stack
**Concept:** int8 quantization + HNSW tuning + hybrid BM25 fusion + optional GPU path.

**Why this wins:**
- Major cost reduction (RAM + compute)
- Better p95 latency at scale
- Better exact-term recall with hybrid retrieval

## 30-Day Implementation Plan

### Phase 1 (Week 1): Baseline + Safety Rails
1. Add hybrid retrieval endpoint (ANN + BM25 + RRF).
2. Instrument precision@5, contradiction-rate, stale-hit-rate, p95 latency.
3. Ship benchmark harness with fixed query corpus.

### Phase 2 (Week 2): Tiered Routing
1. Introduce L0/L1/L2 memory router.
2. Route writes by memory type (observation/fact/decision/policy).
3. Add trace output: `why_this_memory` for every retrieval response.

### Phase 3 (Week 3): Reinforcement Decay
1. Add decay + reinforcement scoring formula.
2. Record score deltas in audit table.
3. Run 30-day replay simulation and tune half-life profiles by lane.

### Phase 4 (Week 4): Local-First Sync MVP
1. Implement local op-log + WAL.
2. Build CRDT merge/replay endpoint and conflict metrics.
3. Run 3-peer deterministic convergence tests under partition/reconnect scenarios.

## Concrete Success Targets
- Precision@5: **+12%** on hard retrieval set
- Contradiction rate: **-40%**
- p95 retrieval latency: **<120ms** at 10k+ memory scale
- Memory infra cost/query: **-35%**
- Offline task continuity: **>=95%** for local-first agents during simulated API outages

## Notes
- This proposal was grounded in the deep-research run focused on neuro-symbolic storage, temporal graph decay, local-first P2P sync, and vector optimization.
- Backlog items reflecting this proposal were added to local fallback backlog because production task API is still inconsistent.
