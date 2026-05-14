# Upstream memU Port Backlog

Upstream reference: **[NevaMind-AI/memU](https://github.com/NevaMind-AI/memU)**
License: Apache-2.0 (compatible — attribution in `NOTICE` is the only obligation).

_Note: `protelynx/memu` resolves 404; it either moved or never existed. Only
NevaMind-AI/memU is canonical as of 2026-04._

Ranked by fumemory value. "Effort" is a 1-dev-afternoon (small) / 1–2 dev-days
(medium) / multi-PR (large) sizing against our current architecture.

## A. Inline `[ref:ITEM_ID]` citations in summaries  — small

Upstream PR: [#205](https://github.com/NevaMind-AI/memU/pull/205)

Category summaries store `(item_id, summary)` tuples; the LLM emits
`[ref:abc]` inline, enabling drill-down retrieval and audit trails.

**Our port shape:** treat `[ref:ID]` as a machine-resolved cousin to our
`[[wikilinks]]` — summaries stay human-readable but carry provenance
pointers. Extend `memu/wiki/wikilink.py` with a second regex for `[ref:…]`
and teach the slug registry to resolve refs to node ids.

## B. Tool Memory type  — medium

Upstream PR: [#247](https://github.com/NevaMind-AI/memU/pull/247)

Adds `memory_type="tool"` with `ToolCallResult` entries, a `when_to_use`
hint, `tool_calls` history, and `get_tool_statistics()` (success rate,
avg latency, avg score).

**Our port shape:** every MCP tool invocation gets logged as a `tool`
kind wiki node under `tool/<server>/<tool_name>`. The Claude Code plugin
already sees every tool call — we can wire a `PostToolUse` hook that
writes a tool memory, and `wiki_search kind=tool` surfaces a
self-maintaining tool playbook.

## C. Workflow step + LLM-call hooks  — medium

Upstream PR: [#240](https://github.com/NevaMind-AI/memU/pull/240)

Pipeline engine and LLM client expose pre/post hooks around
`chat/summarize/vision/embed/transcribe`.

**Our port shape:** the RLM orchestrator and embedder need the same
hook surface for tracing, cost accounting, and policy enforcement
(Warden/OPA). Small interface in `memu/rlm/hooks.py`; wire into
`Orchestrator.solve` and the `Embedder` Protocol.

## D. `happened_at` + `extra` JSON field  — small

Upstream PR: [#262](https://github.com/NevaMind-AI/memU/pull/262)

Decouples *event time* from *ingest time* — critical for importing
backlogs (git history, Slack exports) without corrupting decay curves.

**Our port shape:** add `happened_at: datetime | None` and `extra: dict`
to `WikiNode` + frontmatter; teach `memu.decay` (when we wire it back
in) to prefer `happened_at` over `created_at`.

## E. Reinforcement-on-duplicate via content hash  — small

Upstream PR: [#206](https://github.com/NevaMind-AI/memU/pull/206)

On duplicate-write of equivalent content, bump
`reinforcement_count` + `last_reinforced_at` instead of inserting.
Combined ranking `similarity × log(reinforcement+1) × recency_decay`.

**Our port shape:** we already compute a SHA-256 content hash in the
codebase ingester. Extend the backend's `put_node` to detect exact
content hits and bump a reinforcement counter instead of creating a new
node; mirror upstream's combined ranking in `memu/spreading_activation.py`.

## F. Sufficiency-gated retrieval  — medium

Upstream: `/docs/architecture.md`

Early-terminate the pipeline when category-level recall already answers
the query; only descend to items/resources if not.

**Our port shape:** drops straight into `memu/rlm/orchestrator.py`'s
recursion loop. After each sub-query, check a sufficiency heuristic
(e.g., score-cluster entropy, citation coverage); if sufficient, skip
further descent. Saves tokens and latency on queries that are already
answered by the first retrieval.

## G. LangGraph two-tool adapter surface  — small

Upstream PR: [#258](https://github.com/NevaMind-AI/memU/pull/258)

Exposes `save_memory`, `search_memory` as a minimal LangChain/LangGraph
surface.

**Our port shape:** we already have `memu/adapters/langgraph.py`; mirror
upstream's two-tool contract so existing LangGraph users get parity
without reading docs.

---

## Do NOT port

* **Proactive 24/7 cloud agent (`api.memu.so`)** — server-centric,
  contradicts solo/local ethos.
* **memUbot / Clawdchat / Sealos integrations** — vendor-specific
  growth hacks.
* **LazyLLM backend** — extra dep weight; we already have multi-provider
  support.
* **Cargo/Rust components** — upstream is drifting toward hybrid build;
  we stay pure-Python and MCP-first.
* **Rigid `Resource → Item → Category` hierarchy as source of truth** —
  would undermine our markdown-first Obsidian layer. Borrow the
  *retrieval* shape, keep markdown+frontmatter as ground truth.

## Architectural divergences (intentional)

| Axis | Upstream | fumemory |
|---|---|---|
| Source of truth | DB rows (SQLModel) | Markdown + frontmatter |
| Link types | `CategoryItem` + `[ref:ID]` | `[[wikilinks]]` + slug registry |
| Salience | `reinforcement_count` × recency | `decay.py` + `spreading_activation.py` |
| Retrieval | Dual RAG/LLM w/ sufficiency gate | Karpathy-style RLM + hybrid RRF |
| Agent coord | Workflow pipeline + LangGraph | MCP server + Claude Code plugin |
| Multi-tenant | `user_scope` on every row | Single-user default; RLS tier exists |

Relevant local files for the port work:
`memu/decay.py`, `memu/spreading_activation.py`,
`memu/migrations/016_salience_score.sql`, `memu/memory_agent.py`,
`memu/mcp/tools.py`, `memu/wiki/slug_registry.py`,
`memu/rlm/orchestrator.py`, `memu/rlm/retriever.py`.
