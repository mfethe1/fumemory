"""Recursive Language Model orchestration layer.

The :class:`Orchestrator` decomposes a task into sub-queries, routes each
sub-query through the existing :mod:`memu.semantic_router`, and recursively
summarizes results via the existing compaction helpers in
:mod:`memu.memory_agent`. Sub-agents receive *slugs*, never raw bodies, so
context stays small.
"""
from .orchestrator import Orchestrator, RLMContext, RLMResult

__all__ = ["Orchestrator", "RLMContext", "RLMResult"]
