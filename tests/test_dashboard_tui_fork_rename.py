"""Fork… and Rename… in the terminal dashboard: the inline prompts and the argv they run."""

from __future__ import annotations

import dataclasses

import pytest

from jailbee.dashboard import menus as dmenus
from jailbee.dashboard import model as dmodel
from jailbee.dashboard import overlays as doverlays
from jailbee.dashboard.tui import session as tsession
from tests.dashboard_fixtures import cfg_group, ci
from tests.dashboard_pilot import drive, keys, patch_pause

pytestmark = pytest.mark.usefixtures("no_real_branch_listing")


def _prompts(run):  # type: ignore[no-untyped-def]
    return run.of_type(tsession.TextPrompt)


def _target(tmp_path):  # type: ignore[no-untyped-def]
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    return group, dmodel.RepoTarget.of(group)


def test_fork_argv_puts_the_names_after_the_separator(tmp_path):
    _group, target = _target(tmp_path)
    assert dmenus.fork_container_argv(target, "alpha-x", "b") == [
        "jailbee",
        "fork",
        "--config",
        str(tmp_path / ".jailbee" / "config.yaml"),
        "--background",
        "--",
        "alpha-x",
        "b",
    ]


def test_rename_argv_clears_on_an_empty_alias():
    assert dmenus.rename_argv("alpha-x", "login") == ["rename", "alpha-x", "--", "login"]
    assert dmenus.rename_argv("alpha-x", "") == ["rename", "alpha-x", "--clear"]


def test_rename_prompt_accepts_an_empty_answer_but_fork_does_not():
    rename = doverlays.TextPrompt("container-rename", "t", "Alias", target="x")
    fork = doverlays.TextPrompt("container-fork", "t", "New container name", target="x")
    assert doverlays.validate_answer(rename, "  ") is None
    assert doverlays.validate_answer(fork, "  ") == "New container name cannot be empty"


def test_fork_prompt_runs_the_background_fork(mocker, tmp_path):
    group, target = _target(tmp_path)
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)

    run = drive(mocker, ["j", "enter", "f", *keys("b"), "enter"], [group])
    assert run.rc == 0

    prompts = _prompts(run)
    assert prompts[0].purpose == "container-fork"
    assert prompts[0].target == "alpha-x"
    child.assert_called_once()
    assert child.call_args.args[0] == dmenus.fork_container_argv(target, "alpha-x", "b")


def test_rename_prompt_starts_on_the_current_alias_and_runs_the_cli(mocker, tmp_path):
    info = dataclasses.replace(ci("alpha-x", "alpha"), alias="old")
    group = cfg_group(tmp_path, (info,))
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)

    run = drive(
        mocker,
        ["j", "enter", "R", *["backspace"] * 3, *keys("login"), "enter"],
        [group],
    )
    assert run.rc == 0

    prompts = _prompts(run)
    assert prompts[0].purpose == "container-rename"
    assert prompts[0].initial == "old"
    cfg = str(tmp_path / ".jailbee" / "config.yaml")
    assert child.call_args.args[0] == [
        "jailbee",
        "rename",
        "alpha-x",
        "--config",
        cfg,
        "--",
        "login",
    ]


def test_rename_prompt_with_an_empty_answer_clears_the_alias(mocker, tmp_path):
    info = dataclasses.replace(ci("alpha-x", "alpha"), alias="old")
    group = cfg_group(tmp_path, (info,))
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)

    run = drive(mocker, ["j", "enter", "R", *["backspace"] * 3, "enter"], [group])
    assert run.rc == 0

    cfg = str(tmp_path / ".jailbee" / "config.yaml")
    assert child.call_args.args[0] == ["jailbee", "rename", "alpha-x", "--clear", "--config", cfg]


def test_rename_is_offered_for_a_stopped_container_but_fork_is_not():
    ctx = dmenus.MenuContext(has_repo=True, state="Stopped", mode="clone")
    verbs = [verb for _label, verb in dmenus.menu_actions(ctx)]
    assert "rename" in verbs
    assert "fork" not in verbs


