# Neighborhood Lock Design

Produced by the Plan subagent on 2026-04-19.
Coordination primitive preventing two agents from concurrently editing
nodes inside the same wiki neighborhood (a node plus its 1-hop link
graph). Scales across all three storage tiers and composes with the
existing lane-lock machinery in `memu/lane_lock.py`.

## 1. API shape

New module `memu/neighborhood_lock.py`. Mirrors the ergonomics of
`LaneLockedExecution` (`memu/lane_lock.py:93`) but is storage-backend
aware rather than NATS-only.

```python
class NeighborhoodLock:
    def __init__(
        self,
        backend: StorageBackend,           # from memu/storage/base.py:116
        slug: str,                          # the node being edited
        *,
        agent: str,                         # e.g. "wiki-worker-1"
        ttl: float = 30.0,                  # seconds
        renew_every: float = 10.0,
        scope: Literal["out", "in", "both"] = "both",
        block: bool = False,                # False = fail-fast, True = wait
        wait_timeout: float | None = None,
    ): ...

    async def __aenter__(self) -> "NeighborhoodLock": ...
    async def __aexit__(self, exc_type, exc, tb) -> bool: ...

    fencing_token: int                      # set on acquire
    held_slugs: frozenset[str]              # node + resolved neighbors
    async def assert_held(self) -> None     # raises FencingTokenError
    async def renew(self) -> None
```

**Composition with `LaneLockedExecution`.** The NATS tier **reuses** the
KV buckets from `lane_lock.py:62–63` (`RESOURCE_LANES`,
`FENCING_TOKENS`) under a namespaced prefix `nbhd:<slug>` so orphan
detection (`lane_lock.py:534`) and the heartbeat loop
(`lane_lock.py:291`) automatically cover neighborhoods. A thin
`_NatsNeighborhoodRegistry` delegates `_acquire_lane` /
`_renew_lane_lock` per slug; we do not fork the state machine. On tiers
0/1 we supply separate registry implementations behind the same
`LockRegistry` Protocol so the context-manager body is identical.

## 2. Per-tier backend

All tiers implement `LockRegistry` with `try_acquire(slugs, agent, ttl,
token) -> AcquireResult`, `renew(...)`, `release(...)`, `inspect(slug)`.

**Tier 0 — Markdown.** Registry is a sidecar directory `.memu/locks/`
inside the vault (`VaultLayout`, referenced from
`markdown_backend.py:41`). One JSON file per acquisition, plus per-slug
`*.lock` files created via `os.open(..., O_CREAT | O_EXCL)` for
atomicity; fencing tokens live in `.memu/locks/_fence/<slug>` and are
incremented under `fcntl.flock` on Unix / `msvcrt.locking` on Windows.
Multi-process on a single host: supported. Multi-machine over NFS /
Dropbox: **not supported** — document this and refuse to acquire if
`MEMU_MULTI_HOST=1` without Postgres / NATS. Crash semantics: recovery
reads JSON `expires_at` the same way `lane_lock.py:182–191` does.

**Tier 1 — SQLite.** Add two tables to the schema in
`sqlite_backend.py:26–67`:

```sql
CREATE TABLE IF NOT EXISTS neighborhood_locks (
    slug TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    root_slug TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fencing_tokens (
    slug TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
```

