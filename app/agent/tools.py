import base64
import shlex
from dataclasses import dataclass, field
from typing import Callable

from e2b.sandbox.commands.command_handle import CommandExitException


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict
    handler: Callable[[dict, object, dict], object]
    # Decides whether a given live sandbox exposes this tool, by checking the
    # attribute the handler needs - not by a hand-maintained per-kind list that
    # can drift out of sync with what the sandbox object actually supports.
    supported: Callable[[object], bool] = field(default=lambda sandbox: True)


@dataclass
class VirtualToolSpec:
    """A tool that mutates session state directly instead of touching a sandbox -
    e.g. the model's own plan, or its persistent memory about the user."""

    name: str
    description: str
    schema: dict
    handler: Callable[[dict, dict], object]


def _needs(attr: str) -> Callable[[object], bool]:
    return lambda sandbox: hasattr(sandbox, attr)


def _run_bash(sandbox, command: str, envs: dict | None = None) -> str:
    try:
        result = sandbox.commands.run(command, envs=envs)
    except CommandExitException as exc:
        result = exc
    output = (result.stdout or "") + (result.stderr or "")
    return output or f"(exit {result.exit_code}, no output)"


def _bash(session: dict, sandbox, input: dict) -> str:
    return _run_bash(sandbox, input["command"])


def _read_file(session: dict, sandbox, input: dict) -> str:
    return sandbox.files.read(input["path"])


def _write_file(session: dict, sandbox, input: dict) -> str:
    sandbox.files.write(input["path"], input["content"])
    return f"wrote {len(input['content'])} bytes to {input['path']}"


def _expose_port(session: dict, sandbox, input: dict) -> str:
    return f"https://{sandbox.get_host(input['port'])}"


def _screenshot(session: dict, sandbox, input: dict) -> list[dict]:
    png = sandbox.screenshot()
    data = base64.b64encode(png).decode()
    return [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}]


def _click(session: dict, sandbox, input: dict) -> str:
    sandbox.left_click(input["x"], input["y"])
    return f"clicked ({input['x']}, {input['y']})"


def _move_mouse(session: dict, sandbox, input: dict) -> str:
    sandbox.move_mouse(input["x"], input["y"])
    return f"moved mouse to ({input['x']}, {input['y']})"


def _type_text(session: dict, sandbox, input: dict) -> str:
    sandbox.write(input["text"])
    return "typed"


def _press_key(session: dict, sandbox, input: dict) -> str:
    sandbox.press(input["key"])
    return f"pressed {input['key']}"


def _scroll(session: dict, sandbox, input: dict) -> str:
    sandbox.scroll(input["direction"], input.get("ticks", 3))
    return "scrolled"


def _git_commit(session: dict, sandbox, input: dict) -> str:
    return _run_bash(sandbox, f"git add -A && git commit -m {shlex.quote(input['message'])}")


def _git_diff(session: dict, sandbox, input: dict) -> str:
    return _run_bash(sandbox, "git diff --staged" if input.get("staged") else "git diff")


def _create_pull_request(session: dict, sandbox, input: dict) -> str:
    # Deferred import: app.github depends on nothing in agent/, but keeping this
    # import local avoids agent/tools.py (imported very early, by providers/base.py)
    # pulling in the github package - and Firebase/crypto init - at module load time.
    from app.github import client as github_client
    from app.github import credentials as github_credentials

    token = github_credentials.get_token(session["uid"])
    push_cmd = f"git push https://x-access-token:$GH_TOKEN@github.com/{input['repo']}.git {input['head_branch']}"
    push_output = _run_bash(sandbox, push_cmd, envs={"GH_TOKEN": token})
    pr_url = github_client.create_pull_request(
        token, input["repo"], input["title"], input["body"], input["head_branch"], input["base_branch"]
    )
    return f"{push_output}\nopened pull request: {pr_url}"


def _update_plan(session: dict, input: dict) -> str:
    session["plan"] = input["steps"]
    return f"plan updated ({len(input['steps'])} steps)"


