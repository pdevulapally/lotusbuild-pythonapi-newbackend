from app.agent.risk import is_risky


def test_safe_bash_command_is_not_risky():
    assert is_risky("bash", {"command": "ls -la"}) is False


def test_destructive_bash_command_is_risky():
    assert is_risky("bash", {"command": "rm -rf /workspace/build"}) is True


def test_sudo_is_risky():
    assert is_risky("bash", {"command": "sudo apt-get install -y curl"}) is True


def test_expose_port_is_always_risky():
    assert is_risky("expose_port", {"port": 3000}) is True


def test_create_pull_request_is_always_risky():
    assert is_risky("create_pull_request", {"repo": "a/b"}) is True


def test_tool_with_no_policy_is_never_risky():
    assert is_risky("read_file", {"path": "a.txt"}) is False
    assert is_risky("update_plan", {"steps": []}) is False


if __name__ == "__main__":
    test_safe_bash_command_is_not_risky()
    test_destructive_bash_command_is_risky()
    test_sudo_is_risky()
    test_expose_port_is_always_risky()
    test_create_pull_request_is_always_risky()
    test_tool_with_no_policy_is_never_risky()
    print("ok")
