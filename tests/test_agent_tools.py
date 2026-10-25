from tool_unit_adapter import handler_receipt_adapter
from app.agent.tools import run_tool, supported_specs
from app.sandbox.manager import manager


class _Result:
    def __init__(self, stdout="", stderr="", exit_code=0):
        self.stdout, self.stderr, self.exit_code = stdout, stderr, exit_code


class FakeCommands:
    def run(self, command):
        return _Result(stdout=f"ran: {command}\n")


class FakeFiles:
    def __init__(self):
        self._data = {}

    def read(self, path):
        return self._data[path]

    def write(self, path, content):
        self._data[path] = content


class FakeCodeSandbox:
    """Duck-types the e2b.Sandbox surface our tools touch - no network needed."""

    def __init__(self):
        self.commands = FakeCommands()
        self.files = FakeFiles()

    def get_host(self, port):
        return f"{port}-fake.e2b.app"


class FakeDesktopSandbox(FakeCodeSandbox):
    """Adds the e2b_desktop.Sandbox-only surface (screenshot, mouse, keyboard)."""

    def screenshot(self):
        return b"\x89PNG\r\n"

    def left_click(self, x, y):
        self.last_click = (x, y)

    def move_mouse(self, x, y):
        pass

    def write(self, text):
        pass

    def press(self, key):
        pass

    def scroll(self, direction, ticks=3):
        pass


def test_code_sandbox_exposes_only_core_tools():
    names = {t.name for t in supported_specs(FakeCodeSandbox())}
    assert names == {
        "bash", "read_file", "write_file", "expose_port", "git_commit", "git_diff",
    }


def test_desktop_sandbox_also_exposes_desktop_tools():
    names = {t.name for t in supported_specs(FakeDesktopSandbox())}
    assert {"screenshot", "click", "move_mouse", "type_text", "press_key", "scroll"} <= names


def test_run_tool_file_roundtrip():
    sandbox = FakeCodeSandbox()
    run_tool({}, sandbox, "write_file", {"path": "a.txt", "content": "hi"})
    assert run_tool({}, sandbox, "read_file", {"path": "a.txt"}) == "hi"


def test_run_tool_rejects_desktop_only_tool_on_code_sandbox():
    try:
        run_tool({}, FakeCodeSandbox(), "screenshot", {})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_run_tool_rejects_unknown_tool():
    try:
        run_tool({}, FakeCodeSandbox(), "not_a_tool", {})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_manager_rejects_unknown_sandbox_kind():
    try:
        manager.create("fake-session", "not-a-real-kind")
        assert False, "expected ValueError"
    except ValueError:
        pass


if __name__ == "__main__":
    test_code_sandbox_exposes_only_core_tools()
    test_desktop_sandbox_also_exposes_desktop_tools()
    test_run_tool_file_roundtrip()
    test_run_tool_rejects_desktop_only_tool_on_code_sandbox()
    test_run_tool_rejects_unknown_tool()
    test_manager_rejects_unknown_sandbox_kind()
    print("ok")


def test_github_write_fails_closed_without_accessing_credentials():
    import pytest
    with pytest.raises(PermissionError, match="disabled"):
        run_tool({}, None, "create_pull_request", {})
