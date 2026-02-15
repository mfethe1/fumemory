"""memU — Free, open-source shared memory for AI agents."""

__version__ = "0.1.0"

from memu.client import MemUClient
from memu.models import Memory, MemoryType, SearchResult

__all__ = ["MemUClient", "Memory", "MemoryType", "SearchResult"]