def _remember(session: dict, input: dict) -> str:
    from app.agent.memory import append_note

    append_note(session["uid"], input["note"])
    return "noted."


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        "bash",
        "Run a shell command in the sandbox.",
        {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        _bash,
    ),
    ToolSpec(
        "read_file",
        "Read a text file from the sandbox.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        _read_file,
    ),
    ToolSpec(
        "write_file",
        "Write text to a file in the sandbox, creating it if needed.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        _write_file,
    ),
    ToolSpec(
        "expose_port",
        "Expose a port the sandbox is listening on and get back its public preview URL.",
        {"type": "object", "properties": {"port": {"type": "integer"}}, "required": ["port"]},
        _expose_port,
    ),
    ToolSpec(
        "screenshot",
        "Take a screenshot of the desktop.",
        {"type": "object", "properties": {}},
        _screenshot,
        supported=_needs("screenshot"),
    ),
    ToolSpec(
        "click",
        "Left-click at an (x, y) position on the desktop.",
        {
            "type": "object",
            "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
            "required": ["x", "y"],
        },
        _click,
        supported=_needs("left_click"),
    ),
    ToolSpec(
        "move_mouse",
        "Move the mouse to an (x, y) position on the desktop.",
        {
            "type": "object",
            "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
            "required": ["x", "y"],
        },
        _move_mouse,
        supported=_needs("move_mouse"),
    ),
    ToolSpec(
        "type_text",
        "Type text at the current focus on the desktop.",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        _type_text,
        supported=_needs("write"),
    ),
    ToolSpec(
        "press_key",
        "Press a single key on the desktop (e.g. 'Return', 'Escape').",
        {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
        _press_key,
        supported=_needs("press"),
    ),
    ToolSpec(
        "scroll",
        "Scroll the desktop up or down.",
        {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down"]},
                "ticks": {"type": "integer"},
            },
            "required": ["direction"],
        },
        _scroll,
        supported=_needs("scroll"),
    ),
    ToolSpec(
        "git_commit",
        "Stage and commit all changes in the sandbox's git repository.",
        {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
        _git_commit,
    ),
    ToolSpec(
        "git_diff",
        "Show the current git diff (staged or unstaged) in the sandbox.",
        {"type": "object", "properties": {"staged": {"type": "boolean"}}},
        _git_diff,
    ),
    ToolSpec(
        "create_pull_request",
        "Push the current branch to GitHub (assumes it already exists locally, e.g. via "
        "`git checkout -b`) and open a pull request. Requires the user to have linked their "
        "GitHub account.",
        {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/name"},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "head_branch": {"type": "string"},
                "base_branch": {"type": "string"},
            },
            "required": ["repo", "title", "head_branch", "base_branch"],
        },
        _create_pull_request,
    ),
]

VIRTUAL_TOOL_SPECS: list[VirtualToolSpec] = [
    VirtualToolSpec(
        "update_plan",
        "Set or update your visible step-by-step plan for this task.",
        {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step": {"type": "string"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "done"]},
                        },
                        "required": ["step", "status"],
                    },
                }
            },
            "required": ["steps"],
        },
        _update_plan,
    ),
    VirtualToolSpec(
        "remember",
        "Save a durable note about this user for future sessions (preferences, project context, etc).",
        {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]},
        _remember,
    ),
]


def supported_specs(sandbox) -> list[ToolSpec]:
    """Tool specs limited to what this live sandbox actually supports. Rendering
    these into a provider's wire format is that provider adapter's job."""
    return [t for t in TOOL_SPECS if t.supported(sandbox)]


def run_tool(session: dict, sandbox, name: str, input: dict) -> object:
    for t in TOOL_SPECS:
        if t.name == name:
            if not t.supported(sandbox):
                raise ValueError(f"tool {name!r} is not supported by this sandbox")
            return t.handler(session, sandbox, input)
    raise ValueError(f"unknown tool: {name}")


def dispatch_tool_call(session: dict, sandbox, name: str, input: dict) -> object:
    """The one call site for executing any tool call - checks virtual tools (which
    mutate session state directly and never touch the sandbox) first, then falls
    through to the sandbox-dispatched tools in TOOL_SPECS."""
    for t in VIRTUAL_TOOL_SPECS:
        if t.name == name:
            return t.handler(session, input)
    return run_tool(session, sandbox, name, input)
