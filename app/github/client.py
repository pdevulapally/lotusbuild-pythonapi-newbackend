"""Narrow GitHub.com REST boundary. No shell, redirects, retries or OAuth fallback.

Only a trusted installation issuer may construct credential leases. There is no
default issuer or production registration; legacy UID OAuth tokens are unrelated.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import math
import time
import urllib.parse
import urllib.request

from app.github.operations import PERMISSIONS, PullRequestProposal

MAX_RESPONSE_BYTES = 64_000
MAX_CREDENTIAL_SECONDS = 300
MAX_PROVIDER_TOKEN_SECONDS = 3600
REQUEST_TIMEOUT_SECONDS = 10


class ProviderFailure(Exception):
    def __init__(self):
        super().__init__("GitHub provider operation did not return verified evidence")


@dataclass(repr=False)
class InstallationCredential:
    # Entire object is server-private; never serialize it into a receipt/event.
    secret: str = field(repr=False)
    repository_id: int
    installation_id: int
    permissions: frozenset[str]
    issued_at: float
    # Actual upstream expiry and a separate shorter local use deadline.
    expires_at: float
    use_until: float


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderFailure()


def safe_result(value, intent):
    if (not isinstance(value, dict) or set(value) != {"number", "url"} or
            type(value["number"]) is not int or not 0 < value["number"] <= 2**63 - 1 or
            value["url"] != f"https://github.com/{intent.repo}/pull/{value['number']}"):
        raise ProviderFailure()
    return {"number": value["number"], "url": value["url"]}


class InstallationClientLeases:
    """Issuer must mint a fresh single-repository installation credential.

    issuer.issue(grant) and issuer.revoke(credential) run only in trusted server
    code. No implementation/configuration is supplied by this bounded foundation.
    Failure during revocation is conservatively uncertain to the broker.
    """
    def __init__(self, issuer, *, clock=time.time):
        self.issuer, self.clock = issuer, clock

    @contextmanager
    def open(self, grant):
        credential = None
        client = None
        try:
            credential = self.issuer.issue(grant)
            client = _GitHubClient(credential, grant, clock=self.clock)
            yield client
        except Exception:
            raise ProviderFailure() from None
        finally:
            if client is not None:
                client.close()
            if credential is not None:
                try:
                    self.issuer.revoke(credential)
                except Exception:
                    raise ProviderFailure() from None
                finally:
                    # Drops references, not a claim of erasing Python/OS memory.
                    if type(credential) is InstallationCredential:
                        credential.secret = ""


class _GitHubClient:
    def __init__(self, credential, grant, *, clock=time.time):
        self._credential, self._grant, self._clock = credential, grant, clock
        # Do not inherit HTTP(S)_PROXY or agent-controlled proxy configuration.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._check()

    def _check(self):
        c, g, now = self._credential, self._grant, self._clock()
        if (type(c) is not InstallationCredential or type(c.secret) is not str or
                not 16 <= len(c.secret) <= 512 or any(ord(ch) < 33 or ord(ch) > 126 for ch in c.secret) or
                type(c.repository_id) is not int or type(c.installation_id) is not int or
                (c.repository_id, c.installation_id, c.permissions) != (g.repository_id, g.installation_id, PERMISSIONS) or
                type(c.permissions) is not frozenset or
                any(type(n) not in (int, float) or not math.isfinite(n) for n in (c.issued_at, c.expires_at, c.use_until)) or
                not c.issued_at <= now < c.use_until <= min(c.expires_at, c.issued_at + MAX_CREDENTIAL_SECONDS) or
                not c.expires_at <= c.issued_at + MAX_PROVIDER_TOKEN_SECONDS or
                not g.issued_at <= now < g.expires_at):
            raise ProviderFailure()

    def close(self):
        self._credential = None
        self._opener = None

    def _request(self, method, path, payload=None):
        self._check()
        request = None
        try:
            request = urllib.request.Request("https://api.github.com" + path,
                data=json.dumps(payload).encode() if payload is not None else None, method=method,
                headers={"Authorization": "Bearer " + self._credential.secret,
                         "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                         "User-Agent": "LotusBuild-credential-broker", "X-GitHub-Api-Version": "2026-03-10"})
            # urllib has no implicit retries; opener rejects every redirect.
            with self._opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                if response.status != (201 if method == "POST" else 200):
                    raise ProviderFailure()
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ProviderFailure()
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ProviderFailure()
            return value
        except Exception:
            raise ProviderFailure() from None
        finally:
            if request is not None:
                request.remove_header("Authorization")

    def _bind(self, intent):
        # Defensive even for internal direct invocation; names are never URLs.
        if type(intent) is not PullRequestProposal:
            raise ProviderFailure()
        from app.github.operations import proposal
        try:
            proposal(intent.model_dump())
        except Exception:
            raise ProviderFailure() from None
        if (intent.repo, intent.repository_id, intent.installation_id) != (
                self._grant.repo, self._grant.repository_id, self._grant.installation_id):
            raise ProviderFailure()
        if intent.head_branch not in self._grant.allowed_heads or intent.base_branch not in self._grant.allowed_bases:
            raise ProviderFailure()

    def verify(self, intent):
        self._bind(intent)
        repo = self._request("GET", f"/repositories/{intent.repository_id}")
        if (type(repo.get("id")) is not int or repo["id"] != intent.repository_id or
                repo.get("full_name", "").lower() != intent.repo or
                repo.get("owner", {}).get("login", "").lower() != intent.repo.split("/")[0] or
                repo.get("archived") is not False or repo.get("disabled") is not False):
            raise ProviderFailure()
        for branch, sha in [(intent.head_branch, intent.expected_head_sha), (intent.base_branch, intent.expected_base_sha)]:
            ref = self._request("GET", f"/repos/{intent.repo}/git/ref/heads/{urllib.parse.quote(branch, safe='')}")
            if (ref.get("ref") != "refs/heads/" + branch or ref.get("object", {}).get("type") != "commit" or
                    ref["object"].get("sha") != sha):
                raise ProviderFailure()

    def create_pull_request(self, intent):
        self._bind(intent)
        result = self._request("POST", f"/repos/{intent.repo}/pulls", {
            "title": intent.title, "body": intent.body, "head": intent.head_branch,
            "base": intent.base_branch, "maintainer_can_modify": False})
        try:
            for side, branch, sha in [("head", intent.head_branch, intent.expected_head_sha),
                                      ("base", intent.base_branch, intent.expected_base_sha)]:
                if (result[side]["ref"] != branch or result[side]["sha"] != sha or
                        type(result[side]["repo"]["id"]) is not int or
                        result[side]["repo"]["id"] != intent.repository_id or
                        result[side]["repo"]["full_name"].lower() != intent.repo):
                    raise ProviderFailure()
            if result["title"] != intent.title or (result["body"] or "") != intent.body:
                raise ProviderFailure()
            return safe_result({"number": result["number"], "url": result["html_url"]}, intent)
        except Exception:
            raise ProviderFailure() from None
