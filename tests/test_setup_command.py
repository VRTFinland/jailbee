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
# host agent skills
# --------------------------------------------------------------------------


def _opt_in(host_skills: bool, home: Path) -> Path:
    """Write the global.yaml that turns the host-skills install on (or off)."""
    path = home / ".config" / "jailbee" / "global.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"install_host_skills: {str(host_skills).lower()}\n")
    return path


def _which(*binaries: str) -> object:
    """A `shutil.which` stand-in that finds exactly `binaries`."""
    return lambda binary: f"/usr/bin/{binary}" if binary in binaries else None


def test_skills_status_reports_opt_in_when_flag_off(home: Path) -> None:
    from jailbee.setup_command import skills_status

    status = skills_status()

    assert status.installed is True
    assert "opt-in" in status.detail
    assert str(home / ".config" / "jailbee" / "global.yaml") in status.detail


def test_skills_status_flips_after_install(home: Path, mocker: MockerFixture) -> None:
    _opt_in(True, home)
    mocker.patch("shutil.which", _which("claude"))
    from jailbee.agent_skills import bundled_skill_names, host_skill_targets, install_host_skills
    from jailbee.setup_command import skills_status

    assert skills_status().installed is False
    install_host_skills(host_skill_targets())
    status = skills_status()
    assert status.installed is True
    assert str(home / ".claude" / "skills") in status.detail
    assert len(bundled_skill_names()) == 3  # detail says "3 skills", keep it honest


def test_skills_status_no_agents_found_when_flag_on(home: Path, mocker: MockerFixture) -> None:
    """A host with none of the agents installed owes nothing."""
    _opt_in(True, home)
    mocker.patch("shutil.which", return_value=None)
    from jailbee.setup_command import skills_status

    status = skills_status()

    assert status.installed is True
    assert "no skill-capable agents" in status.detail


def test_skills_status_checks_every_detected_agent(home: Path, mocker: MockerFixture) -> None:
    """Skills installed for claude but not for the codex the host also has."""
    _opt_in(True, home)
    mocker.patch("shutil.which", _which("claude", "codex"))
    from jailbee.agent_skills import host_skill_targets, install_host_skills
    from jailbee.setup_command import skills_status

    install_host_skills(host_skill_targets()[:1])  # claude only

    status = skills_status()
    assert status.installed is False
    assert str(home / ".codex" / "skills") in status.detail


def _global_config(content: str, home: Path) -> Path:
    """Write a raw global.yaml body into this test's home."""
    path = home / ".config" / "jailbee" / "global.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_skills_status_tolerates_a_schema_invalid_global_yaml(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A schema-invalid global.yaml must not crash the probe: `jailbee setup`
    is the repair command, so the skills step reads as opted out and warns."""
    path = _global_config("install_host_skills: maybe\n", home)
    from jailbee.setup_command import skills_status

    status = skills_status()

    assert status.installed is True
    assert "opt-in: off" in status.detail
    # Collapse the lines Rich may fold a long tmp path across.
    assert str(path) in capsys.readouterr().out.replace("\n", "")


def test_skills_status_tolerates_an_unrelated_schema_error(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The "broken host needs the repair command" case: a pre-existing
    docker_registry_mirror error is not about skills, and must not be fatal."""
    path = _global_config("docker_registry_mirror:\n  port: banana\n", home)
    from jailbee.setup_command import skills_status

    status = skills_status()

    assert status.installed is True
    assert "opt-in: off" in status.detail
    assert str(path) in capsys.readouterr().out.replace("\n", "")


def test_run_setup_survives_a_schema_invalid_global_yaml(
    home: Path, capsys: pytest.CaptureFixture[str], mocker: MockerFixture
) -> None:
    """`jailbee setup --only skills` on a broken host: no exception, nothing
    ran, nothing is installed under the opted-out home — and the warning about
    the unreadable file is printed once, not once per read."""
    path = _global_config("install_host_skills: maybe\n", home)
    mocker.patch("shutil.which", _which("claude"))
    from jailbee.setup_command import run_setup

    ran = run_setup(keys=["skills"], shells=["bash"], confirm=None)

    assert ran == []
    assert not (home / ".claude" / "skills").exists()
    out = capsys.readouterr().out.replace("\n", "")
    assert "opt-in" in out
    # One read, so one warning: `run_setup` used to resolve the opt-in once
    # for the status and again for the install, printing the whole pydantic
    # error twice.
    assert out.count(f"ignoring {path}") == 1


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
    mocker.patch("shutil.which", _which("claude"))
    _opt_in(True, home)

    ran = run_setup(shells=["bash"], confirm=None)

    assert ran == list(STEP_KEYS)
    units.assert_called_once_with()
    assert (home / ".local" / "share" / "bash-completion" / "completions" / "jb").is_file()
    assert (home / ".claude" / "skills" / "jailbee-usage").is_dir()


