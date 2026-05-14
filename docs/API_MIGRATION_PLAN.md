# API Migration Plan: FastAPI Routes → StorageBackend

Produced by the `claude-code-guide` / `Explore` audit agent on 2026-04-19
against commit [42be7eb](https://github.com/mfethe1/fumemory/commit/42be7eb).
Goal: make every FastAPI endpoint in `memu/api.py` a thin handler over
`StorageBackend`, so solo users (SQLite) and enterprise users (Postgres)
hit the same code path and existing Postgres tests continue to pass
throughout the transition.

## 1. Route Inventory

| Method | Path | Storage Operation(s) | Current Helper | Target Backend Method | Capability Needed |
|--------|------|---------------------|-----------------|----------------------|-------------------|
| POST | /memories | `put_node` + dedup | asyncpg + embedding | `put_node(WikiNode)` | vector dedup, FTS dedup, content hash |
| GET | /memories/{id} | `get_node` + access tracking | asyncpg | `get_node(ref)` | increment access_count (new) |
| DELETE | /memories/{id} | delete | asyncpg | `delete_node(ref)` | — |
| GET | /memories/search | search with decay | asyncpg + semantic_router | `search_hybrid(query, ...)` | vector + FTS + temporal decay |
| GET | /memories/recent | list by type | asyncpg | `list_nodes(kind, limit)` | temporal ordering (new) |
| GET | /api/v1/memu/stats | aggregate counts | asyncpg | `iter_changed()` + aggregation | — |
| POST | /search | hybrid search | asyncpg + embedding | `search_hybrid(query, k, agent_id, ...)` | vector + FTS + decay + ABAC filtering (new) |
| POST | /search-text | FTS only | asyncpg | `search_fts(query, k)` | agent_id/type filter (new) |
| POST | /memories/bulk | bulk insert | asyncpg loop + embedding | `bulk_put_nodes([WikiNode])` | **NEW METHOD** — batch with dedup |
| POST | /tasks | insert task | asyncpg (backlog table) | **`put_task(Task)` NEW** | — |
| GET | /tasks | list tasks with filters | asyncpg | **`list_tasks(status, owner, ...)` NEW** | — |
| PATCH | /tasks/{id} | update task | asyncpg | **`update_task(id, fields)` NEW** | — |
| POST | /tasks/{id}/review | review workflow | asyncpg | **`review_task(id, decision)` NEW** | — |
| POST | /api/v1/memu/links | insert link | asyncpg | `put_link(LinkRecord)` | — |
| GET | /api/v1/memu/links/{id} | list bidirectional links | asyncpg JOIN | `list_links(ref, "both")` | — |
| POST | /api/v1/memu/cypher | raw graph query | asyncpg + AGE | **`execute_cypher(query)` NEW** | graph-specific (Postgres-only) |
| GET | /api/v1/memu/graph/neighbors/{id} | 1st-degree neighbors | asyncpg + AGE | **`list_graph_neighbors(id)` NEW** | graph traversal (Postgres-only) |
| GET | /api/v1/memu/graph/stats | vertex/edge counts | asyncpg + AGE | **`get_graph_stats()` NEW** | graph stats (Postgres-only) |
| GET | /api/v1/memu/graph/path/{from}/{to} | shortest path | asyncpg + AGE | **`find_graph_path(from_id, to_id, max_hops)` NEW** | graph traversal (Postgres-only) |
| POST | /api/v1/memu/supersede | mark old, create link | asyncpg transaction | **`supersede_node(old_id, new_id)` NEW** | temporal invalidation |
| GET | /api/v1/memu/superseded/{id} | list superseding chain | asyncpg | **`get_supersession_chain(id)` NEW** | — |
| GET | /api/v1/memu/entities/current | temporal entity query | asyncpg | **`list_current_entities(agent_id)` NEW** | temporal filter (valid_to IS NULL) |
| GET | /api/v1/memu/temporal | temporal search | asyncpg | **`search_temporal(query, before_date, after_date)` NEW** | time-windowed FTS/vector |
| GET | /api/v1/memu/at | point-in-time snapshot | asyncpg | **`snapshot_at(timestamp)` NEW** | temporal snapshot (complex) |
| POST | /chat | RAG pipeline | asyncpg (search) + LLM | `search_hybrid(...)` | — |
| POST | /gateway-leases/* | lease lifecycle | asyncpg (leases table) | **`put_lease()`, `get_lease()`, etc. NEW** | — |
| POST/GET | /notion/* | 3rd-party bridge | asyncpg + HTTP bridge | **`get_notion_queue()`, `claim/complete` NEW** | integration (Postgres-only semantics) |
| GET | /api/forensics/* | task audit log | asyncpg (forensics table) | **`get_task_forensics()` NEW** | audit trail (Postgres-only) |
| POST | /api/v1/lanes/publish | message queue | asyncpg (lanes table) | **`publish_lane_message()` NEW** | pub/sub (Postgres-only) |
| POST | /api/v1/tenants | multi-tenant ops | asyncpg (tenants table) | **`put_tenant()`, `list_tenants()` NEW** | RLS/multi-tenancy (schema) |

**Summary:** 67 routes. ~30 dispatch cleanly through StorageBackend
(core CRUD + search), ~20 are Postgres-specific (graph, audit, tenants,
pub/sub), ~17 are compatibility shims or business-logic wrappers.

## 2. Capability Gaps

### 2.1 Extend `StorageBackend` Protocol

1. **`search_hybrid(query, *, k=10, kind=None, agent_id=None, temporal_weight=0.0, min_confidence=0.0, time_window_start=None, time_window_end=None) -> list[SearchHit]`**
   - Routes: `/search`, `/memories/search`
   - Current logic at `api.py:953–1100` — blends vector similarity, FTS fallback, and the `compute_final_score()` decay.

2. **`search_fts(query, *, k=10, kind=None, agent_id=None)`**
   - Exists; add `agent_id` and `kind` filters (currently implicit in Postgres ILIKE).

3. **`put_node_with_dedup(node, *, dedup_threshold=0.95) -> tuple[WikiNode, bool]`**
   - Routes: `/memories` (line 679), `/memories/bulk` (line 1324)
   - Moves content-hash + vector-similarity dedup out of route handlers.

4. **`bulk_put_nodes(nodes, *, dedup_threshold=0.95) -> BulkResult`**
   - Route: `/memories/bulk` (line 1324). Batches per-item dedup efficiently.

5. **`increment_node_access(ref) -> Optional[WikiNode]`**
   - Route: `/memories/{id}` (line 927). Reinforcement signal for decay.

6. **`put_link(link, *, upsert_strength_increment=None)`**
   - Route: `/api/v1/memu/links` (line 1936). Upstream's `strength = LEAST(1.0, strength + 0.1)`.

7. **`search_temporal(query, *, k=10, before=None, after=None, kind=None)`**
   - Routes: `/api/v1/memu/temporal` (line 2154), `/api/v1/memu/at` (line 2218).

8. **`supersede_node(old_id, new_id, *, metadata=None)` + `get_supersession_chain(node_id)`**
   - Routes: `/api/v1/memu/supersede` (1991), `/api/v1/memu/superseded/{id}` (2065).

### 2.2 Postgres-Specific Extensions

Live only on `postgres_backend`; gated behind a capability check so other tiers return 501:

- `execute_cypher(query, graph_name)` — `/api/v1/memu/cypher` (1781)
- `list_graph_neighbors(node_id, graph_name)` — `/api/v1/memu/graph/neighbors/{id}` (1795)
- `get_graph_stats(graph_name)` — `/api/v1/memu/graph/stats` (1824)
- `find_graph_path(from_id, to_id, max_hops)` — `/api/v1/memu/graph/path/{from}/{to}` (1877)
- Task domain: `put_task`, `list_tasks`, `update_task`, `review_task` — the task schema is separate from the wiki model, so consider a sibling `TaskBackend` Protocol.

## 3. Phased Migration Plan

### Phase 1 — Feature flag + adapter (2–3 PRs, behind `MEMU_USE_STORAGE_BACKEND`)

- Add `storage_backend: Optional[StorageBackend]` to FastAPI lifespan (`api.py:99–150`).
- Complete the SQLite and Markdown backends for the methods listed in §2.1.
- Add an adapter layer: `StorageBackendAdapter` wraps the backend and falls back to the existing asyncpg path if a method raises `NotImplementedError`.
- Guard `/memories` POST/GET/DELETE, `/search`, `/search-text` with the feature flag; call `adapter.put_node()` when enabled.
- Tests: existing Postgres tests pass via the fallback path; new tests exercise SQLite + Markdown backends against the same suite.

### Phase 2 — Dedup + bulk import (1 PR)

- Implement `put_node_with_dedup()` + `bulk_put_nodes()` in SQLite / Postgres.
- `/memories` POST calls `backend.put_node_with_dedup(...)` (commit to the backend path; no fallback).
- `/memories/bulk` calls `backend.bulk_put_nodes(...)` replacing the current loop.
- Risk: SQLite dedup with large datasets is O(N²); mitigate with chunking and a `MAX_BULK_SIZE`.

### Phase 3 — Hybrid search + temporal (2 PRs)

- Implement `search_hybrid`, `search_temporal`, `increment_node_access` across tiers.
- `/search` → `backend.search_hybrid(...)`; `/search-text` → `backend.search_fts(...)`.
- `/memories/{id}` GET calls `backend.increment_node_access(ref)` (replacing the inline UPDATE).
- Remove `semantic_router` from the hot path; let the backend choose its routing.

### Phase 4 — Entity supersession + links (1 PR)

- `supersede_node()` and `get_supersession_chain()` on both backends.
- `put_link()` grows an `upsert_strength_increment` kwarg.
- `/api/v1/memu/supersede`, `/superseded/{id}`, `/links`, `/links/{id}` all flip to the backend.

### Phase 5 — Postgres-only segregation + Task domain (2–3 PRs)

- Split out `TaskBackend` Protocol; implement on both tiers (no graph methods on SQLite).
- Introduce an optional `GraphBackend` mixin for `execute_cypher`, `list_graph_neighbors`, `get_graph_stats`, `find_graph_path`. Non-Postgres tiers return 501 Not Implemented.
- Move audit/pub-sub/forensics routes to Postgres-only gated handlers.
- Remove `_row_to_memory` / `_row_to_task` from `api.py` (now in backends). Keep `asyncpg` for the Notion bridge (external system).

## 4. Risk Flags

1. **Postgres-specific semantics** — RLS policies (`api.py:70`), `async with conn.transaction()` in `supersede_entity()` (1991), raw Cypher dependencies on Apache AGE. Mitigate by passing `tenant_id` explicitly, requiring backends to expose transactional semantics, and gating graph routes behind capability checks with clear 501 responses.
2. **Embedding-service fallback** (`/search`, line 957) — two codepaths in one handler. Mitigate by pushing fallback into `backend.search_hybrid()`; test both paths.
3. **Bulk dedup at scale** — `/memories/bulk` with a large `k` can be O(N²) in SQLite. Cap with `MAX_BULK_SIZE` (e.g. 10K) and benchmark latency before rollout.
4. **Temporal query complexity** — `/api/v1/memu/at` (2218) builds a snapshot of the entire dataset at a point in time. SQLite will sequential-scan; document the perf ceiling before migration.
5. **Test-suite assumption** — existing tests require `DATABASE_URL`. Phase 1 adds a `conftest` fixture that switches backends via env var and marks Postgres-only cases with `pytest.mark.postgres`.
6. **Feature-flag bloat** — `MEMU_USE_STORAGE_BACKEND` must respect `verify_api_key()` and tenant context; never apply it to multi-tenant routes (`/api/v1/tenants`).
7. **NATS event publishing** — routes publish events after writes (`api.py:799–810`, `1418–1428`). Keep NATS publishing in the api layer (outside the backend transaction) so a backend write without a downstream publish never corrupts the DAG.
8. **Notion bridge** — `/notion/*` uses asyncpg directly and talks to an external system; defer to Phase 5 at earliest.

## Implementation Checklist

- [ ] Phase 1: all routes guarded by the flag; adapter falls back when backend unavailable.
- [ ] Phase 1: SQLite + Markdown backends pass the existing test suite.
- [ ] Phase 2: dedup logic in backend; no SQL in `api.py` for `create_memory`.
- [ ] Phase 3: semantic router off the hot path; search_hybrid handles routing.
- [ ] Phase 3: temporal routes return 501 on tiers without support, with a documented SQLite limitation.
- [ ] Phase 4: no raw SQL for links or supersession in `api.py`.
- [ ] Phase 5: task + graph routes dispatch to their respective Protocols.
- [ ] All phases: NATS publish stays in api.py (outside backend transactions); tenancy filtering is passed explicitly.
- [ ] All phases: asyncpg pool retained for compatibility through the rollout; removed in the cleanup PR after Phase 5.

Estimated effort: ~8–10 weeks (1–2 PRs/week, 3–5 day review cycle). Total
LoC: ~800 `api.py` + ~500 backend implementations.
