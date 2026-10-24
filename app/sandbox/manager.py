import os

from e2b import Sandbox as CodeSandbox
from e2b_desktop import Sandbox as DesktopSandbox

from app.firebase import db

# Every E2B sandbox product E2B ships maps to one SDK class here. Everything
# that varies per deployment (template, timeout) comes from env vars keyed by
# kind, not from code - add a new kind by setting E2B_<KIND>_TEMPLATE, no edits below.
_BACKENDS = {"code": CodeSandbox, "desktop": DesktopSandbox}
REGISTRY_COLLECTION = "agent_v2_sandbox_registry"


def _kind_config(kind: str) -> dict:
    prefix = f"E2B_{kind.upper()}_"
    return {
        "template": os.environ.get(prefix + "TEMPLATE") or None,
        "timeout": int(os.environ.get(prefix + "TIMEOUT", "300")),
    }


class SandboxManager:
    """Owns the E2B sandbox behind each agent session via a Firestore registry
    (session_id -> e2b sandbox_id), reconnecting per call via E2B's own
    Sandbox.connect() instead of holding a live object in process memory - so
    any instance behind a load balancer can serve any session's sandbox.
    """

    def create(self, session_id: str, kind: str) -> object:
        if kind not in _BACKENDS:
            raise ValueError(f"unknown sandbox kind: {kind!r}, expected one of {sorted(_BACKENDS)}")
        config = _kind_config(kind)
        kwargs = {"timeout": config["timeout"]}
        if config["template"]:
            kwargs["template"] = config["template"]
        sandbox = _BACKENDS[kind].create(**kwargs)
        db.collection(REGISTRY_COLLECTION).document(session_id).set(
            {"kind": kind, "sandbox_id": sandbox.sandbox_id, "stream_started": False}
        )
        return sandbox

    def get(self, session_id: str) -> object | None:
        doc = db.collection(REGISTRY_COLLECTION).document(session_id).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        kind = data["kind"]
        try:
            return _BACKENDS[kind].connect(data["sandbox_id"], timeout=_kind_config(kind)["timeout"])
        except Exception as exc:
            raise ValueError(
                f"sandbox for session {session_id} is no longer available - start a new session"
            ) from exc

    def close(self, session_id: str) -> None:
        # Connection failure does not prove termination. Retain the registry
        # so durable cleanup can retry/reconcile rather than orphan a resource.
        sandbox = self.get(session_id)
        if sandbox is not None:
            sandbox.kill()
        db.collection(REGISTRY_COLLECTION).document(session_id).delete()

    def preview_url(self, session_id: str, port: int) -> str:
        return f"https://{self._require(session_id).get_host(port)}"

    def desktop_stream_url(self, session_id: str) -> str:
        ref = db.collection(REGISTRY_COLLECTION).document(session_id)
        doc = ref.get()
        if not doc.exists:
            raise ValueError(f"no sandbox for session {session_id}")
        sandbox = self._require(session_id)
        if not hasattr(sandbox, "stream"):
            raise ValueError(f"session {session_id} has no desktop stream (not a desktop sandbox)")
        if not doc.to_dict().get("stream_started"):
            sandbox.stream.start()
            ref.update({"stream_started": True})
        return sandbox.stream.get_url()

    def screenshot(self, session_id: str) -> bytes:
        sandbox = self._require(session_id)
        if not hasattr(sandbox, "screenshot"):
            raise ValueError(f"session {session_id} has no screenshot capability (not a desktop sandbox)")
        return sandbox.screenshot()

    def _require(self, session_id: str) -> object:
        sandbox = self.get(session_id)
        if sandbox is None:
            raise ValueError(f"no sandbox for session {session_id}")
        return sandbox


manager = SandboxManager()
