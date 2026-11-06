import subprocess
import sys


def test_container_command_tree_does_not_load_config():
    code = (
        "import jailbee.container_cli; import sys;"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'pydantic','yaml'} or m == 'jailbee.config'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_inbox_behaviour_modules_import_stdlib_only():
    code = (
        "import jailbee.inbox.cli, jailbee.inbox.send, jailbee.inbox.waiting, "
        "jailbee.inbox.render, jailbee.inbox.watch, jailbee.inbox.errors, "
        "jailbee.inbox.transport, jailbee.inbox.socket_cli; import sys;"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'typer','pydantic','rich','yaml','click'}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
