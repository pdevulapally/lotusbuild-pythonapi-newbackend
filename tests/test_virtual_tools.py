from tool_unit_adapter import handler_receipt_adapter
import app.agent.memory as memory_module
from app.agent.tools import dispatch_tool_call


class FakeCommands:
    def run(self, command, envs=None):
        class R:
            stdout, stderr, exit_code = f"ran: {command}\n", "", 0

        return R()


class FakeSandbox:
    def __init__(self):
        self.commands = FakeCommands()


def test_update_plan_mutates_session_without_touching_sandbox():
    session = {"plan": None}
    result = dispatch_tool_call(session, None, "update_plan", {"steps": [{"step": "do X", "status": "pending"}]})
    assert session["plan"] == [{"step": "do X", "status": "pending"}]
    assert "updated" in result


def test_remember_calls_append_note():
    calls = []
    memory_module.append_note = lambda uid, note: calls.append((uid, note))
    session = {"uid": "u1"}
    dispatch_tool_call(session, None, "remember", {"note": "likes dark mode"})
    assert calls == [("u1", "likes dark mode")]


def test_falls_through_to_sandbox_tool_for_non_virtual_names():
    session = {}
    sandbox = FakeSandbox()
    result = dispatch_tool_call(session, sandbox, "bash", {"command": "echo hi"})
    assert "echo hi" in result


if __name__ == "__main__":
    test_update_plan_mutates_session_without_touching_sandbox()
    test_remember_calls_append_note()
    test_falls_through_to_sandbox_tool_for_non_virtual_names()
    print("ok")
