"""memU — Free, open-source shared memory for AI agents."""

__version__ = "0.1.0"

from memu.client import MemUClient
from memu.models import Memory, MemoryType, SearchResult
from memu.session_hook import SessionMemUHook, SessionHookConfig, SessionHookError
from memu.temporal_workflow import MemUHealthAndRepairWorkflow, WorkflowState

__all__ = [
    "MemUClient",
    "Memory",
    "MemoryType",
    "SearchResult",
    "SessionMemUHook",
    "SessionHookConfig",
    "SessionHookError",
    "MemUHealthAndRepairWorkflow",
    "WorkflowState",
]
