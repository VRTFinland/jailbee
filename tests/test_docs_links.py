"""Focused assertions for user-facing documentation contracts."""

from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"


def test_command_reference_names_the_ssh_enable_command() -> None:
    commands = (DOCS / "commands.md").read_text()

    assert "jb remote ssh enable" in commands


def test_security_reference_marks_full_remote_commands_as_high_trust() -> None:
    security = (DOCS / "security.md").read_text()

    assert "`commands.mode: full` is a high-trust setting" in security
