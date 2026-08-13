"""Guards on what importing the CLI module costs.

`gie` runs as a fresh process for every command *and* for every shell-completion
TAB press, so module-scope imports are a latency budget, not a style question.
"""

from __future__ import annotations

import subprocess
import sys


def test_importing_cli_does_not_pull_in_sqlmodel():
    """sqlmodel costs ~230 ms of SQLAlchemy imports; only DB paths may pay it.

    Runs in a subprocess because the pytest process has already imported
    sqlmodel (conftest.py uses it), so sys.modules here proves nothing.
    """
    code = "import sys; import jailbee.cli; sys.exit(1 if 'sqlmodel' in sys.modules else 0)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "jailbee.cli imported sqlmodel at module scope; "
        "move the import inside the functions that need it"
    )
