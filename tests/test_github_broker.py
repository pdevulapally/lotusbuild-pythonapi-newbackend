"""Synthetic trusted-server boundary tests; no credential or network is live."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import copy
import json
from types import SimpleNamespace

import pytest

import app.agent.tools as tools
from app.execution.actions import ACTIONS_COLLECTION
from app.execution.errors import ExecutionStopped, ToolOutcomeUncertain
from app.execution.repository import LeaseLost
from app.github.broker import BrokerDenied, BrokerUnavailable, GitHubWriteBroker, RepositoryGrant
from app.github.client import InstallationClientLeases, InstallationCredential, ProviderFailure, _GitHubClient, _NoRedirect
from app.github.operations import OPERATION, PERMISSIONS, proposal
from app.store import SESSIONS_COLLECTION
from test_durable_runs import create, doc, env, seal_pending

INPUT = {"approval_scope": "named_branches", "repo": "fixture-owner/fixture-repo", "repository_id": 17, "installation_id": 23,
         "head_branch": "feature/local", "base_branch": "main", "expected_head_sha": "a" * 40,
         "expected_base_sha": "b" * 40, "title": "Synthetic title $(no-shell)", "body": "Synthetic body `no-shell`"}
RESULT = {"number": 31, "url": "https://github.com/fixture-owner/fixture-repo/pull/31"}
SYNTHETIC_SECRET = "synthetic-nonfunctional-credential-do-not-use"


@pytest.fixture
def enabled(monkeypatch):
    # Production registry remains disabled. Only this isolated mock harness may seal it.
    spec = next(t for t in tools.TOOL_SPECS if t.name == OPERATION)
    monkeypatch.setattr(spec, "enabled", True)


def approved(repo, uid="alice", *, decision=True):
    session = repo.submit(uid, "create", "create", {"task": "synthetic", "kind": "code"})
    first = repo.claim(session["active_run_id"], "worker")
    _, current = repo.start(first)
    seal_pending(repo, first, current, {"id": "approval", "calls": [
        {"id": "pr", "name": OPERATION, "input": copy.deepcopy(INPUT), "risky": True}]})
    repo.save(first, current)
    repo.finish(first)
    response = repo.submit(uid, "approve", "approve", {
        "approval_id": "approval", "decisions": [{"call_id": "pr", "approved": decision}]}, session["id"])
    claim = repo.claim(response["active_run_id"], "approved-worker")
    _, current = repo.start(claim)
    aid = current["pending_approval"]["calls"][0]["action_id"]
    return claim, current, aid


class Authority:
    def __init__(self, session, now):
        self.grant = RepositoryGrant(session["uid"], session["id"], INPUT["repo"], 17, 23,
            frozenset({INPUT["head_branch"]}), frozenset({INPUT["base_branch"]}), PERMISSIONS, now, now + 120)
        self.calls = 0
        self.fail = None
        self.entitlements = []

    def authorize(self, session, intent, entitlements):
        self.calls += 1
        self.entitlements.append(entitlements.tier_id)
        if self.fail:
            self.fail(self.calls)
        return self.grant


class Clients:
    def __init__(self):
        self.opens = self.verifies = self.posts = self.closes = 0
        self.before_verify = self.before_post = self.on_close = None
        self.result = RESULT

    @contextmanager
    def open(self, grant):
        self.opens += 1
        try:
            yield self
        finally:
            self.closes += 1
            if self.on_close:
                self.on_close()

    def verify(self, intent):
        self.verifies += 1
        if self.before_verify:
            self.before_verify()

    def create_pull_request(self, intent):
        self.posts += 1
        if self.before_post:
            self.before_post()
        return self.result


def harness(env):
    repo, db, clock = env
    claim, session, aid = approved(repo)
    auth, clients = Authority(session, clock[0]), Clients()
    broker = GitHubWriteBroker(repo, authority=auth, clients=clients, clock=lambda: clock[0])
    return SimpleNamespace(repo=repo, db=db, clock=clock, claim=claim, session=session, aid=aid,
                           auth=auth, clients=clients, broker=broker)


def execute(h, **changes):
    return h.broker.execute(h.claim, **{"action_id": h.aid, "operation": OPERATION,
                                        "input": copy.deepcopy(INPUT), **changes})


def unknown(h):
    assert doc(h.db, ACTIONS_COLLECTION, h.aid)["state"] == "outcome_unknown"
    with pytest.raises(ExecutionStopped):
        execute(h)


def test_default_broker_and_missing_trusted_adapter_fail_closed(env, enabled):
    repo, _, _ = env
    with pytest.raises(BrokerUnavailable):
        GitHubWriteBroker(repo).execute(None, action_id="x", operation=OPERATION, input=INPUT)
    h = harness(env)
    h.broker.authority = None
    with pytest.raises(BrokerUnavailable):
        execute(h)
    assert h.clients.opens == 0


def test_production_tool_has_no_execution_or_advertisement():
    spec = next(t for t in tools.TOOL_SPECS if t.name == OPERATION)
    assert spec.enabled is False and spec not in tools.supported_specs(object())
    with pytest.raises(PermissionError):
        tools.run_tool({"mode": "build"}, object(), OPERATION, INPUT)


def test_safe_result_receipt_and_duplicate_never_redispatches(env, enabled):
    h = harness(env)
    assert execute(h) == RESULT
    assert (h.clients.opens, h.clients.posts, h.clients.closes, h.auth.calls) == (1, 1, 1, 2)
    assert doc(h.db, ACTIONS_COLLECTION, h.aid)["state"] == "handler_returned"
    with pytest.raises(ExecutionStopped):
        execute(h)
    assert h.clients.posts == 1
    assert SYNTHETIC_SECRET not in json.dumps(h.db.data, default=str, skipkeys=True)
    assert INPUT["body"] not in str(h.repo.inspect_action(h.session["id"], "alice", h.aid))


@pytest.mark.parametrize("field,value", [
    ("repo", "https://evil.test/a/b"), ("repo", "fixture-owner/../evil"), ("repo", "owner/repo?token=x"),
    ("repo", "owner/repo#path"), ("repo", "Owner/repo"), ("repo", "owner/repo%2fother"),
    ("head_branch", "a;curl evil.test"), ("head_branch", "a\nmain"), ("head_branch", "-force"),
    ("head_branch", "owner:fork"), ("head_branch", "a/../b"), ("head_branch", "a%2fb"),
    ("head_branch", "refs/heads/main"), ("base_branch", "a.lock"), ("base_branch", "HEAD"),
    ("base_branch", "feature/local"), ("title", "a\nInjected"), ("title", " " * 4),
    ("body", "\x00"), ("expected_head_sha", "z" * 40), ("expected_base_sha", "a" * 40),
    ("repository_id", True), ("repository_id", "17"), ("installation_id", -1),
    ("url", "http://127.0.0.1/"), ("path", "../../secrets"), ("token", SYNTHETIC_SECRET),
    ("approval_scope", "exact_diff"), ("operation", "push"), ("content", "private source"), ("title", "x" * 257), ("body", "x" * 16001)])
def test_proposal_rejects_injection_unsupported_scope_and_bounds(field, value):
    with pytest.raises(ValueError, match="invalid GitHub") as error:
        proposal({**INPUT, field: value})
    assert SYNTHETIC_SECRET not in str(error.value)


def test_proposal_requires_body_and_bounds_actual_utf8():
    with pytest.raises(ValueError):
        proposal({k: v for k, v in INPUT.items() if k != "body"})
    with pytest.raises(ValueError):
        proposal({**INPUT, "body": "\U0001f600" * 9000})


@pytest.mark.parametrize("change", [
    {"uid": "other"}, {"session_id": "other"}, {"repo": "another/repo"}, {"repository_id": 18},
    {"installation_id": 24}, {"allowed_heads": frozenset({"other"})}, {"allowed_bases": frozenset({"other"})},
    {"permissions": frozenset({"contents:write", "pull_requests:write"})}, {"expires_at": 1000.0},
    {"issued_at": 1001.0}, {"expires_at": float("nan")}, {"expires_at": 1400.0}])
def test_wrong_tenant_repository_installation_allowlist_scope_and_expiry(env, enabled, change):
    h = harness(env)
    h.auth.grant = replace(h.auth.grant, **change)
    with pytest.raises(BrokerDenied):
        execute(h)
    assert h.clients.opens == 0
    assert doc(h.db, ACTIONS_COLLECTION, h.aid)["state"] == "prepared"


@pytest.mark.parametrize("change", [{"repo": "other/repo"}, {"head_branch": "other"},
                                    {"title": "changed"}, {"expected_head_sha": "c" * 40}])
def test_changed_sealed_input_rejects_without_credentials(env, enabled, change):
    h = harness(env)
    with pytest.raises(ExecutionStopped):
        execute(h, input={**INPUT, **change})
    assert h.clients.opens == 0


@pytest.mark.parametrize("case", ["plan", "denied", "approval", "expired", "model", "revoked"])
def test_current_policy_approval_and_entitlement_cannot_be_bypassed(env, enabled, case):
    h = harness(env)
    if case == "plan":
        h.db.data[(SESSIONS_COLLECTION, h.session["id"])]["mode"] = "plan"
    elif case == "denied":
        from app.execution.repository import JOBS_COLLECTION
        h.db.data[(JOBS_COLLECTION, h.claim.job_id)]["payload"]["decisions"][0]["approved"] = False
    elif case == "approval":
        from app.execution.repository import JOBS_COLLECTION
        h.db.data[(JOBS_COLLECTION, h.claim.job_id)]["approval_batch_id"] = "wrong"
    elif case == "expired":
        h.clock[0] += 46
    elif case == "model":
        h.db.data[("plans", "fixture-approval-alice")]["models"] = []
    else:
        h.auth.fail = lambda n: (_ for _ in ()).throw(RuntimeError(SYNTHETIC_SECRET))
    with pytest.raises(ExecutionStopped) as error:
        execute(h)
    assert SYNTHETIC_SECRET not in str(error.value)
    assert h.clients.opens == 0


@pytest.mark.parametrize("window", ["revoked", "expired", "lease", "provider", "oversized", "cleanup"])
def test_failures_after_permit_stay_unknown_and_redacted(env, enabled, window):
    h = harness(env)
    if window == "revoked":
        h.auth.fail = lambda n: (_ for _ in ()).throw(RuntimeError(SYNTHETIC_SECRET)) if n == 2 else None
    elif window == "expired":
        h.clients.before_verify = lambda: h.clock.__setitem__(0, 1201.0)
    elif window == "lease":
        h.clients.before_verify = lambda: h.clock.__setitem__(0, 1046.0)
    elif window == "provider":
        h.clients.before_post = lambda: (_ for _ in ()).throw(TimeoutError(SYNTHETIC_SECRET))
    elif window == "oversized":
        h.clients.result = {**RESULT, "raw": SYNTHETIC_SECRET * 3000}
    else:
        h.clients.on_close = lambda: (_ for _ in ()).throw(RuntimeError(SYNTHETIC_SECRET))
    with pytest.raises(ToolOutcomeUncertain) as error:
        execute(h)
    assert SYNTHETIC_SECRET not in str(error.value)
    before = h.clients.posts
    unknown(h)
    assert h.clients.posts == before and h.clients.closes == 1


@pytest.mark.parametrize("operation", ["permit_action", "record_action_result"])
def test_lost_committed_reply_never_repeats_post(env, enabled, monkeypatch, operation):
    h = harness(env)
    original = getattr(h.repo, operation)
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError(SYNTHETIC_SECRET)
    monkeypatch.setattr(h.repo, operation, lost)
    with pytest.raises(ToolOutcomeUncertain):
        execute(h)
    assert h.clients.posts == (0 if operation == "permit_action" else 1)
    assert doc(h.db, ACTIONS_COLLECTION, h.aid)["state"] == (
        "outcome_unknown" if operation == "permit_action" else "handler_returned")
    with pytest.raises(ExecutionStopped):
        execute(h)
    assert h.clients.posts <= 1


def test_competing_dispatches_issue_one_post(env, enabled):
    h = harness(env)
    def attempt(_):
        try:
            return execute(h)
        except ExecutionStopped:
            return None
    with ThreadPoolExecutor(6) as pool:
        assert sum(v is not None for v in pool.map(attempt, range(6))) == 1
    assert h.clients.posts == 1


def test_provider_never_runs_in_transaction_and_unsupported_operation_denied(env, enabled):
    h = harness(env)
    h.clients.before_post = lambda: (h.db.lock.acquire(blocking=False) and h.db.lock.release()) or None
    def check():
        assert h.db.lock.acquire(blocking=False)
        h.db.lock.release()
    h.clients.before_post = check
    with pytest.raises(BrokerDenied):
        execute(h, operation="push")
    assert execute(h) == RESULT


def test_authority_control_flow_exception_is_redacted_before_permit(env, enabled):
    h = harness(env)
    h.auth.fail = lambda n: (_ for _ in ()).throw(ExecutionStopped(SYNTHETIC_SECRET))
    with pytest.raises(BrokerDenied) as error:
        execute(h)
    assert SYNTHETIC_SECRET not in str(error.value) and h.clients.opens == 0
    assert doc(h.db, ACTIONS_COLLECTION, h.aid)["state"] == "prepared"


@pytest.mark.parametrize("case", ["plan", "model", "issuer"])
def test_postpermit_fresh_policy_and_adapter_failures_stop_without_retry(env, enabled, case):
    h = harness(env)
    if case == "plan":
        h.clients.before_verify = lambda: h.db.data[(SESSIONS_COLLECTION, h.session["id"])].update(mode="plan")
    elif case == "model":
        h.clients.before_verify = lambda: h.db.data[("plans", "fixture-approval-alice")].update(models=[])
    else:
        class Issuer:
            def issue(self, grant): raise RuntimeError(SYNTHETIC_SECRET)
        h.broker.clients = InstallationClientLeases(Issuer())
    with pytest.raises(ToolOutcomeUncertain) as error:
        execute(h)
    assert SYNTHETIC_SECRET not in str(error.value)
    assert h.clients.posts == 0
    unknown(h)


class Response:
    def __init__(self, value, status=200):
        self.raw = json.dumps(value).encode() if not isinstance(value, bytes) else value
        self.status = status
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, bound):
        assert bound == 64001
        return self.raw[:bound]


def native(monkeypatch, values):
    grant = RepositoryGrant("alice", "session", INPUT["repo"], 17, 23, frozenset({"feature/local"}),
                            frozenset({"main"}), PERMISSIONS, 1000, 1120)
    credential = InstallationCredential(SYNTHETIC_SECRET, 17, 23, PERMISSIONS, 1000, 4600, 1120)
    calls = []
    class Opener:
        def open(self, req, timeout):
            assert timeout == 10
            calls.append(req)
            item = values[len(calls) - 1]
            if isinstance(item, Exception): raise item
            return item
    def build(*handlers):
        assert isinstance(handlers[0], __import__('urllib.request').request.ProxyHandler)
        assert handlers[0].proxies == {} and isinstance(handlers[1], _NoRedirect)
        return Opener()
    monkeypatch.setattr("urllib.request.build_opener", build)
    client = _GitHubClient(credential, grant, clock=lambda: 1000)
    return client, credential, calls, grant


def provider_evidence():
    return [Response({"id": 17, "full_name": INPUT["repo"], "owner": {"login": "fixture-owner"},
                      "archived": False, "disabled": False}),
            Response({"ref": "refs/heads/feature/local", "object": {"type": "commit", "sha": "a" * 40}}),
            Response({"ref": "refs/heads/main", "object": {"type": "commit", "sha": "b" * 40}})]


def pr_response(**changes):
    value = {"number": 31, "html_url": RESULT["url"], "title": INPUT["title"], "body": INPUT["body"],
             "head": {"ref": INPUT["head_branch"], "sha": "a" * 40, "repo": {"id": 17, "full_name": INPUT["repo"]}},
             "base": {"ref": "main", "sha": "b" * 40, "repo": {"id": 17, "full_name": INPUT["repo"]}}}
    return Response({**value, **changes}, 201)


def test_native_fixed_origin_json_no_shell_and_no_authorization_retained(monkeypatch):
    client, credential, calls, _ = native(monkeypatch, provider_evidence() + [pr_response()])
    intent = proposal(INPUT)
    client.verify(intent)
    assert client.create_pull_request(intent) == RESULT
    assert len(calls) == 4
    assert all(c.full_url.startswith("https://api.github.com/") for c in calls)
    assert all(not c.has_header("Authorization") for c in calls)
    assert calls[1].full_url.endswith("feature%2Flocal")
    assert json.loads(calls[-1].data)["title"] == INPUT["title"]
    assert SYNTHETIC_SECRET not in repr(credential) + repr(client)


@pytest.mark.parametrize("bad", [Response({"message": SYNTHETIC_SECRET}, 401),
    Response({"message": SYNTHETIC_SECRET}, 302), Response(b"x" * 64001), Response(b"not json"),
    TimeoutError(SYNTHETIC_SECRET), Response([])])
def test_native_error_response_bounded_redacted_and_not_retried(monkeypatch, bad):
    client, _, calls, _ = native(monkeypatch, [bad])
    with pytest.raises(ProviderFailure) as error:
        client.verify(proposal(INPUT))
    assert SYNTHETIC_SECRET not in str(error.value)
    assert len(calls) == 1 and not calls[0].has_header("Authorization")


@pytest.mark.parametrize("field,value", [("id", 18), ("full_name", "foreign/repo"),
                                         ("owner", {"login": "foreign"}), ("archived", True)])
def test_native_wrong_repository_metadata_rejects(monkeypatch, field, value):
    evidence = provider_evidence()
    metadata = json.loads(evidence[0].raw)
    evidence[0] = Response({**metadata, field: value})
    client, _, calls, _ = native(monkeypatch, evidence)
    with pytest.raises(ProviderFailure):
        client.verify(proposal(INPUT))
    assert len(calls) == 1


@pytest.mark.parametrize("change", [{"ref": "refs/heads/foreign"},
    {"object": {"type": "commit", "sha": "c" * 40}}, {"object": {"type": "tag", "sha": "a" * 40}}])
def test_native_changed_branch_or_commit_rejects_before_post(monkeypatch, change):
    values = provider_evidence()
    values[1] = Response({**json.loads(values[1].raw), **change})
    client, _, calls, _ = native(monkeypatch, values)
    with pytest.raises(ProviderFailure):
        client.verify(proposal(INPUT))
    assert len(calls) == 2 and all(c.method == "GET" for c in calls)


def test_native_direct_branch_allowlist_binding_before_network(monkeypatch):
    client, _, calls, _ = native(monkeypatch, [])
    with pytest.raises(ProviderFailure):
        client.create_pull_request(proposal({**INPUT, "head_branch": "unapproved"}))
    assert calls == []


def test_server_only_credential_end_to_end_is_not_in_receipts_events_or_output(env, enabled, monkeypatch):
    h = harness(env)
    _, credential, calls, _ = native(monkeypatch, provider_evidence() + [pr_response()])
    class Issuer:
        def issue(self, grant): return credential
        def revoke(self, value): assert value is credential
    h.broker.clients = InstallationClientLeases(Issuer(), clock=lambda: h.clock[0])
    result = execute(h)
    assert result == RESULT and len(calls) == 4 and credential.secret == ""
    assert SYNTHETIC_SECRET not in str(h.db.data) + str(result)
    assert all(not req.has_header("Authorization") for req in calls)


@pytest.mark.parametrize("changes", [{"html_url": "https://evil.test/secret"}, {"number": True},
    {"title": "changed"}, {"head": {"ref": "feature/local", "sha": "c" * 40, "repo": {"id": 17}}}])
def test_native_ambiguous_write_evidence_is_not_success(monkeypatch, changes):
    client, _, calls, _ = native(monkeypatch, [pr_response(**changes)])
    with pytest.raises(ProviderFailure):
        client.create_pull_request(proposal(INPUT))
    assert len(calls) == 1


def test_redirect_handler_never_visits_user_url():
    with pytest.raises(ProviderFailure):
        _NoRedirect().redirect_request(None, None, 302, "", {}, "http://127.0.0.1/secrets")


@pytest.mark.parametrize("change", [{"repository_id": 18}, {"installation_id": 24},
    {"permissions": frozenset({"contents:write"})}, {"expires_at": 999}, {"use_until": 1400},
    {"secret": "bad\r\nheader"}, {"issued_at": float("nan")}])
def test_credential_scope_ttl_and_header_injection_fail_closed(monkeypatch, change):
    client, credential, calls, _ = native(monkeypatch, [])
    for k, v in change.items(): setattr(credential, k, v)
    with pytest.raises(ProviderFailure): client.verify(proposal(INPUT))
    assert calls == []


@pytest.mark.parametrize("fail", [False, True])
def test_server_lease_revokes_drops_secret_and_redacts(monkeypatch, fail):
    _, credential, _, grant = native(monkeypatch, [])
    class Issuer:
        revoked = 0
        def issue(self, g): return credential
        def revoke(self, c):
            self.revoked += 1
            if fail: raise RuntimeError(SYNTHETIC_SECRET)
    issuer = Issuer()
    leases = InstallationClientLeases(issuer, clock=lambda: 1000)
    if fail:
        with pytest.raises(ProviderFailure) as error:
            with leases.open(grant): pass
        assert SYNTHETIC_SECRET not in str(error.value)
    else:
        with leases.open(grant) as client: assert client._credential is credential
        assert client._credential is None
    assert issuer.revoked == 1 and credential.secret == ""
