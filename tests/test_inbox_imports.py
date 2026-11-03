import subprocess
import sys


def test_inbox_imports_stdlib_only():
    code = (
        "import jailbee.inbox.cli, jailbee.inbox.send; import sys; "
        "print(sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'typer','pydantic','rich','yaml','click'}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
