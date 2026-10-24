from pydantic import BaseModel


class SessionCreate(BaseModel):
    task: str
    kind: str = "code"  # which E2B sandbox backend to run in: "code" or "desktop"


class MessageIn(BaseModel):
    content: str


class SessionOut(BaseModel):
    id: str
    kind: str
    status: str
    error: str | None = None
    messages: list[dict]
    plan: list[dict] | None = None
    pending_approval: dict | None = None


class PreviewOut(BaseModel):
    url: str


class DesktopOut(BaseModel):
    stream_url: str


class CheckoutIn(BaseModel):
    tier_id: str


class ApprovalDecision(BaseModel):
    call_id: str
    approved: bool


class ApproveIn(BaseModel):
    decisions: list[ApprovalDecision]


class TraceStepOut(BaseModel):
    step: int
    tool: list[str]
    duration_ms: float
    input_tokens: int
    output_tokens: int
    provider: str
    model: str
    timestamp: str
