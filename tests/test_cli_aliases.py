"""CLI tests for the hidden top-level `jailbee git` aliases and the `jailbee git merge` removal.

Every `jailbee git <sub>` command has a top-level alias (e.g. `jailbee checkout` ==
`jailbee git checkout`). The aliases are hidden from `jailbee --help` so the top-level
command list stays short, but they remain invocable and the canonical forms
are listed under `jailbee git --help`.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.lifecycle import ResolvedContainer

# Every canonical `jailbee git` subcommand that has a top-level alias.
ALIASES = ["fetch", "checkout", "pull", "retarget", "diff", "push"]


def test_jailbee_git_merge_returns_no_such_command():
    """`jailbee git merge feat-foo` must fail — the command was renamed to `pull`."""
    result = CliRunner().invoke(app, ["git", "merge", "feat-foo"])
    assert result.exit_code != 0
    combined = (result.output or "") + (result.stderr or "")
    assert "merge" in combined.lower()
    assert "no such command" in combined.lower()


# --- aliases are hidden from `jailbee --help` but reachable ---------------------


def test_jailbee_help_hides_all_git_aliases():
    """`jailbee --help` lists none of the git aliases (no 'Alias for' rows)."""
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "Alias for" not in result.output


@pytest.mark.parametrize("name", ALIASES)
def test_top_level_alias_is_invocable(name):
    """Each hidden alias is still registered and reachable via `--help`."""
    result = CliRunner().invoke(app, [name, "--help"])
    assert result.exit_code == 0, result.output
    assert f"jailbee git {name}" in result.output


@pytest.mark.parametrize("name", ALIASES)
def test_git_help_lists_canonical_command(name):
    """`jailbee git --help` lists each canonical subcommand."""
    result = CliRunner().invoke(app, ["git", "--help"])
    assert result.exit_code == 0, result.output
    assert name in result.output


# --- each alias routes to the same code path as `jailbee git <sub>` -------------


def test_top_level_fetch_calls_same_function(mocker, tmp_path):
    """`jailbee fetch feat-foo` runs the same code path as `jailbee git fetch ...`."""
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "sampleapp-feat-foo"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mocker.patch("jailbee.cli._print_fetch_summary")
    fetch_mock = mocker.patch("jailbee.sync.fetch_from_container")

    result = CliRunner().invoke(app, ["fetch", "feat-foo"])

    assert result.exit_code == 0, result.output
    fetch_mock.assert_called_once()


def test_top_level_checkout_calls_same_function(mocker, tmp_path):
    """`jailbee checkout feat-foo` runs the same code path as `jailbee git checkout ...`."""
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(
            mocker.MagicMock(),
            ResolvedContainer(name="sampleapp-feat-foo", auto_selected=False),
        ),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mocker.patch("jailbee.cli._print_fetch_summary")
    checkout_result = mocker.MagicMock()
    checkout_result.branch = "feat/foo"
    checkout_result.head_oid = "abc1234def"
    checkout_mock = mocker.patch(
        "jailbee.sync.checkout_from_container", return_value=checkout_result
    )

    result = CliRunner().invoke(app, ["checkout", "feat-foo"])

    assert result.exit_code == 0, result.output
    checkout_mock.assert_called_once()


def test_top_level_retarget_calls_same_function(mocker, tmp_path):
    """`jailbee retarget feat-foo main` runs the same code path as `jailbee git retarget ...`."""
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "sampleapp-feat-foo"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    retarget_result = mocker.MagicMock()
    retarget_result.old_base = "feat/a"
    retarget_result.new_base = "main"
    retarget_mock = mocker.patch("jailbee.sync.retarget_container", return_value=retarget_result)

    result = CliRunner().invoke(app, ["retarget", "feat-foo", "main"])

    assert result.exit_code == 0, result.output
    retarget_mock.assert_called_once()


def test_top_level_pull_calls_same_function(mocker, tmp_path):
    """`jailbee pull feat-foo --ff` runs the same code path as `jailbee git pull ...`."""
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    cfg_mock.pull.destroy_container = "never"
    cfg_mock.pull.delete_branch = "never"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing_detailed",
        return_value=(
            mocker.MagicMock(),
            ResolvedContainer(name="sampleapp-feat-foo", auto_selected=False),
        ),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    do_pull = mocker.patch("jailbee.cli._do_single_pull")

    result = CliRunner().invoke(app, ["pull", "feat-foo", "--ff"])

    assert result.exit_code == 0, result.output
    do_pull.assert_called_once()
    kwargs = do_pull.call_args.kwargs
    assert kwargs["ff_only"] is True


def test_top_level_push_calls_same_function(mocker, tmp_path):
    """`jailbee push feat-foo --merge` runs the same code path as `jailbee git push ...`."""
    from jailbee.sync import MergeInContainerResult, PushResult

    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    cfg_mock.push.default_action = "ask"
    cfg_mock.push.default_source = "default-branch"
    cfg_mock.default_branch = "main"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "sampleapp-feat-foo"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    push_mock = mocker.patch(
        "jailbee.sync.push_and_merge",
        return_value=MergeInContainerResult(
            push=PushResult(
                source="main",
                source_ref="refs/remotes/origin/main",
                container_ref="refs/jailbee/host/main",
                old_oid=None,
                new_oid="2222222",
            ),
            container_branch="feat/foo",
            fast_forward_only=False,
            head_oid="abcdef1234567",
        ),
    )

    result = CliRunner().invoke(app, ["push", "feat-foo", "--merge"])

    assert result.exit_code == 0, result.output
    push_mock.assert_called_once()


def test_top_level_diff_calls_same_function(mocker, tmp_path):
    """`jailbee diff feat-foo --stat` runs the same code path as `jailbee git diff ...`."""
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "sampleapp-feat-foo"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    diff_mock = mocker.patch("jailbee.sync.diff_from_container", return_value="patch")

    result = CliRunner().invoke(app, ["diff", "feat-foo", "--stat"])

    assert result.exit_code == 0, result.output
    diff_mock.assert_called_once()
    assert diff_mock.call_args.kwargs["stat_only"] is True


# --- `jailbee pr` is canonical/visible; `jailbee git pr` is the hidden alias --------
# (inverted from the other git subcommands: `pr` used to live only under
# `jailbee git create-pr`, with a hidden top-level `create-pr` alias.)


def test_pr_top_level_help_shows_full_docstring():
    """`jailbee pr --help` shows the canonical command docstring."""
    result = CliRunner().invoke(app, ["pr", "--help"])
    assert result.exit_code == 0, result.output
    assert "Create or update" in result.output


def test_git_pr_alias_is_invocable():
    """`jailbee git pr --help` is reachable (hidden alias)."""
    result = CliRunner().invoke(app, ["git", "pr", "--help"])
    assert result.exit_code == 0, result.output


def test_git_help_hides_pr_alias():
    """`jailbee git --help` does not advertise the hidden `pr` alias."""
    result = CliRunner().invoke(app, ["git", "--help"])
    assert result.exit_code == 0, result.output
    assert "Create or update" not in result.output
