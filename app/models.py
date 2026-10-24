from typing import Literal

from pydantic import BaseModel, Field, ConfigDict


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(min_length=1, max_length=32000)
    kind: Literal["code", "desktop"] = "code"  # which E2B sandbox backend to run in: "code" or "desktop"


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=32000)


class SessionOut(BaseModel):
    id: str
    kind: str
    status: str
    schema_version: int = 2
    revision: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    active_run_id: str | None = None
    cancel_requested: bool = False
    recovery_required: bool = False
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
    model_config = ConfigDict(extra="forbid")
    approval_id: str = Field(min_length=1, max_length=128)
    decisions: list[ApprovalDecision] = Field(min_length=1, max_length=100)


class TraceStepOut(BaseModel):
    step: int
    run_id: str | None = None
    tool: list[str]
    duration_ms: float
    input_tokens: int
    output_tokens: int
    provider: str
    model: str
    timestamp: str
