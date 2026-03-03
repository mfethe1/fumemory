"""memU — Free, open-source shared memory for AI agents."""

__version__ = "0.1.0"

__all__ = ["MemUClient", "Memory", "MemoryType", "SearchResult", "adapters"]


def __getattr__(name):
    if name == "MemUClient":
        from memu.client import MemUClient
        return MemUClient
    if name == "Memory":
        from memu.models import Memory
        return Memory
    if name == "MemoryType":
        from memu.models import MemoryType
        return MemoryType
    if name == "SearchResult":
        from memu.models import SearchResult
        return SearchResult
    if name == "adapters":
        import memu.adapters
        return memu.adapters
    raise AttributeError(name)
