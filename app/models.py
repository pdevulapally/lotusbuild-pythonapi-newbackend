from typing import Literal

from pydantic import BaseModel, Field, ConfigDict, field_validator


class SelectionInput(BaseModel):
    @field_validator("model", "effort", "mode", mode="before", check_fields=False)
    @classmethod
    def explicit_selection_is_text(cls, value):
        if type(value) is not str:
            raise ValueError("selection must be a string")
        return value


class SessionCreate(SelectionInput):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(min_length=1, max_length=32000)
    project_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")
    kind: Literal["code", "desktop"] = "code"
    model: str | None = Field(default=None, min_length=1, max_length=128)
    effort: Literal["low", "standard", "high", "xhigh"] | None = None
    mode: Literal["build", "plan"] = "build"


class MessageIn(SelectionInput):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=32000)
    mode: Literal["build", "plan"] | None = None


class SessionOut(BaseModel):
    id: str
    kind: str
    model: str | None = None
    effort: str | None = None
    mode: str = "build"
    status: str
    project_id: str | None = None
    message_count: int | None = None
    schema_version: int = 2
    revision: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    active_run_id: str | None = None
    cancel_requested: bool = False
    recovery_required: bool = False
    recovery: dict | None = None
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
