"""Trusted-server-only GitHub boundary; deliberately no runtime registration.

No route, child RPC, environment switch, OAuth-token fallback or default adapters.
The agent tool stays disabled. Tests inject synthetic authority and client leases.
"""
import math
import time
from dataclasses import dataclass
from typing import ContextManager, Protocol

from app.agent.policy import require_tool
from app.billing.entitlements import resolve_entitlements
from app.billing.model_select import selected_model
from app.execution.errors import ExecutionStopped, ToolOutcomeUncertain
from app.execution.repository import _digest
from app.github.operations import OPERATION, PERMISSIONS, PullRequestProposal, proposal

MAX_AUTHORIZATION_SECONDS = 300  # Technical freshness ceiling, not a paid allowance.


class BrokerUnavailable(ExecutionStopped):
    def __init__(self):
        super().__init__("GitHub write broker is unavailable")


class BrokerDenied(ExecutionStopped):
    def __init__(self):
        super().__init__("GitHub write authorization denied")


@dataclass(frozen=True, repr=False)
class RepositoryGrant:
    """Returned only by a trusted authority; never loaded from an agent session.

    The authority must verify fresh account authentication/revocation, installation
    ownership, repository allowlisting and product policy without inventing tiers.
    This object records that decision, not an implementation of those services.
    """
    uid: str
    session_id: str
    repo: str
    repository_id: int
    installation_id: int
    allowed_heads: frozenset[str]
    allowed_bases: frozenset[str]
    permissions: frozenset[str]
    issued_at: float
    expires_at: float


class WriteAuthority(Protocol):
    # Fresh revocation/status checks must return the same current grant, including
    # its lifetime. A changed grant requires a new proposal/approval, never retry.
    def authorize(self, session: dict, intent: PullRequestProposal, entitlements) -> RepositoryGrant: ...


class ClientLeases(Protocol):
    def open(self, grant: RepositoryGrant) -> ContextManager: ...


def require_grant(grant, session, intent, now):
    if (type(grant) is not RepositoryGrant or
            (grant.uid, grant.session_id, grant.repo, grant.repository_id, grant.installation_id) !=
            (session["uid"], session["id"], intent.repo, intent.repository_id, intent.installation_id) or
            type(grant.repository_id) is not int or type(grant.installation_id) is not int or
            type(grant.allowed_heads) is not frozenset or type(grant.allowed_bases) is not frozenset or
            type(grant.permissions) is not frozenset or grant.permissions != PERMISSIONS or
            intent.head_branch not in grant.allowed_heads or intent.base_branch not in grant.allowed_bases or
            any(type(n) not in (int, float) or not math.isfinite(n) for n in (grant.issued_at, grant.expires_at)) or
            not grant.issued_at <= now < grant.expires_at <= grant.issued_at + MAX_AUTHORIZATION_SECONDS):
        raise BrokerDenied()


class GitHubWriteBroker:
    def __init__(self, repository, *, authority: WriteAuthority | None = None,
                 clients: ClientLeases | None = None, clock=time.time):
        self.repository, self.authority, self.clients, self.clock = repository, authority, clients, clock

    def _authorize(self, claim, action_id, intent, *, expected_state="prepared"):
        # All reads/rechecks use the existing transaction authority. No provider
        # I/O, credential acquisition or callback to external authority inside it.
        def inspect(tx, jr, sr, job, session):
            from app.agent.tools import tool_spec
            from app.execution.workspaces import require_unlocked
            spec = tool_spec(OPERATION)
            require_unlocked(session)
            require_tool(session, spec)
            _, item = self.repository._action(tx, job, session, action_id)
            if (job["state"] != "running" or item["state"] != expected_state or
                    item["name"] != OPERATION or item["input_digest"] != _digest(intent.model_dump()) or
                    item.get("risky") is not True or item.get("effectful") is not True):
                raise BrokerDenied()
            if expected_state == "outcome_unknown":
                permit = item.get("permit", {})
                if ((permit.get("run_id"), permit.get("owner"), permit.get("fence")) !=
                        (claim.job_id, claim.owner, claim.token) or action_id not in job.get("open_actions", [])):
                    raise BrokerDenied()
            # Always resolve current entitlements, including historical sessions.
            entitlements = resolve_entitlements(session["uid"], client=self.repository.db,
                                                transaction=tx, now=self.repository.clock())
            selected_model(session, entitlements, session.get("pending_approval"))
            return session, entitlements
        try:
            session, entitlements = self.repository._authority_tx(claim, inspect)
        except ExecutionStopped:
            raise
        except Exception:
            raise BrokerDenied() from None
        try:
            grant = self.authority.authorize(session, intent, entitlements)
            require_grant(grant, session, intent, self.clock())
        except Exception:
            # Adapter control-flow exceptions are still untrusted diagnostics.
            raise BrokerDenied() from None
        return session, grant

    def execute(self, claim, *, action_id, operation, input):
        if self.authority is None or self.clients is None:
            raise BrokerUnavailable()
        if operation != OPERATION:
            raise BrokerDenied()
        intent = proposal(input)
        session, grant = self._authorize(claim, action_id, intent)
        # This one-shot local transition precedes ANY credential/provider access.
        # Lost commit replies are uncertain and may never trigger a dispatch.
        try:
            self.repository.permit_action(claim, action_id=action_id, name=OPERATION, input=intent.model_dump())
        except ExecutionStopped:
            raise
        except Exception:
            raise ToolOutcomeUncertain(OPERATION) from None
        try:
            with self.clients.open(grant) as client:
                # Refresh account/revocation/installation/policy after credential
                # acquisition, then check durable cancellation/lease immediately
                # before dispatch. No tokens are returned by this interface.
                client.verify(intent)
                _, current = self._authorize(claim, action_id, intent, expected_state="outcome_unknown")
                if current != grant:
                    raise BrokerDenied()
                require_grant(grant, session, intent, self.clock())
                self.repository.checkpoint(claim)
                result = client.create_pull_request(intent)
                # Only canonical local projection enters the result receipt/model.
                from app.github.client import safe_result
                result = safe_result(result, intent)
            self.repository.record_action_result(claim, action_id=action_id, result_digest=_digest(result))
            return result
        except Exception:
            # Includes remote 4xx/redirects/timeouts, lease/revocation failure,
            # ambiguous cleanup and lost result replies. Never retry/clear/refund.
            raise ToolOutcomeUncertain(OPERATION) from None
