# FuMemory Evolution Proposal — 2026-03-05

## Run Evidence
- `routine_memu_evolution.sh`: ✅ MD sync + health check passed (`ok=true`, `db/scheme/vector=ok`, `total_memories=2733`).
- Perplexity deep research: ✅ completed via `sonar-deep-research` (`29 in / 9399 out tokens`).
- memU task API writes: ❌ unavailable in production routing (`POST /api/v1/memu/tasks` -> 405, `POST /tasks` -> 404).
- Fallback applied: ✅ backlog updated in `memu-oss/TASK_BACKLOG.md` with new P2 items (#9-#12).

## Creative Architecture Proposal: “Living Memory Mesh”

### Core Idea
Evolve memU from a single vector recall layer into a **living memory mesh**:
1. **Neuro layer** for fuzzy semantic recall (ANN vectors).
2. **Symbolic layer** for truth constraints, provenance, and contradiction checks.
3. **Temporal vitality layer** to promote memories that repeatedly prove useful.
4. **Local-first sync layer** to survive outages and converge deterministically.

This gives better recall quality, less stale noise, and stronger reliability under real multi-agent load.

## Most Promising Upgrades (ranked)

### 1) Provenance-weighted neuro-symbolic retrieval (highest leverage)
**What changes**
- Store confidence/provenance with each memory (source, timestamp, validation state).
- Keep ANN candidate generation, then run symbolic contradiction and policy filters.
- Final rank = semantic similarity × confidence × recency/vitality.

**Why now**
- Current failure mode is not “can’t find memory,” it’s “finds conflicting or weak memory.”
- This directly attacks contradiction-rate without killing semantic recall breadth.

### 2) Adaptive index maintenance (IVF/HNSW drift repair)
**What changes**
- Monitor shard-level latency + recall proxies.
- Re-cluster only degraded partitions instead of full rebuilds.
- Keep ingest online while tuning indexes.

**Why now**
- Corpus is growing; full rebuild strategy won’t scale operationally.
- Incremental repair improves stability and cost.

### 3) Two-stage context compression (masking → summary)
**What changes**
- Stage 1: mask low-value old context.
- Stage 2: summarize only when token pressure remains high.
- Regression tests for “lost-in-the-middle” behavior.

**Why now**
- Cheap compression-first strategy cuts token burn while preserving quality.
- Prevents summary-overcompression from degrading answer accuracy.

### 4) Event-driven multi-device coherence (delta push + merge ack)
**What changes**
- Publish memory deltas as events; peers apply CRDT merge.
- Track merge acknowledgements and retries.
- Add convergence tests under concurrent updates.

**Why now**
- Required for reliable local-first operation across agents/devices.
- Reduces stale windows after updates.

## Implementation Steps (specific)

### Phase A (Week 1): Retrieval integrity foundation
1. Add `confidence`, `provenance`, and `validation_state` fields to memory metadata.
2. Implement contradiction checker in post-ANN pipeline.
3. Add retrieval trace output (`candidate -> filtered -> final`) for QA.

### Phase B (Week 2): Adaptive vector ops
1. Add index health telemetry by partition (p95, miss proxy, queue depth).
2. Build targeted reindex worker for degraded partitions.
3. Add guardrails to prevent over-aggressive re-clustering.

### Phase C (Week 3): Context compression policy engine
1. Implement masking-first context reducer.
2. Implement structured summarizer fallback triggered by token pressure.
3. Add benchmark suite comparing quality/cost across policies.

### Phase D (Week 4): Coherence and sync hardening
1. Add delta event stream for memory mutations.
2. Add merge ack + retry queue + dead-letter handling.
3. Run 3-peer partition/reconnect convergence tests and publish report.

## Target Metrics
- Contradiction rate: **-40%**
- p95 retrieval latency at 10k+ memory scale: **<120ms**
- Token cost for long-context tasks: **-25%**
- Incremental index maintenance overhead: **<20%** of full rebuild equivalent
- Cross-peer convergence success under partition test: **>=99%**

## Backlog Sync Result
Attempted production task ingestion failed due routing mismatch. Added the most promising upgrades to local fallback backlog:
- #9 Adaptive vector index maintenance
- #10 Provenance-weighted contradiction guardrail
- #11 Two-stage context compression
- #12 Event-driven multi-device memory coherence

These are now ready for implementation sequencing once `/tasks` API route is fixed in prod.