Acquisition is a single `BEGIN IMMEDIATE` transaction that deletes
expired rows and inserts the neighborhood set; conflicts surface as
`IntegrityError` on the PK. Default deployment is single-process; for
multi-process we rely on SQLite's writer lock under `PRAGMA
journal_mode = WAL` (already on at `sqlite_backend.py:99`). No multi-
machine.

**Tier 2 — Postgres + NATS.** Use JetStream KV exactly as lane-lock
does (`lane_lock.py:71–90`), one KV entry per slug in `RESOURCE_LANES`
keyed `nbhd:<slug>`, reusing `FENCING_TOKENS`. Acquisition is per-slug
CAS (`kv.create`) iterated over the neighborhood; on any contention we
roll back already-acquired slugs. The existing `orphan_detector_loop`
(`lane_lock.py:534`) reclaims crashed agents unchanged. Full multi-
machine support; clock skew is bounded by KV TTL rather than wall time.

## 3. Neighborhood definition

`scope="both"` (default) = `{root} ∪ outbound([[wikilinks]]) ∪ inbound
backlinks`, resolved via `backend.list_links(ref, direction="both")`
(`base.py:142`). **Not transitive.** Two-hop would serialize most edits
in a dense vault; one-hop is the smallest set that still prevents the
realistic races:

- Rename of A invalidating backlinks in B.
- Dual edits of A and its parent both rewriting the same link.

Agents that need two-hop safety explicitly pass `scope="both"` on each
root they touch.

Resolution happens once at acquire time. If a neighbor appears after
acquire (someone else added a backlink), the fencing-token check on
`put_node` still catches concurrent writes even without re-resolution.

## 4. Conflict resolution protocol

Default: **fail fast** with
`NeighborhoodConflict(slug, held_by, expires_at)` — a subclass of
`LaneContestedError` (`lane_lock.py:597`) so existing orchestrator
`except` branches still fire. The RLM orchestrator
(`memu/rlm/orchestrator.py:89`) catches it, re-queues the task onto a
different slug from its fanout, and only blocks if no alternative
exists.

Opt-in `block=True, wait_timeout=T` mode: poll-and-backoff every 500 ms
up to `T` seconds before raising. We deliberately do not implement a
server-side queue — it would require the backend to notify waiters,
which NATS supports but SQLite / Markdown do not, so parity wins.

## 5. Fencing tokens

Tokens are monotonic per-slug integers, initialized at 1, incremented
on every successful acquire (same algorithm as
`lane_lock.py:203–210`). The context manager exposes `fencing_token` as
a public attribute.

**Integration.** Add an optional `fencing_token: int | None` kwarg to
`StorageBackend.put_node` (widen the Protocol at `base.py:126`).
Backends check it inside the same transaction that mutates the node:

- SQLite: `put_node` (`sqlite_backend.py:116`) gains a guard
  `SELECT fencing_token FROM neighborhood_locks WHERE slug = ?`;
  mismatch raises `FencingTokenError` (reuse `lane_lock.py:607`).
- Postgres: `WHERE` clause compares against `neighborhood_locks.token`.
- Markdown: check `.memu/locks/_fence/<slug>` before
  `path.write_text` (`markdown_backend.py:63`).

The MCP tool `mcp__memu__wiki_write` (invoked by `wiki-worker.md:3`)
threads the token through from the currently active context manager.

## 6. Testing strategy

Tests live in `tests/test_neighborhood_lock.py` and run against all
three registries via parametrization.

1. `test_single_process_conflict_raises` — two coroutines acquire
   overlapping neighborhoods in the same process; the second raises
   `NeighborhoodConflict` with the correct `held_by`.
2. `test_neighborhood_includes_backlinks` — seed node B linking to A;
   lock A blocks lock on B. Proves scope resolution.
3. `test_ttl_expiry_reclaim` — agent 1 acquires with `ttl=1`, sleeps
   past TTL without renewing; agent 2 reclaims cleanly and gets a
   higher token.
4. `test_renewal_keeps_lock_alive` — agent 1 renews every 100 ms for
   2 s; agent 2 never acquires. Proves heartbeat path.
5. `test_fencing_rejects_stale_write` — force expiry on agent 1, have
   agent 2 acquire and bump token, then agent 1 attempts `put_node`
   with its old token and gets `FencingTokenError`. Proves split-brain
   safety.
6. `test_multiprocess_sqlite_file_lock` — spawn two processes via
   `multiprocessing`, both contend on a WAL SQLite DB; exactly one
   succeeds.
7. `test_nats_orphan_detector_reclaims` — kill holder coroutine without
   `__aexit__`; after TTL the `orphan_detector_loop` emits
   `TASK_ORPHANED` and the slug is free. Proves crash recovery on
   tier 2.

### Critical files for implementation

- `memu/neighborhood_lock.py` (new)
- `memu/lane_lock.py`
- `memu/storage/base.py`
- `memu/storage/sqlite_backend.py`
- `memu/storage/markdown_backend.py`
