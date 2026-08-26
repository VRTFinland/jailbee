"""Tests for `jailbee setup` — the post-install user-level steps.

Every test takes the module-local ``home`` fixture: these functions write
shell-completion scripts, systemd units and skill directories into the
user's home for real, so each needs a home (and XDG dirs) of its own
rather than the session-wide one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

_NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def home(private_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`private_home` with XDG_DATA_HOME / XDG_CONFIG_HOME inside it.

    The session-wide `_isolate_global_config` fixture points
    XDG_CONFIG_HOME at a *shared* tmp dir, which would put fish
    completions outside this test's home and leak them between tests.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(private_home / ".local" / "share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(private_home / ".config"))
    return private_home


# --------------------------------------------------------------------------
# completion paths and installation
# --------------------------------------------------------------------------


def test_completion_paths_follow_each_shell_convention(home: Path) -> None:
    """bash and fish autoload by filename; zsh needs `_<name>` on the fpath."""
    from jailbee.setup_command import completion_path

    assert completion_path("bash", "jb") == (
        home / ".local" / "share" / "bash-completion" / "completions" / "jb"
    )
    assert completion_path("zsh", "jb") == home / ".zfunc" / "_jb"
    assert completion_path("fish", "jb") == home / ".config" / "fish" / "completions" / "jb.fish"


def test_completion_path_rejects_an_unsupported_shell(home: Path) -> None:
    _ = home
    from jailbee.setup_command import completion_path

    with pytest.raises(ValueError, match="csh"):
        completion_path("csh", "jb")


def test_install_completions_writes_a_script_per_prog_name(home: Path) -> None:
    """Both console scripts get one — `jb` has its own `_JB_COMPLETE` var."""
    from jailbee.setup_command import install_completions

    written = install_completions(["bash"])

    comp_dir = home / ".local" / "share" / "bash-completion" / "completions"
    assert set(written) == {comp_dir / "jailbee", comp_dir / "jb"}
    assert "_JAILBEE_COMPLETE=complete_bash" in (comp_dir / "jailbee").read_text()
    assert "_JB_COMPLETE=complete_bash" in (comp_dir / "jb").read_text()
    assert "complete -o default -F _jb_completion jb" in (comp_dir / "jb").read_text()


def test_install_completions_covers_every_requested_shell(home: Path) -> None:
    from jailbee.setup_command import install_completions

    install_completions(["bash", "zsh", "fish"])

    assert (home / ".zfunc" / "_jailbee").read_text().startswith("#compdef jailbee")
    fish = home / ".config" / "fish" / "completions" / "jb.fish"
    assert "complete --command jb" in fish.read_text()


def test_completions_status_flips_once_every_script_exists(home: Path) -> None:
    from jailbee.setup_command import completions_status, install_completions

    assert completions_status(["bash"]).installed is False
    install_completions(["bash"])
    assert completions_status(["bash"]).installed is True
    # A second shell that was never installed is still missing.
    assert completions_status(["bash", "fish"]).installed is False
    _ = home


def test_completions_status_with_no_shell_is_not_installed(home: Path) -> None:
    """An undetected shell must not read as "already done"."""
    _ = home
    from jailbee.setup_command import completions_status

    status = completions_status([])
    assert status.installed is False
    assert "shell" in status.detail


# --------------------------------------------------------------------------
# shell detection
# --------------------------------------------------------------------------


def test_detect_shell_prefers_the_detected_interactive_shell(
    home: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> None:
    _ = home
    from jailbee import setup_command

    mocker.patch.object(setup_command, "_shellingham_name", return_value="fish")
    monkeypatch.setenv("SHELL", "/bin/bash")

    assert setup_command.detect_shell() == "fish"


def test_detect_shell_falls_back_to_shell_env_when_detection_is_useless(
    home: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> None:
    """Under `make`, shellingham sees `sh` — `$SHELL` still holds the login shell.

    This is why `make install` needed `SHELL := /bin/bash` to run
    `--show-completion`; the fallback is what retires that workaround.
    """
    _ = home
    from jailbee import setup_command

    mocker.patch.object(setup_command, "_shellingham_name", return_value="sh")
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")

    assert setup_command.detect_shell() == "zsh"


def test_detect_shell_returns_none_when_nothing_is_supported(
    home: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> None:
    _ = home
    from jailbee import setup_command

    mocker.patch.object(setup_command, "_shellingham_name", return_value=None)
    monkeypatch.setenv("SHELL", "/bin/csh")

    assert setup_command.detect_shell() is None


# --------------------------------------------------------------------------
# host Claude skills
# --------------------------------------------------------------------------


def test_install_host_skills_copies_every_bundled_skill(home: Path) -> None:
    from jailbee.claude_skills import bundled_skill_names, install_host_skills

    written = install_host_skills()

    names = bundled_skill_names()
    assert "jailbee-usage" in names
    assert {p.name for p in written} == set(names)
    assert (home / ".claude" / "skills" / "jailbee-usage" / "SKILL.md").is_file()


def test_install_host_skills_replaces_a_stale_copy(home: Path) -> None:
    """Files removed upstream must disappear, as `make install-skill` did."""
    from jailbee.claude_skills import install_host_skills

    stale = home / ".claude" / "skills" / "jailbee-usage" / "GONE.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("removed upstream")

    install_host_skills()

    assert not stale.exists()
    assert (stale.parent / "SKILL.md").is_file()


def test_install_host_skills_leaves_unrelated_skills_alone(home: Path) -> None:
    from jailbee.claude_skills import install_host_skills

    mine = home / ".claude" / "skills" / "my-own-skill" / "SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("mine")

    install_host_skills()

    assert mine.read_text() == "mine"


def test_skills_status_flips_after_install(home: Path) -> None:
    _ = home
    from jailbee.claude_skills import install_host_skills
    from jailbee.setup_command import skills_status

    assert skills_status().installed is False
    install_host_skills()
    assert skills_status().installed is True


# --------------------------------------------------------------------------
# refresh timer
# --------------------------------------------------------------------------


def test_timer_status_follows_the_unit_file(home: Path, mocker: MockerFixture) -> None:
    """Probed by a `stat`, not `systemctl` — the hint runs on every `jb ls`."""
    from jailbee.init_command import install_systemd_units
    from jailbee.setup_command import timer_status

    assert timer_status().installed is False

    mocker.patch("shutil.which", return_value="/usr/local/bin/jailbee")
    mocker.patch("subprocess.run")
    install_systemd_units()

    assert timer_status().installed is True
    assert str(home) in timer_status().detail


# --------------------------------------------------------------------------
# run_setup
# --------------------------------------------------------------------------


def test_run_setup_installs_every_step_when_not_interactive(
    home: Path, mocker: MockerFixture
) -> None:
    from jailbee.setup_command import STEP_KEYS, run_setup

    units = mocker.patch("jailbee.init_command.install_systemd_units")

    ran = run_setup(shells=["bash"], confirm=None)

    assert ran == list(STEP_KEYS)
    units.assert_called_once_with()
    assert (home / ".local" / "share" / "bash-completion" / "completions" / "jb").is_file()
    assert (home / ".claude" / "skills" / "jailbee-usage").is_dir()


def test_run_setup_honours_the_keys_it_is_given(home: Path, mocker: MockerFixture) -> None:
    from jailbee.setup_command import run_setup

    units = mocker.patch("jailbee.init_command.install_systemd_units")

    ran = run_setup(keys=["skills"], shells=["bash"], confirm=None)

    assert ran == ["skills"]
    units.assert_not_called()
    assert not (home / ".local" / "share" / "bash-completion").exists()


def test_run_setup_skips_a_step_the_callback_declines(home: Path, mocker: MockerFixture) -> None:
    from jailbee.setup_command import run_setup

    units = mocker.patch("jailbee.init_command.install_systemd_units")
    asked: list[tuple[str, bool]] = []

    def confirm(question: str, default: bool) -> bool:
        asked.append((question, default))
        return "completions" in question

    ran = run_setup(shells=["bash"], confirm=confirm)

    assert ran == ["completions"]
    units.assert_not_called()
    assert not (home / ".claude" / "skills").exists()
    # A missing step is offered with a "yes" default.
    assert all(default is True for _, default in asked)


def test_run_setup_offers_an_installed_step_with_a_no_default(
    home: Path, mocker: MockerFixture
) -> None:
    """Re-running must not silently rewrite what is already in place."""
    _ = home
    from jailbee.setup_command import install_completions, run_setup

    mocker.patch("jailbee.init_command.install_systemd_units")
    install_completions(["bash"])
    defaults: dict[str, bool] = {}

    def confirm(question: str, default: bool) -> bool:
        if "completions" in question:
            defaults["completions"] = default
        return False

    run_setup(keys=["completions"], shells=["bash"], confirm=confirm)

    assert defaults["completions"] is False


# --------------------------------------------------------------------------
# the zsh rc line
# --------------------------------------------------------------------------


def test_run_setup_appends_the_zshrc_line_when_confirmed(home: Path) -> None:
    from jailbee.setup_command import ZSHRC_LINE, run_setup

    (home / ".zshrc").write_text("# mine\n")

    run_setup(keys=["completions"], shells=["zsh"], confirm=lambda q, d: True)

    content = (home / ".zshrc").read_text()
    assert content.startswith("# mine\n")
    assert ZSHRC_LINE in content


def test_run_setup_never_touches_zshrc_without_a_callback(home: Path, capsys) -> None:
    """`--yes` (non-interactive) prints the line instead of editing the rc."""
    from jailbee.setup_command import ZSHRC_LINE, run_setup

    (home / ".zshrc").write_text("# mine\n")

    run_setup(keys=["completions"], shells=["zsh"], confirm=None)

    assert (home / ".zshrc").read_text() == "# mine\n"
    assert ZSHRC_LINE in capsys.readouterr().out


def test_run_setup_does_not_duplicate_an_existing_zshrc_line(home: Path) -> None:
    from jailbee.setup_command import ZSHRC_LINE, run_setup

    (home / ".zshrc").write_text(f"{ZSHRC_LINE}\n")

    run_setup(keys=["completions"], shells=["zsh"], confirm=lambda q, d: True)

    assert (home / ".zshrc").read_text().count(ZSHRC_LINE) == 1


def test_run_setup_asks_nothing_about_zshrc_for_other_shells(home: Path) -> None:
    from jailbee.setup_command import run_setup

    asked: list[str] = []

    def confirm(question: str, default: bool) -> bool:
        asked.append(question)
        return True

    run_setup(keys=["completions"], shells=["bash"], confirm=confirm)

    assert not any("zshrc" in q.lower() for q in asked)
    assert not (home / ".zshrc").exists()


# --------------------------------------------------------------------------
# the one-shot hint
# --------------------------------------------------------------------------


def _session():
    from sqlmodel import Session, SQLModel, create_engine

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_consume_hint_names_the_missing_steps(home: Path) -> None:
    _ = home
    from jailbee.setup_command import consume_hint

    with _session() as session:
        lines = consume_hint(session, shells=["bash"], now=_NOW)

    assert any("jb setup" in line for line in lines)
    assert any("completions" in line for line in lines)


def test_consume_hint_fires_only_once(home: Path) -> None:
    """A long-time user must see this at most once, never on every command."""
    _ = home
    from jailbee.setup_command import consume_hint

    with _session() as session:
        assert consume_hint(session, shells=["bash"], now=_NOW) != []
        assert consume_hint(session, shells=["bash"], now=_NOW) == []


def test_consume_hint_is_silent_when_nothing_is_missing(home: Path, mocker: MockerFixture) -> None:
    from jailbee.claude_skills import install_host_skills
    from jailbee.setup_command import consume_hint, install_completions

    mocker.patch("shutil.which", return_value="/usr/local/bin/jailbee")
    mocker.patch("subprocess.run")
    from jailbee.init_command import install_systemd_units

    install_completions(["bash"])
    install_host_skills()
    install_systemd_units()
    _ = home

    with _session() as session:
        assert consume_hint(session, shells=["bash"], now=_NOW) == []


def test_consume_hint_is_silent_after_setup_ran(home: Path) -> None:
    """`jb setup` run, a step declined: the user has decided. No nagging."""
    _ = home
    from jailbee.setup_command import consume_hint, record_setup

    with _session() as session:
        record_setup(session, "1.2.0", now=_NOW)
        assert consume_hint(session, shells=["bash"], now=_NOW) == []


def test_record_setup_stores_the_version_it_ran_at(home: Path) -> None:
    _ = home
    from jailbee.db.models import HostSetupState
    from jailbee.setup_command import record_setup

    with _session() as session:
        record_setup(session, "1.2.0", now=_NOW)
        row = session.get(HostSetupState, 1)

    assert row is not None
    assert row.setup_version == "1.2.0"
    assert row.setup_at == _NOW
