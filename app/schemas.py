from typing import List, Optional, Any, Dict
from pydantic import BaseModel, Field, ConfigDict
from enum import Enum


class PageItem(BaseModel):
    index: int
    text: str
    href: Optional[str] = None
    tag: Optional[str] = None
    selector: Optional[str] = None
    score: Optional[float] = None
    llm_confidence: Optional[float] = None


class JobDetail(BaseModel):
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    salary: Optional[str] = None
    description: Optional[str] = None
    apply_link: Optional[str] = None
    requirements: Optional[List[str]] = Field(default_factory=list)
    benefits: Optional[List[str]] = Field(default_factory=list)


class PageSnapshot(BaseModel):
    url: str
    title: str
    text: str = Field(default="")
    links: List[PageItem] = Field(default_factory=list)
    buttons: List[PageItem] = Field(default_factory=list)
    inputs: List[PageItem] = Field(default_factory=list)


class RepeatedBlock(BaseModel):
    index: int
    html: str
    text: str
    children_count: int
    tag_path: str
    href: Optional[str] = None
    score: float = 0.0


class ExtractRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    url: str
    extraction_schema: Optional[Dict[str, Any]] = Field(default=None, alias="schema")
    max_items: int = 20
    use_llm: bool = True


class QueryRequest(BaseModel):
    url: str
    query: str
    use_llm: bool = True


class BrowserFetchRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    url: str
    params: Dict[str, Any] = Field(default_factory=dict)
    warm_up_url: Optional[str] = None
    headers: Dict[str, str] = Field(default_factory=dict)
    timeout_ms: int = 30000
    wait_until: str = "domcontentloaded"


class ActionRequest(BaseModel):
    url: str
    action: str
    target: Optional[str] = None
    value: Optional[str] = None
    use_llm: bool = True


class AQLQueryRequest(BaseModel):
    url: str
    aql: str
    use_llm: bool = True




class AgentRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    url: str
    task: str
    extraction_schema: Optional[Dict[str, Any]] = Field(default=None, alias="schema")
    max_steps: int = 8
    use_llm: bool = True
    screenshot: bool = False


class WorkflowRequest(BaseModel):
    url: str
    steps: List[Dict[str, Any]]
    max_retries: int = 3
    use_llm: bool = True


class APIResponse(BaseModel):
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    screenshot: Optional[str] = None
    llm_used: bool = False
    retries: int = 0
    healing_applied: bool = False


class LLMElementResult(BaseModel):
    element_type: str
    text: str
    confidence: float
    reason: str
    selector_hint: Optional[str] = None


class SessionStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    ERROR = "error"
    CLOSED = "closed"


class SessionInfo(BaseModel):
    session_id: str
    status: SessionStatus
    url: Optional[str] = None
    created_at: str
    last_used: str
    error_count: int = 0