def test_run_setup_honours_the_keys_it_is_given(home: Path, mocker: MockerFixture) -> None:
    from jailbee.setup_command import run_setup

    _opt_in(True, home)
    mocker.patch("shutil.which", _which("claude"))
    units = mocker.patch("jailbee.init_command.install_systemd_units")

    ran = run_setup(keys=["skills"], shells=["bash"], confirm=None)

    assert ran == ["skills"]
    units.assert_not_called()
    assert not (home / ".local" / "share" / "bash-completion").exists()


def test_run_setup_does_not_install_skills_when_opted_out(
    home: Path, capsys: pytest.CaptureFixture[str], mocker: MockerFixture
) -> None:
    """Opted out there is nothing to install and nothing to refresh, so even
    `--yes` writes nothing, claims nothing ran, and says why."""
    mocker.patch("shutil.which", _which("claude"))
    _opt_in(False, home)
    from jailbee.setup_command import run_setup

    ran = run_setup(keys=["skills"], shells=["bash"], confirm=None)

    assert ran == []
    assert not (home / ".claude" / "skills").exists()
    out = capsys.readouterr().out
    assert "opt-in" in out
    # Not "installed (opt-in: off)", which reads as a claim about files.
    assert "installed" not in out


def test_run_setup_never_asks_about_an_opted_out_skills_step(
    home: Path, mocker: MockerFixture
) -> None:
    """Interactive: "Refresh agent skills (host)?" would offer a no-op —
    a yes could only reach an installer with nothing to write."""
    mocker.patch("shutil.which", _which("claude"))
    _opt_in(False, home)
    from jailbee.setup_command import run_setup

    asked: list[str] = []

    def confirm(question: str, default: bool) -> bool:
        asked.append(question)
        return True

    ran = run_setup(keys=["skills"], shells=["bash"], confirm=confirm)

    assert asked == []
    assert ran == []
    assert not (home / ".claude" / "skills").exists()


def test_skills_status_names_skills_left_behind_by_the_opt_in(
    home: Path, mocker: MockerFixture
) -> None:
    """A host that ran `jailbee setup` before the opt-in existed keeps the
    files it installed then, and nothing refreshes them any more. The step is
    still ok — it owes nothing — but the detail has to say so, because this is
    the only surface that population ever sees."""
    mocker.patch("shutil.which", _which("claude"))
    from jailbee.agent_skills import host_skill_targets, install_host_skills
    from jailbee.setup_command import skills_status

    install_host_skills(host_skill_targets())  # the pre-upgrade state
    _opt_in(False, home)

    status = skills_status()

    assert status.installed is True
    assert status.actionable is False
    assert "no longer refreshed" in status.detail
    assert str(home / ".claude" / "skills") in status.detail


def test_skills_status_opt_out_detail_stays_short_on_a_clean_host(home: Path) -> None:
    """Nothing was ever installed, so there is nothing to warn about."""
    _opt_in(False, home)
    from jailbee.setup_command import skills_status

    assert "no longer refreshed" not in skills_status().detail


def test_run_setup_skips_a_step_the_callback_declines(home: Path, mocker: MockerFixture) -> None:
    """Opted in, so skills is genuinely missing — every step is offered with
    a "yes" default, and the declined ones are not run."""
    from jailbee.setup_command import run_setup

    mocker.patch("shutil.which", _which("claude"))
    _opt_in(True, home)
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
    from jailbee.setup_command import consume_hint, install_completions

    mocker.patch("shutil.which", return_value="/usr/local/bin/jailbee")
    mocker.patch("subprocess.run")
    from jailbee.init_command import install_systemd_units

    # Completions and timer in place; skills opted out, which also counts
    # as nothing-missing for the hint.
    install_completions(["bash"])
    install_systemd_units()
    _opt_in(False, home)

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


# --------------------------------------------------------------------------
# the linger tip
# --------------------------------------------------------------------------


def test_run_setup_skips_completions_when_no_shell_is_known(
    home: Path, capsys, mocker: MockerFixture
) -> None:
    """Nothing was installed, so nothing may be reported as installed."""
    mocker.patch("jailbee.init_command.install_systemd_units")

    from jailbee.setup_command import run_setup

    ran = run_setup(keys=["completions"], shells=[], confirm=None)

    assert ran == []
    assert not (home / ".local" / "share" / "bash-completion").exists()
    assert "--shell" in capsys.readouterr().out


def test_linger_tip_names_the_command_when_linger_is_off(
    home: Path, capsys, mocker: MockerFixture
) -> None:
    _ = home
    from subprocess import CompletedProcess

    from jailbee.setup_command import linger_tip

    mocker.patch(
        "subprocess.run",
        return_value=CompletedProcess(args=[], returncode=0, stdout="Linger=no\n", stderr=""),
    )

    linger_tip()

    assert "enable-linger" in capsys.readouterr().out


def test_linger_tip_is_silent_when_linger_is_on(home: Path, capsys, mocker: MockerFixture) -> None:
    _ = home
    from subprocess import CompletedProcess

    from jailbee.setup_command import linger_tip

    mocker.patch(
        "subprocess.run",
        return_value=CompletedProcess(args=[], returncode=0, stdout="Linger=yes\n", stderr=""),
    )

    linger_tip()

    assert capsys.readouterr().out == ""


