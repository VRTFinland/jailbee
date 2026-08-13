"""The package must be runnable as `python -m jailbee` (worker re-exec)."""

from __future__ import annotations

import subprocess
import sys


def test_python_m_invokes_cli_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "jailbee", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Usage" in result.stdout
