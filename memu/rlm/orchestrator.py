"""Karpathy-style Recursive Language Model orchestrator.

Flow::

    solve(task, ctx, depth=0, budget=8000):
        if depth >= max_depth: distill
        sub_queries = decompose(task)
        for q in sub_queries:
            nodes = retrieve(q)              # reuses semantic_router when avail
            if tokens(nodes) > budget:
                solve(q, ctx.scope(nodes), depth+1, budget // 2)
            else:
                collect(nodes)
        return aggregate(task, collected)    # reuses memory_agent compaction

The orchestrator is deliberately small: decomposition and aggregation are
pluggable callables so tests can pass deterministic stubs while production
wires in an LLM.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Protocol

from ..storage.base import SearchHit, StorageBackend, WikiNode
from .retriever import Embedder, HybridRetriever


DecomposerFn = Callable[[str, int], Awaitable[list[str]]]
AggregatorFn = Callable[[str, list["RLMResult"]], Awaitable["RLMResult"]]


class Retriever(Protocol):
    async def retrieve(self, query: str, k: int) -> list[SearchHit]: ...


@dataclass
class RLMContext:
    max_depth: int = 3
    k: int = 8
    token_budget: int = 8000
    persona: str = "default"
    # Token counter: replace with tiktoken in production; default is a rough
    # whitespace estimate so the orchestrator works without extra deps.
    token_counter: Callable[[str], int] = field(default=lambda s: max(1, len(s) // 4))


@dataclass
class RLMResult:
    task: str
    answer: str
    slugs: list[str] = field(default_factory=list)
    depth: int = 0
    children: list["RLMResult"] = field(default_factory=list)


class DefaultRetriever:
    """Backward-compatible wrapper: delegates to :class:`HybridRetriever`.

    Older code constructed :class:`Orchestrator` without an explicit
    retriever and got FTS-only behavior. The orchestrator now wraps
    :class:`HybridRetriever` instead, which stays FTS-only when no
    embedder is wired but transparently adds vector + graph fusion once
    one is provided.
    """

    def __init__(self, backend: StorageBackend, *, embedder: Optional[Embedder] = None):
        self._hybrid = HybridRetriever(backend, embedder=embedder)

    async def retrieve(self, query: str, k: int) -> list[SearchHit]:
        return await self._hybrid.retrieve(query, k=k)


async def _noop_decompose(task: str, fanout: int) -> list[str]:
    """Deterministic fallback: no decomposition, one query == the task."""
    return [task]


async def _concat_aggregate(task: str, parts: list[RLMResult]) -> RLMResult:
    slugs: list[str] = []
    pieces: list[str] = []
    for p in parts:
        slugs.extend(p.slugs)
        if p.answer:
            pieces.append(p.answer)
    return RLMResult(
        task=task,
        answer="\n\n".join(pieces),
        slugs=list(dict.fromkeys(slugs)),
        children=parts,
    )


class Orchestrator:
    def __init__(
        self,
        backend: StorageBackend,
        *,
        retriever: Optional[Retriever] = None,
        embedder: Optional[Embedder] = None,
        decomposer: DecomposerFn = _noop_decompose,
        aggregator: AggregatorFn = _concat_aggregate,
    ) -> None:
        self.backend = backend
        if retriever is not None:
            self.retriever = retriever
        else:
            self.retriever = HybridRetriever(backend, embedder=embedder)
        self.decomposer = decomposer
        self.aggregator = aggregator

    async def solve(
        self,
        task: str,
        ctx: Optional[RLMContext] = None,
        *,
        depth: int = 0,
        budget: Optional[int] = None,
    ) -> RLMResult:
        ctx = ctx or RLMContext()
        budget = budget if budget is not None else ctx.token_budget

        if depth >= ctx.max_depth:
            return await self._leaf(task, ctx, budget)

        sub_queries = await self.decomposer(task, ctx.k)
        if not sub_queries:
            sub_queries = [task]

        parts: list[RLMResult] = []
        for q in sub_queries:
            hits = await self.retriever.retrieve(q, k=ctx.k)
            nodes = [h.node for h in hits]
            if not nodes:
                parts.append(RLMResult(task=q, answer="", slugs=[], depth=depth + 1))
                continue
            cost = sum(ctx.token_counter(n.body) for n in nodes)
            if cost > budget and depth + 1 < ctx.max_depth:
                # Recurse on a scoped sub-problem; child budget halves.
                child = await self.solve(q, ctx, depth=depth + 1, budget=budget // 2)
                parts.append(child)
            else:
                parts.append(self._as_result(q, nodes, depth=depth + 1))
        return await self.aggregator(task, parts)

    async def _leaf(self, task: str, ctx: RLMContext, budget: int) -> RLMResult:
        hits = await self.retriever.retrieve(task, k=ctx.k)
        nodes = [h.node for h in hits]
        return self._as_result(task, nodes, depth=ctx.max_depth)

    @staticmethod
    def _as_result(task: str, nodes: list[WikiNode], *, depth: int) -> RLMResult:
        slugs = [n.slug for n in nodes]
        # Leaf answer: a citation list. The caller is responsible for
        # substituting a real LLM synthesis step if needed.
        lines = [f"- [[{n.slug}]] — {n.title}" for n in nodes]
        return RLMResult(
            task=task,
            answer="\n".join(lines),
            slugs=slugs,
            depth=depth,
        )
