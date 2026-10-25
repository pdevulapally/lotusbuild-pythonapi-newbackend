# GitHub write broker: disabled local foundation

Bounded local slice, 2026-10-25. No GitHub credentials, grants, installations,
tokens, PRs or API writes were created. Existing reviewed changes are preserved.

## Supported proposal and approval meaning

The sole operation is `create_pull_request` between **existing named branches in
the same GitHub.com repository**. The strict typed proposal requires:

- `approval_scope`: exactly `named_branches`;
- canonical lowercase `repo` (`owner/name`), positive immutable `repository_id`
  and `installation_id` (proposed identifiers confer no authority);
- `head_branch`, `base_branch`, `expected_head_sha`, `expected_base_sha`;
- explicit `title` and `body`, including an empty body if intended.

The existing sealed approval binds every submitted field, including that scope,
the exact PR text, repository/installation identifiers, branch names and observed
commit SHAs. The authority independently matches identifiers to a trusted binding.
It cannot take ownership, permission, URLs or credentials from an agent session.
Approval of `named_branches` means opening a PR whose branch contents can change.
It **does not approve an immutable exact diff**. `exact_diff` is rejected.

Expected SHAs are conservative drift checks before POST and on returned evidence.
GitHub's PR creation endpoint accepts branch names and has no expected-SHA CAS
parameter. Another writer can change either branch after verification. A changed
response can therefore leave an **already-created PR** with `outcome_unknown`;
there is no retry, compensating delete, inferred no-effect, or automatic recovery.
Even a matching response does not freeze future branch contents. Commit pairs
identify content observations; no diff is downloaded, applied or published here.

There is no ref-exclusion adapter or locking infrastructure. A local mutex cannot
exclude remote writers. An installation's permissions alone cannot freeze refs
against other authorized writers/admins. No such production guarantee was proved.
A supported production version can offer a separately reviewed named-branch PR
policy with an approval surface explaining mutable contents, fresh permissions,
trusted installation/repository allowlists and scoped issuance/revocation. An
immutable-diff requirement needs a different verified provider-supported operation
or workflow with its own explicit approvals and enforced writer policy; the
current native PR endpoint is insufficient. That design is outside this slice.

No pushes, commits, file writes, merges, workflow dispatch, settings changes,
forks, GitHub Enterprise hosts, arbitrary paths, URLs or generic HTTP are supported.
Unknown fields/operations/scopes reject. Branch syntax is deliberately narrow;
there are no shell commands, Git hooks or URL-driven fetches. Text is untrusted
data passed as bounded JSON. Tool input is bounded by the existing 32 KB limit;
title/body have additional bounds. No invented commercial entitlement is applied.

## One receipt and permission authority

`GitHubWriteBroker` is an explicitly injected trusted-server service. It reuses
`RunRepository._authority_tx`, the existing claim/fence, lease, cancellation,
deadline, workspace exclusion, session ownership and exact sealed action identity.
Existing model entitlements are resolved at execution; a model entitlement is
not a GitHub commercial entitlement. A separate trusted authority must supply the
actual account authentication/revocation and installation/repository policy.

All GitHub proposals remain consequential/risky even if risk-registry data changes.
The existing `permit_action` verifies the exact current approval batch and decision
and atomically transitions `prepared` to `outcome_unknown` once. Plan mode rejects
before dispatch, including forged child state. No provider call occurs inside a
Firestore transaction, whose SDK may retry the transaction callback.

Credential/provider access follows the one-shot permit. Fresh persisted session,
model entitlement, authentication/revocation and grant checks run again after
provider verification and before POST. Reauthorization must return the same
current grant including its lifetime, after checking live status. A changed grant
denies rather than silently adopting new permission. The clock enforces finite
grant lifetimes with a technical five-minute ceiling, not a paid allowance.
Authorization can change after its final check; these reads and GitHub are not
one atomic transaction. A provider rejection remains conservative unknown.

Only a bounded canonical PR number and GitHub.com URL can return. The result
receipt stores their digest after client cleanup, never raw provider diagnostics.
The existing `handler_returned` state still means a handler returned evidence;
it does not establish exactly-once remote effects or trusted recovery resolution.

Lost permit/result replies, provider errors/timeouts/redirects, changed evidence,
credential/grant expiry, authorization revocation, cleanup errors and crashes do
not authorize redispatch. A committed result with a lost local reply may already
be `handler_returned`; inspect its receipt rather than posting again. Stable
action identity is reused, with no separate retry key or second effect ledger.

## Credential and provider boundaries

The former unused unsafe `client.create_pull_request(token, repo, ...)` helper is
removed. Legacy encrypted long-lived UID OAuth tokens are not used by this broker.
No token enters tool input/output, agent/model state, sandbox files, commands,
environment variables, events, receipts or application logs through this service.

`InstallationClientLeases` accepts a trusted issuer, not an agent token. Issuance
must narrow to one immutable repository ID and exact `contents:read` /
`pull_requests:write` permissions under the verified installation. Credentials are
server-private and repr-hidden. Their **actual upstream expiry** (GitHub documents
one-hour installation tokens) is separate from a local `use_until` capped at five
minutes. Every request checks both, plus the grant expiry. Lease exit requests
revocation and drops references. Python immutable strings/OS memory are not
claimed to be securely erased. Real issuer behavior/revocation remains unverified.

The narrow REST client uses only fixed `https://api.github.com` paths constructed
from validated identifiers, no environment proxy, no redirects and no retries.
Each request has a 10-second timeout setting and 64 KB read limit. These settings
are not hard wall-clock process containment. Repository ID/name/owner and exact
branch refs/commit observations are checked. PR response IDs/refs/commits/text and
canonical result URL are verified; provider URLs are never fetched. HTTP errors
and external adapter exceptions are replaced with fixed diagnostics, including
adapter exceptions derived from execution control-flow classes.

## Production gates and configuration

The named tool remains `enabled=False`; direct dispatch still rejects. No runtime
route, child RPC, worker hook, enabling ENV switch or default adapter was added.
Missing authority/client adapters raise `BrokerUnavailable`. Tests temporarily
enable only a synthetic local registry and inject mock authority/client leases.

Real account/project/session alignment, verified fresh-auth policy and trusted
installation/repository grant storage do not exist in this checkout. Production
requires those contracts, a real scoped issuer/revoker, the supported named-branch
approval policy/UI, credential-isolated service deployment and supervised bounded
provider calls, trusted outcome reconciliation, and separately authorized isolated
end-to-end review. The worker child currently inherits configured service
credentials; adding this service to child RPC would not prove process isolation.
Linux descendant containment and parent DB/finalization delays remain open.

No new configuration variable names are introduced. Existing OAuth names are
`GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_REDIRECT_URI`,
`GITHUB_OAUTH_SUCCESS_REDIRECT_URL`, `CREDENTIAL_ENCRYPTION_KEY`; none enables this
broker or supplies the missing installation policy. Local ENV remains unset.
`ENABLE_EXPERIMENTAL_WORKER` stays guarded. No secret values were read or created.

Primary provider contract evidence, read-only documentation:

- [PR creation](https://docs.github.com/en/rest/pulls/pulls#create-a-pull-request)
- [Installation authentication and scope](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation)
- [Supported API versions](https://docs.github.com/en/rest/about-the-rest-api/api-versions)

Local verification evidence and exact final totals are recorded in
`NEXT_TASK_HANDOFF.md` and task-6 JUnit/log artifacts. No live integration or
production readiness follows from local tests.
