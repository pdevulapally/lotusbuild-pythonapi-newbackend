"""Plan capability policy shared by rendering, dispatch and parent receipts."""
from app.execution.errors import ExecutionStopped


class ToolPolicyDenied(ExecutionStopped):
    pass


def session_mode(session):
    # Missing means a historical native Build session. Present invalid values
    # never default to Build, including null and alternate casing.
    mode = session.get("mode", "build")
    if type(mode) is not str or mode not in {"build", "plan"}:
        raise ToolPolicyDenied("invalid persisted session mode")
    return mode


def tool_allowed(spec, mode):
    if mode == "build":
        return True
    if mode != "plan":
        raise ToolPolicyDenied("invalid persisted session mode")
    # Pinned server readOnlyTools. A capability must match the registered name;
    # aliases and wrappers cannot inherit permission by copying a read spec.
    return spec.name in {"read_file", "list_files", "search_files"} and spec.plan_capability == spec.name


def require_tool(session, spec):
    if session.get("task_authorization_id") and spec.name not in session.get("task_allowed_tools", []):
        raise ToolPolicyDenied("tool exceeds task authorization")
    if not tool_allowed(spec, session_mode(session)):
        raise ToolPolicyDenied("tool is not permitted in Plan mode")