def _session(mocker, tmp_path, **kw):  # type: ignore[no-untyped-def]
    from tests.dashboard_pilot import make_app

    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    app = make_app(mocker, [group], **kw)
    return app.session, group


def test_fork_builder_over_ssh_addresses_the_repo_by_cwd(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteSSHConfig

    session, _group = _session(
        mocker,
        tmp_path,
        remote=True,
        over_ssh=True,
        ssh_policy=RemoteSSHConfig(restrict_host=False),
    )
    run_new = mocker.patch.object(session, "run_new_container")

    prompt = doverlays.TextPrompt("container-fork", "t", "New container name", target="alpha-x")
    session.submit_prompt(prompt, " b ")

    prefix, what, builder = run_new.call_args.args
    assert (prefix, what) == ("alpha", "fork b")
    repo = dmodel.RepoTarget(tmp_path, tmp_path / "c.yaml")
    assert builder(repo) == ["jailbee", "fork", "--background", "--", "alpha-x", "b"]


def test_fork_submit_for_a_vanished_container_notices_and_runs_nothing(mocker, tmp_path):
    session, _group = _session(mocker, tmp_path)
    run_new = mocker.patch.object(session, "run_new_container")
    session.groups = []

    prompt = doverlays.TextPrompt("container-fork", "t", "New container name", target="alpha-x")
    assert session.submit_prompt(prompt, "b") is None

    run_new.assert_not_called()
    assert "'alpha-x' is gone" in str(session.notice)


def test_open_rename_for_a_vanished_container_notices(mocker, tmp_path):
    session, group = _session(mocker, tmp_path)
    mocker.patch.object(session, "dispatchable", return_value=dmodel.RepoTarget.of(group))
    session.groups = [dataclasses.replace(group, containers=[])]

    assert session.open_rename("alpha-x") is None
    assert "'alpha-x' is gone" in str(session.notice)


_FORK_KEYS = ["j", "enter", "f", *keys("b"), "enter"]


def test_a_failed_background_fork_is_noticed_as_a_fork(mocker, tmp_path):
    group, _ = _target(tmp_path)
    mocker.patch.object(
        tsession.subprocess,
        "run",
        return_value=mocker.Mock(returncode=1, stderr="error: uncommitted changes in 'x'\n"),
    )
    patch_pause(mocker)

    run = drive(mocker, _FORK_KEYS, [group])

    notices = [str(n) for n in run.notices()]
    assert any("jailbee fork failed: error: uncommitted changes" in n for n in notices), notices
    assert not any("jailbee new" in n for n in notices)


def test_a_failed_attended_fork_is_noticed_as_a_fork(mocker, tmp_path):
    group, _ = _target(tmp_path)
    detached = mocker.Mock(returncode=2, stderr="error: ... no terminal to ask on. Re-run")
    attended = mocker.Mock(returncode=3, stderr=None)
    mocker.patch.object(tsession.subprocess, "run", side_effect=[detached, attended])
    patch_pause(mocker)

    run = drive(mocker, _FORK_KEYS, [group])

    notices = [str(n) for n in run.notices()]
    assert any("'jailbee fork' exited 3" in n for n in notices), notices


@pytest.mark.parametrize(("key", "purpose"), [("f", "container-fork"), ("R", "container-rename")])
def test_prompt_titles_name_the_short_container_name(mocker, tmp_path, key, purpose):
    group, _ = _target(tmp_path)
    mocker.patch.object(tsession.subprocess, "run").return_value.returncode = 0
    patch_pause(mocker)

    run = drive(mocker, ["j", "enter", key, "escape"], [group])

    prompt = next(p for p in _prompts(run) if p.purpose == purpose)
    assert "'x'" in prompt.title
    assert "alpha-x" not in prompt.title
