"""Tests for the global `--repo PREFIX` argv lift."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlmodel import Session
from typer.testing import CliRunner

from jailbee import prompting, repo_option
from jailbee.db.models import RegisteredRepo
from jailbee.repo_option import RepoOptionError, lift_repo, with_repo_first


@pytest.mark.parametrize(
    ("argv", "prefix", "rest"),
    [
        (["--repo", "x", "ls"], "x", ["ls"]),
        (["ls", "--repo", "x"], "x", ["ls"]),
        (["ls", "--repo=x", "--all"], "x", ["ls", "--all"]),
        (["chrome", "feat", "--repo", "andbible"], "andbible", ["chrome", "feat"]),
        (["git", "--repo", "x", "pull", "feat"], "x", ["git", "pull", "feat"]),
        (["net", "--repo", "x", "egress", "ls"], "x", ["net", "egress", "ls"]),
        (["ls"], None, ["ls"]),
        ([], None, []),
    ],
)
def test_lift_finds_the_global_option_anywhere(argv, prefix, rest):
    assert lift_repo(argv) == (prefix, rest)


def test_tokens_after_double_dash_are_never_inspected():
    argv = ["exec", "feat", "--", "grep", "--repo", "x"]
    assert lift_repo(argv) == (None, argv)


def test_leaf_owned_repo_is_left_for_the_leaf():
    # `net egress add --repo` is the leaf's own repo-scope flag.
    argv = ["net", "egress", "add", "example.com", "--repo"]
    assert lift_repo(argv) == (None, argv)
    assert lift_repo(["console", "--repo", "x"]) == (None, ["console", "--repo", "x"])


def test_repo_inside_the_command_path_is_global_even_for_an_owning_leaf():
    argv = ["net", "--repo", "x", "egress", "add", "--repo", "example.com"]
    assert lift_repo(argv) == ("x", ["net", "egress", "add", "--repo", "example.com"])


def test_alias_paths_resolve_like_their_public_leaf():
    from jailbee.remote_ssh.router import leaf_owns_option, routable_leaf_paths

    assert "egress add" in routable_leaf_paths()
    assert leaf_owns_option("egress add", "--repo")
    # `egress add` is a hidden alias of `net egress add`, which owns `--repo`.
    argv = ["egress", "add", "example.com", "--repo"]
    assert lift_repo(argv) == (None, argv)


def test_unknown_command_lifts_only_a_leading_option():
    assert lift_repo(["--repo", "x", "nope", "--repo", "y"]) == (
        "x",
        ["nope", "--repo", "y"],
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--repo", "x", "ls", "--repo", "y"],
        ["ls", "--repo"],
        ["ls", "--repo", "--all"],
        ["ls", "--repo="],
        ["--repo"],
    ],
)
def test_malformed_or_repeated_option_is_an_error(argv):
    with pytest.raises(RepoOptionError):
        lift_repo(argv)


def test_repo_after_value_taking_option_is_still_lifted():
    # Click, not the lift, then reports `--base` missing a value.
    assert lift_repo(["new", "feat", "--base", "--repo", "x"]) == (
        "x",
        ["new", "feat", "--base"],
    )


def test_no_tree_walk_without_repo_option(mocker):
    walk = mocker.patch("jailbee.remote_ssh.router.routable_leaf_paths")
    assert lift_repo(["ls", "--all"]) == (None, ["ls", "--all"])
    walk.assert_not_called()


def test_with_repo_first():
    assert with_repo_first("x", ["ls"]) == ["--repo", "x", "ls"]
    assert with_repo_first(None, ["ls"]) == ["ls"]


@pytest.fixture
def repos(tmp_path, db_engine, monkeypatch):
    roots = {}
    with Session(db_engine) as db:
        for prefix in ("alpha", "beta"):
            root = tmp_path / prefix
            root.mkdir()
            roots[prefix] = root
            db.add(
                RegisteredRepo(
                    container_prefix=prefix,
                    repo_root=str(root),
                    registered_at=datetime(2026, 10, 5, tzinfo=UTC),
                )
            )
        db.commit()
    monkeypatch.setattr("jailbee.remote_ssh.router.get_engine", lambda: db_engine)
    monkeypatch.setattr("jailbee.remote_ssh.repo_scope.get_engine", lambda: db_engine)
    return roots


def test_enter_repo_changes_directory(repos, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert repo_option.enter_repo("beta", pick=False) == "beta"
    assert Path.cwd() == repos["beta"]


def test_unknown_prefix_names_the_registered_ones(repos):
    with pytest.raises(RepoOptionError, match=r"unknown registered repo: nope.*alpha, beta"):
        repo_option.enter_repo("nope", pick=False)


def test_excluded_repo_is_unknown_inside_a_restricted_session(repos, monkeypatch):
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

    monkeypatch.setattr(
        "jailbee.remote_ssh.repo_scope.scope_for_session",
        lambda: RemoteRepoScope(frozenset({"beta"})),
    )
    with pytest.raises(RepoOptionError, match=r"unknown registered repo: beta.*registered: alpha"):
        repo_option.enter_repo("beta", pick=False)


def test_pick_uses_choose_one_over_scoped_repos(repos, monkeypatch, mocker, tmp_path):
    monkeypatch.chdir(tmp_path)
    choose = mocker.patch("jailbee.prompting.choose_one", return_value="alpha")
    assert repo_option.enter_repo(None, pick=True) == "alpha"
    assert [o.label for o in choose.call_args.args[1]] == ["alpha", "beta"]
    assert Path.cwd() == repos["alpha"]


def test_pick_filters_excluded_repos(repos, monkeypatch, tmp_path):
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "jailbee.remote_ssh.repo_scope.scope_for_session",
        lambda: RemoteRepoScope(frozenset({"beta"})),
    )
    assert repo_option.enter_repo(None, pick=True) == "alpha"
    assert Path.cwd() == repos["alpha"]


@pytest.mark.parametrize("prefix,pick", [(None, False), ("alpha", True)])
def test_enter_repo_rejects_missing_or_conflicting_selection(repos, prefix, pick):
    with pytest.raises(RepoOptionError):
        repo_option.enter_repo(prefix, pick=pick)


def test_pick_noninteractive_missing_value(repos):
    with pytest.raises(prompting.MissingValue):
        repo_option.enter_repo(None, pick=True)


def test_pick_cancelled(repos, monkeypatch):
    monkeypatch.setattr(prompting, "is_interactive", lambda: True)
    monkeypatch.setattr(prompting, "_select", lambda *args: None)
    with pytest.raises(prompting.Cancelled):
        repo_option.enter_repo(None, pick=True)


def test_cli_trailing_repo_runs_in_that_repo(repos, monkeypatch, tmp_path):
    from jailbee.cli import app

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, with_repo_first(*lift_repo(["version", "--repo", "alpha"])))
    assert result.exit_code == 0, result.output
    assert Path.cwd() == repos["alpha"]


def test_cli_unknown_repo_exits_2(repos):
    from jailbee.cli import app

    result = CliRunner().invoke(app, ["--repo", "nope", "version"])
    assert result.exit_code == 2
    assert "unknown registered repo: nope" in result.output


def test_cli_repo_with_config_is_rejected(repos, tmp_path, monkeypatch):
    from jailbee.cli import app

    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "c.yaml"
    cfg.write_text("container_prefix: alpha\n")
    result = CliRunner().invoke(app, ["--repo", "alpha", "config", "show", "-c", str(cfg)])
    assert result.exit_code == 2
    assert "--config" in result.output and "--repo" in result.output


def test_cli_without_repo_preserves_cwd(tmp_path, monkeypatch):
    from jailbee.cli import app

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0, result.output
    assert Path.cwd() == tmp_path


def test_cli_pick_repo_is_hidden_and_enters_repo(repos, tmp_path, monkeypatch):
    from jailbee.cli import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(prompting, "choose_one", lambda *args, **kwargs: "beta")
    result = CliRunner().invoke(app, ["--pick-repo", "version"])
    assert result.exit_code == 0, result.output
    assert Path.cwd() == repos["beta"]
    assert "--pick-repo" not in CliRunner().invoke(app, ["--help"]).output


@pytest.mark.parametrize("option", [["-c", "/tmp/beta.yaml"], ["--config", "/tmp/beta.yaml"], ["--config=/tmp/beta.yaml"], ["-c/tmp/beta.yaml"], ["-c=/tmp/beta.yaml"]])
def test_lift_rejects_competing_config_before_stripping(option):
    with pytest.raises(RepoOptionError, match="--config and --repo"):
        lift_repo(["ls", *option, "--repo", "alpha"])


def test_config_in_opaque_payload_does_not_conflict():
    assert lift_repo(["exec", "feat", "--repo", "alpha", "--", "tool", "-c/tmp/x"]) == (
        "alpha", ["exec", "feat", "--", "tool", "-c/tmp/x"]
    )


@pytest.mark.parametrize("args", [
    ["outbox", "ls", "--config", "/tmp/beta.yaml"],
    ["outbox", "--config=/tmp/beta.yaml", "ls"],
    ["outbox", "drop", "feat", "issue/x.json", "-c/tmp/beta.yaml"],
])
def test_cli_outbox_config_conflict_never_loads_or_runs(args, mocker, monkeypatch, tmp_path):
    from jailbee.cli import app

    monkeypatch.chdir(tmp_path)
    mocker.patch("jailbee.repo_option.enter_repo", return_value="alpha")
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.outbox.commands.drop_selected", return_value=0)
    load = mocker.patch("jailbee.config.load_config")
    run = mocker.patch("jailbee.outbox.commands.show_overview", return_value=0)
    result = CliRunner().invoke(app, ["--repo", "alpha", *args])
    assert result.exit_code == 2, result.output
    assert "--config and --repo" in result.output
    load.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize("argv,rest", [
    (["outbox", "feat", "--repo", "alpha"], ["outbox", "feat"]),
    (["outbox", "--repo=alpha"], ["outbox"]),
    (["git", "--help", "--repo", "alpha"], ["git", "--help"]),
    (["--help", "--repo=alpha"], ["--help"]),
    (["git", "--repo", "alpha", "--help"], ["git", "--help"]),
])
def test_help_and_implicit_outbox_lift_without_rewriting(argv, rest):
    assert lift_repo(argv) == ("alpha", rest)


def test_repo_option_import_and_fast_path_are_lazy():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import sys; from jailbee.repo_option import lift_repo; "
         "assert lift_repo(['ls', '--all']) == (None, ['ls', '--all']); "
         "assert 'jailbee.remote_ssh.router' not in sys.modules; "
         "assert 'sqlalchemy' not in sys.modules"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
