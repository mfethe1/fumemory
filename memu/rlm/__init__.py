"""Recursive Language Model orchestration layer.

The :class:`Orchestrator` decomposes a task into sub-queries, routes
each sub-query through a :class:`HybridRetriever` that fuses whichever
of FTS / vector / graph search the active backend supports, and
recursively summarizes results. Sub-agents receive *slugs*, never raw
bodies, so context stays small.
"""
from .orchestrator import Orchestrator, RLMContext, RLMResult
from .retriever import Embedder, HybridRetriever, NullEmbedder, RetrievalPlan

__all__ = [
    "Orchestrator",
    "RLMContext",
    "RLMResult",
    "HybridRetriever",
    "RetrievalPlan",
    "Embedder",
    "NullEmbedder",
]
