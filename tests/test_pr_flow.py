"""Tests for the shared PR flow extracted from `jailbee pr`."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from jailbee import pr_flow


def _super_scope(tmp_path: Path) -> pr_flow.PrScope:
    return pr_flow.PrScope(repo_root=tmp_path, remote="origin", prefix="", subpath=None)


def _sub_scope(tmp_path: Path) -> pr_flow.PrScope:
    return pr_flow.PrScope(
        repo_root=tmp_path / "libs" / "foo",
        remote="upstream",
        prefix="submodule 'libs/foo': ",
        subpath="libs/foo",
    )


def test_noun_names_the_pr_number(tmp_path):
    assert _super_scope(tmp_path).noun("12") == "PR #12"


def test_noun_falls_back_without_a_number(tmp_path):
    assert _super_scope(tmp_path).noun(None) == "the container's PR"


def test_noun_is_prefixed_for_a_submodule(tmp_path):
    assert _sub_scope(tmp_path).noun("12") == "submodule 'libs/foo': PR #12"


def test_reject_as_on_pr_update_exits_2(tmp_path):
    with pytest.raises(typer.Exit) as excinfo:
        pr_flow.reject_as_on_pr_update(_super_scope(tmp_path), "user/x", "12")
    assert excinfo.value.exit_code == 2


def test_foreign_force_push_is_silent_with_yes(tmp_path, mocker):
    confirm = mocker.patch("typer.confirm")
    pr_flow.confirm_foreign_force_push(_super_scope(tmp_path), "feat-foo", "12", "user/x", yes=True)
    confirm.assert_not_called()


def test_foreign_force_push_exits_1_without_a_tty(tmp_path, mocker):
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    with pytest.raises(typer.Exit) as excinfo:
        pr_flow.confirm_foreign_force_push(
            _super_scope(tmp_path), "feat-foo", "12", "user/x", yes=False
        )
    assert excinfo.value.exit_code == 1


def test_foreign_force_push_aborts_on_decline(tmp_path, mocker):
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("typer.confirm", return_value=False)
    with pytest.raises(typer.Abort):
        pr_flow.confirm_foreign_force_push(
            _super_scope(tmp_path), "feat-foo", "12", "user/x", yes=False
        )


def test_confirm_branch_name_returns_proposal_when_equal_to_source(mocker):
    prompt = mocker.patch("typer.prompt")
    assert pr_flow.confirm_pr_branch_name("feat/foo", "feat/foo") == "feat/foo"
    prompt.assert_not_called()


def test_confirm_branch_name_returns_proposal_off_tty(mocker):
    mocker.patch("sys.stdin.isatty", return_value=False)
    prompt = mocker.patch("typer.prompt")
    assert pr_flow.confirm_pr_branch_name("user/ai", "feat/foo") == "user/ai"
    prompt.assert_not_called()


def test_confirm_branch_name_reprompts_until_valid(mocker):
    mocker.patch("sys.stdin.isatty", return_value=True)
    mocker.patch("typer.prompt", side_effect=["bad name", "user/ok"])
    mocker.patch("jailbee.git.check_ref_format", side_effect=[False, True])
    assert pr_flow.confirm_pr_branch_name("user/ai", "feat/foo") == "user/ok"
