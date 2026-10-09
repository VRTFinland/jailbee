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
    assert dmenus.rename_argv("alpha-x", "login") == ["rename", "alpha-x", "login"]
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
    assert child.call_args.args[0] == ["jailbee", "rename", "alpha-x", "login", "--config", cfg]


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
