from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from enum import Enum

class NodeStatus(Enum):
    ACTIVE = "active"
    IDLE = "idle"
    FAILING = "failing"
    TERMINATED = "terminated"

class WardenCommand(BaseModel):
    action: str = Field(..., pattern="^(spawn|kill|respawn|sandbox)$")
    gateway_id: str
    role: Optional[str] = None
    task_id: Optional[str] = None
    fencing_token: Optional[int] = None
    resource_limits: Dict[str, str] = {"memory": "1g", "cpu": "0.5"}

class Heartbeat(BaseModel):
    gateway_id: str
    timestamp: float
    fencing_token: int
    active_task_id: Optional[str]
    load_average: float

class BlackboardEntry(BaseModel):
    key: str
    value: Any
    source_gateway: str
    confidence: float = 1.0
    is_validated: bool = False  # Lenny's QA flag