def test_linger_tip_survives_a_missing_loginctl(home: Path, capsys, mocker: MockerFixture) -> None:
    """A non-systemd host has no `loginctl`; the tip is advice, not a step."""
    _ = home
    from jailbee.setup_command import linger_tip

    mocker.patch("subprocess.run", side_effect=FileNotFoundError("loginctl"))

    linger_tip()

    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------
# the pending split behind the hint, and the interactive offer
# --------------------------------------------------------------------------


def test_hint_pending_returns_the_missing_steps_once(home: Path, mocker: MockerFixture) -> None:
    """The gate both the printed hint and the offer sit behind."""
    mocker.patch("shutil.which", _which("claude"))
    _opt_in(True, home)
    from jailbee.setup_command import hint_pending

    with _session() as session:
        first = hint_pending(session, shells=["bash"], now=_NOW)
        assert [s.key for s in first] == ["completions", "timer", "skills"]
        assert hint_pending(session, shells=["bash"], now=_NOW) == []


def test_hint_pending_is_silent_after_setup_ran(home: Path) -> None:
    _ = home
    from jailbee.setup_command import hint_pending, record_setup

    with _session() as session:
        record_setup(session, "1.3.1", now=_NOW)
        assert hint_pending(session, shells=["bash"], now=_NOW) == []


def test_offer_lines_leave_the_call_to_action_to_the_prompt(home: Path) -> None:
    """The question that follows is the call to action, so the block must not
    also tell the user to run `jb setup` — nor claim to be one-shot."""
    _ = home
    from jailbee.setup_command import DOCS_URL, offer_lines, pending_steps

    text = "\n".join(offer_lines(pending_steps(["bash"])))

    assert "Post-install steps that have not been done on this machine:" in text
    assert DOCS_URL in text
    assert "Run `jb setup`" not in text
    assert "(shown once)" not in text


def test_pending_steps_shrinks_as_steps_are_installed(home: Path, mocker: MockerFixture) -> None:
    _ = home
    from jailbee.setup_command import install_completions, pending_steps

    mocker.patch("jailbee.setup_command.timer_status", return_value=_installed("timer"))
    mocker.patch("jailbee.setup_command.skills_status", return_value=_installed("skills"))
    assert [s.key for s in pending_steps(["bash"])] == ["completions"]

    install_completions(["bash"])
    assert pending_steps(["bash"]) == []


def _installed(key: str):
    from jailbee.setup_command import STEP_TITLES, StepStatus

    return StepStatus(key=key, title=STEP_TITLES[key], installed=True, detail="ok")


# --------------------------------------------------------------------------
# the read-only listing
# --------------------------------------------------------------------------


def test_report_status_names_every_step_and_installs_nothing(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = home
    from jailbee.setup_command import STEP_KEYS, STEP_TITLES, report_status, timer_status

    report_status(STEP_KEYS, ["bash"])

    out = capsys.readouterr().out
    for title in STEP_TITLES.values():
        assert title in out
    assert not timer_status().installed, "--status must not install anything"


def test_report_status_honours_the_keys_it_is_given(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = home
    from jailbee.setup_command import STEP_TITLES, report_status

    report_status(["skills"], ["bash"])

    out = capsys.readouterr().out
    assert STEP_TITLES["skills"] in out
    assert STEP_TITLES["timer"] not in out


# --------------------------------------------------------------------------
# the optional Qt extra — reported, never installed
# --------------------------------------------------------------------------


def test_qt_dashboard_status_names_the_install_command_when_missing(
    home: Path, mocker: MockerFixture
) -> None:
    _ = home
    from jailbee.setup_command import qt_dashboard_status

    mocker.patch("jailbee.setup_command.find_spec", return_value=None)

    installed, detail = qt_dashboard_status()

    assert installed is False
    assert "jailbee[gui]" in detail


def test_qt_dashboard_status_is_content_when_pyside_is_importable(
    home: Path, mocker: MockerFixture
) -> None:
    _ = home
    from jailbee.setup_command import qt_dashboard_status

    mocker.patch("jailbee.setup_command.find_spec", return_value=object())

    installed, detail = qt_dashboard_status()

    assert installed is True
    assert "PySide6" in detail


def test_qt_dashboard_status_survives_a_broken_import_system(
    home: Path, mocker: MockerFixture
) -> None:
    """`find_spec` raises on a package whose parent cannot be imported."""
    _ = home
    from jailbee.setup_command import qt_dashboard_status

    mocker.patch("jailbee.setup_command.find_spec", side_effect=ValueError("__spec__ is None"))

    assert qt_dashboard_status()[0] is False


def test_report_status_mentions_the_optional_qt_extra(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = home
    from jailbee.setup_command import STEP_KEYS, report_status

    report_status(STEP_KEYS, ["bash"])

    assert "Qt dashboard (optional)" in capsys.readouterr().out


def test_report_status_leaves_the_extra_out_of_a_filtered_listing(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--only timer` asked about one step; an unrelated extra is noise there."""
    _ = home
    from jailbee.setup_command import report_status

    report_status(["timer"], ["bash"])

    assert "Qt dashboard (optional)" not in capsys.readouterr().out
