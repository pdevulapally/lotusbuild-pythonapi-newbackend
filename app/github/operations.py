"""One bounded proposal contract. Proposals confer no authorization."""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.execution.limits import MAX_TOOL_INPUT_BYTES, bounded_json

OPERATION = "create_pull_request"
PERMISSIONS = frozenset({"contents:read", "pull_requests:write"})


class PullRequestProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    # Approval is for a PR between mutable named branches, not an immutable diff.
    approval_scope: Literal["named_branches"]
    repo: str = Field(min_length=3, max_length=140)
    repository_id: int = Field(gt=0, le=2**63 - 1)
    installation_id: int = Field(gt=0, le=2**63 - 1)
    head_branch: str = Field(min_length=1, max_length=128)
    base_branch: str = Field(min_length=1, max_length=128)
    expected_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected_base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(max_length=16000)

    @field_validator("repo")
    @classmethod
    def repository(cls, value):
        # Canonical GitHub.com owner/name only, never URL, host, fork or API path.
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?/[a-z0-9][a-z0-9_.-]{0,99}", value):
            raise ValueError("invalid repository")
        if value.endswith((".git", ".", "-")) or ".." in value or "--" in value.split("/")[0]:
            raise ValueError("invalid repository")
        return value

    @field_validator("head_branch", "base_branch")
    @classmethod
    def branch(cls, value):
        # Deliberately narrower than git-check-ref-format; no shell, URL or refspec.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value):
            raise ValueError("invalid branch")
        if (".." in value or value == "HEAD" or value.startswith("refs/") or
                any(not part or part.startswith(".") or part.endswith((".", ".lock")) for part in value.split("/"))):
            raise ValueError("invalid branch")
        return value

    @field_validator("title", "body")
    @classmethod
    def text(cls, value, info):
        if any(ord(c) < 32 and (info.field_name == "title" or c not in "\n\r\t") for c in value):
            raise ValueError("invalid pull request text")
        if info.field_name == "title" and not value.strip():
            raise ValueError("invalid pull request text")
        return value

    @model_validator(mode="after")
    def distinct_refs(self):
        if self.head_branch == self.base_branch or self.expected_head_sha == self.expected_base_sha:
            raise ValueError("distinct branches and commits required")
        bounded_json(self.model_dump(), MAX_TOOL_INPUT_BYTES, tool=OPERATION)
        return self


def proposal(value):
    # Never expose Pydantic diagnostics containing model-controlled source/text.
    try:
        bounded_json(value, MAX_TOOL_INPUT_BYTES, tool=OPERATION)
        return PullRequestProposal.model_validate(value)
    except Exception:
        raise ValueError("invalid GitHub pull request proposal") from None


PROPOSAL_SCHEMA = PullRequestProposal.model_json_schema()
