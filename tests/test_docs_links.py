"""Focused assertions for user-facing documentation contracts."""

from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"


def test_command_reference_names_the_ssh_enable_command() -> None:
    commands = (DOCS / "commands.md").read_text()

    assert "jb remote ssh enable" in commands


def test_security_reference_marks_full_remote_commands_as_high_trust() -> None:
    security = (DOCS / "security.md").read_text()

    assert "`commands.mode: full` is a high-trust setting" in security


def test_config_reference_names_every_host_level_deep_merge_bypass() -> None:
    config = (DOCS / "config.md").read_text()
    section = config.split("### Keys that bypass the deep-merge pipeline", 1)[1].split(
        "### Inspecting the layers", 1
    )[0]

    assert "Nine top-level keys" in section
    for key in (
        "docker_registry_mirror",
        "ls",
        "dashboard",
        "credentials",
        "scratch",
        "config_edit",
        "update_check",
        "install_host_skills",
        "remote",
    ):
        assert f"`{key}`" in section


def test_ssh_smoke_test_distinguishes_forwarding_from_inherited_environment() -> None:
    manual = (DOCS / "manual-testing.md").read_text()
    section = manual.split("### Rejected SSH features", 1)[1].split(
        "## `jailbee git fetch / checkout` smoke test", 1
    )[0]
    normalized = " ".join(section.split())

    assert "inherited service environment" in normalized
    assert "SSH-forwarded environment" in normalized
    assert "must receive neither `SSH_AUTH_SOCK` nor an X11 display" not in normalized
