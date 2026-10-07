"""The inline ``!`` command line on Pilot: what it runs, and what it refuses before spawning."""

from __future__ import annotations

import pytest

from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import overlay as toverlay
from jailbee.dashboard.tui import session as tsession
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import drive, keys, patch_pause

pytestmark = pytest.mark.usefixtures("no_real_branch_listing")


def test_inline_command_on_repo_header_leaves_merge_source_for_cli(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    wait = patch_pause(mocker)

    run = drive(mocker, ["!", *keys("merge"), "enter"], groups=[group])

    assert run.rc == 0
    child.assert_called_once_with(["jailbee", "merge"], cwd=tmp_path, check=False)
    wait.assert_called_once()


def test_inline_command_on_container_uses_selected_source(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)

    run = drive(mocker, ["j", "!", *keys("merge"), "enter"], groups=[group])

    assert run.rc == 0
    child.assert_called_once_with(["jailbee", "merge", "alpha-x"], cwd=tmp_path, check=False)


def test_inline_command_malformed_quote_notifies_without_spawning(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")

    run = drive(mocker, ["j", "!", *keys("merge '"), "enter"], groups=[group])

    child.assert_not_called()
    assert any("cannot parse command" in str(notice) for notice in run.notices())


def test_inline_command_refuses_orphan_before_spawning(mocker):
    group = dmodel.RepoGroup("alpha", None, None, [ci("alpha-x", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")

    run = drive(mocker, ["j", "!", *keys("merge"), "enter"], groups=[group])

    child.assert_not_called()
    assert any("view-only" in str(notice) for notice in run.notices())


def test_inline_command_refuses_without_selection_before_spawning(mocker):
    child = mocker.patch.object(tsession.subprocess, "run")

    run = drive(mocker, ["!", *keys("merge"), "enter"])

    child.assert_not_called()
    assert any("Select a repo or a container" in str(notice) for notice in run.notices())


def test_inline_command_refuses_ssh_policy_before_foreground_or_spawn(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")
    wait = patch_pause(mocker)
    policy = RemoteSSHConfig(
        exec=True, commands=RemoteCommandPolicy(mode="allowlist", allow=["git pull"])
    )

    run = drive(
        mocker,
        ["j", "!", *keys("merge"), "enter", "ctrl+c"],
        [group],
        remote=True,
        over_ssh=True,
        ssh_policy=policy,
    )

    child.assert_not_called()
    wait.assert_not_called()
    assert any("not allowed" in str(notice) for notice in run.notices())


def test_inline_command_reports_vanished_repo_and_returns_to_loop(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    mocker.patch.object(tsession.subprocess, "run", side_effect=OSError("gone"))

    run = drive(mocker, ["j", "!", *keys("merge"), "enter"], groups=[group])

    assert run.rc == 0
    assert any(str(tmp_path) in str(notice) for notice in run.notices())


def test_q_inside_inline_editor_is_text_and_does_not_quit(mocker):
    run = drive(mocker, ["!", "q", "escape", "q"])

    assert any(
        isinstance(overlay, toverlay.CommandState) and overlay.text == "q"
        for overlay in run.overlays()
    )
