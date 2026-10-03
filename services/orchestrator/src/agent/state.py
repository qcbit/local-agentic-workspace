from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


@dataclass
class Message:
    role: Role
    content: str
    name: Optional[str] = None


@dataclass
class AgentState:
    user_goal: str
    history: List[Message] = field(default_factory=list)
    is_complete: bool = False
    is_canceled: bool = False
    iterations: int = 0
    max_iterations: int = 25
    summary: str = ""
    run_id: str = ""
    workflow_context: Optional[Dict[str, Any]] = None