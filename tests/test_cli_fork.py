"""`jailbee fork` and the hidden `jailbee new --fork-of` it delegates to."""

from __future__ import annotations

import pytest

from jailbee.forking import ForkError, ForkSource


def _setup(tmp_path, mocker):
    from tests.test_cli import _setup_new_cmd_env

    env = _setup_new_cmd_env(tmp_path, mocker)
    import jailbee.incus

    # The fork's name is checked free before its commit is pinned under it.
    jailbee.incus.Incus.return_value.exists.return_value = False
    mocker.patch("jailbee.forking.pin_fork_commit", return_value="refs/jailbee/b/HEAD")
    return env


def _run(args):
    from typer.testing import CliRunner

    from jailbee.cli import app

    return CliRunner().invoke(app, args)


SRC = ForkSource("myrepo-src", "feat/a", "abc123", "main")


def test_new_fork_of_pins_clone_and_records_source(tmp_path, mocker):
    _, new_container = _setup(tmp_path, mocker)
    prep = mocker.patch("jailbee.forking.prepare_fork", return_value=SRC)
    result = _run(
        ["new", "--fork-of", "src", "--name", "myrepo-b", "--no-autostart", "--no-background"]
    )
    assert result.exit_code == 0, result.output
    assert prep.call_args.args[2] == "src"
    opts = new_container.call_args.args[2]
    assert (opts.container_branch, opts.name) == ("feat/a", "myrepo-b")
    assert opts.clone_commit == "abc123"
    assert opts.base_branch_label == "main"
    assert opts.base is None
    assert opts.clone is True
    assert opts.fork_of == "myrepo-src"


@pytest.mark.parametrize("untrusted", [False, True])
def test_new_fork_of_carries_the_sources_untrusted_head(tmp_path, mocker, untrusted):
    # A fork of a `--pr` review container must not launder the PR author's
    # autostart config into a container that runs it unasked.
    import dataclasses

    _, new_container = _setup(tmp_path, mocker)
    mocker.patch(
        "jailbee.forking.prepare_fork",
        return_value=dataclasses.replace(SRC, untrusted=untrusted),
    )
    result = _run(
        ["new", "--fork-of", "src", "--name", "myrepo-b", "--no-autostart", "--no-background"]
    )
    assert result.exit_code == 0, result.output
    assert new_container.call_args.args[2].untrusted_head is untrusted


def test_new_fork_of_pins_the_commit_under_the_forks_name(tmp_path, mocker):
    _, new_container = _setup(tmp_path, mocker)
    mocker.patch("jailbee.forking.prepare_fork", return_value=SRC)
    calls = mocker.MagicMock()
    pin = mocker.patch("jailbee.forking.pin_fork_commit", side_effect=lambda *a: calls.pin())
    new_container.side_effect = lambda *a, **k: (calls.new(), "myrepo-b")[1]
    result = _run(
        ["new", "--fork-of", "src", "--name", "myrepo-b", "--no-autostart", "--no-background"]
    )
    assert result.exit_code == 0, result.output
    assert pin.call_args.args[1:] == ("myrepo-b", "abc123")
    assert [c[0] for c in calls.mock_calls] == ["pin", "new"]


def test_new_fork_of_pins_before_the_background_dispatch(tmp_path, mocker):
    # The background path's first act is its own existence check; making that
    # second `exists()` answer True stops the run there, with the pin
    # already written: proof the pin precedes the detached worker.
    import jailbee.incus

    _, new_container = _setup(tmp_path, mocker)
    jailbee.incus.Incus.return_value.exists.side_effect = [False, True]
    mocker.patch("jailbee.forking.prepare_fork", return_value=SRC)
    pin = mocker.patch("jailbee.forking.pin_fork_commit")
    result = _run(["new", "--fork-of", "src", "--name", "myrepo-b", "--no-autostart", "-b"])
    assert "already exists" in result.output
    assert pin.call_args.args[1:] == ("myrepo-b", "abc123")
    new_container.assert_not_called()


def test_new_fork_of_existing_name_exits_2_without_pinning(tmp_path, mocker):
    # Pinning first would move an existing fork's own pin.
    import jailbee.incus

    _, new_container = _setup(tmp_path, mocker)
    jailbee.incus.Incus.return_value.exists.return_value = True
    mocker.patch("jailbee.forking.prepare_fork", return_value=SRC)
    pin = mocker.patch("jailbee.forking.pin_fork_commit")
    result = _run(
        ["new", "--fork-of", "src", "--name", "myrepo-b", "--no-autostart", "--no-background"]
    )
    assert result.exit_code == 2
    assert "already exists" in result.output
    pin.assert_not_called()
    new_container.assert_not_called()


def test_new_fork_of_pin_failure_exits_1(tmp_path, mocker):
    from jailbee.git import GitError

    _, new_container = _setup(tmp_path, mocker)
    mocker.patch("jailbee.forking.prepare_fork", return_value=SRC)
    mocker.patch("jailbee.forking.pin_fork_commit", side_effect=GitError("could not pin"))
    result = _run(
        ["new", "--fork-of", "src", "--name", "myrepo-b", "--no-autostart", "--no-background"]
    )
    assert result.exit_code == 1
    assert "could not pin" in result.output
    new_container.assert_not_called()


def test_new_fork_of_skips_existing_branch_confirm(tmp_path, mocker):
    # A same-branch fork always names a branch that exists; asking whether to
    # reuse it would fire on every fork.
    _, new_container = _setup(tmp_path, mocker)
    mocker.patch("jailbee.forking.prepare_fork", return_value=SRC)
    exists = mocker.patch("jailbee.git.branch_exists_in_source", return_value=True)
    result = _run(
        ["new", "--fork-of", "src", "--name", "myrepo-b", "--no-autostart", "--no-background"]
    )
    assert result.exit_code == 0, result.output
    exists.assert_not_called()
    new_container.assert_called_once()


def test_new_fork_of_with_branch_positional_uses_it(tmp_path, mocker):
    _, new_container = _setup(tmp_path, mocker)
    mocker.patch("jailbee.forking.prepare_fork", return_value=SRC)
    mocker.patch("jailbee.git.branch_exists_in_source", return_value=False)
    result = _run(
        [
            "new",
            "feat/b",
            "--fork-of",
            "src",
            "--name",
            "myrepo-b",
            "--no-autostart",
            "--no-background",
        ]
    )
    assert result.exit_code == 0, result.output
    opts = new_container.call_args.args[2]
    assert opts.container_branch == "feat/b"
    assert opts.clone_commit == "abc123"


@pytest.mark.parametrize(
    "extra",
    [["--pr", "1"], ["--mount"], ["--current"], ["--no-clone"]],
)
def test_new_fork_of_rejects_conflicting_flags(tmp_path, mocker, extra):
    _setup(tmp_path, mocker)
    prep = mocker.patch("jailbee.forking.prepare_fork", return_value=SRC)
    resolve_pr = mocker.patch("jailbee.pr.resolve_pr")
    result = _run(["new", "--fork-of", "src", "--name", "myrepo-b", *extra])
    assert result.exit_code == 2, result.output
    assert "not applicable" in " ".join(result.output.split())
    prep.assert_not_called()
    resolve_pr.assert_not_called()


def test_new_fork_of_rejects_base_positional(tmp_path, mocker):
    _setup(tmp_path, mocker)
    prep = mocker.patch("jailbee.forking.prepare_fork", return_value=SRC)
    result = _run(["new", "feat/b", "main", "--fork-of", "src", "--name", "myrepo-b"])
    assert result.exit_code == 2, result.output
    assert "BASE" in result.output
    prep.assert_not_called()


def test_new_fork_of_requires_name(tmp_path, mocker):
    _setup(tmp_path, mocker)
    prep = mocker.patch("jailbee.forking.prepare_fork", return_value=SRC)
    result = _run(["new", "--fork-of", "src", "--no-autostart"])
    assert result.exit_code == 2
    assert "--name" in result.output
    prep.assert_not_called()


def test_new_fork_of_fork_error_exits_2(tmp_path, mocker):
    _, new_container = _setup(tmp_path, mocker)
    mocker.patch(
        "jailbee.forking.prepare_fork",
        side_effect=ForkError("uncommitted changes in 'src'"),
    )
    result = _run(["new", "--fork-of", "src", "--name", "myrepo-b", "--no-autostart"])
    assert result.exit_code == 2
    assert "uncommitted changes" in result.output
    new_container.assert_not_called()


def test_new_fork_of_detached_head_drops_the_branch_flag_hint(tmp_path, mocker):
    # `sync` suggests `--branch`, a flag of `git fetch`; for a fork it would
    # point at `jailbee fork --branch`, which names the *new* branch instead.
    _, new_container = _setup(tmp_path, mocker)
    mocker.patch(
        "jailbee.forking.prepare_fork",
        side_effect=ForkError(
            "Cannot determine branch for container 'src'. Use --branch <name> to specify."
        ),
    )
    result = _run(["new", "--fork-of", "src", "--name", "myrepo-b", "--no-autostart"])
    assert result.exit_code == 2
    assert "Cannot determine branch" in result.output
    assert "--branch <name>" not in result.output
    new_container.assert_not_called()


def test_new_fork_of_git_error_exits_1(tmp_path, mocker):
    from jailbee.git import GitError

    _, new_container = _setup(tmp_path, mocker)
    mocker.patch("jailbee.forking.prepare_fork", side_effect=GitError("fetch failed: boom"))
    result = _run(["new", "--fork-of", "src", "--name", "myrepo-b", "--no-autostart"])
    assert result.exit_code == 1
    assert "fetch failed: boom" in result.output
    new_container.assert_not_called()


def test_fork_cmd_delegates(tmp_path, mocker):
    _setup(tmp_path, mocker)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(mocker.MagicMock(), "myrepo-src"))
    new_cmd = mocker.patch("jailbee.cli.new_cmd")
    result = _run(["fork", "src", "b", "--memory", "8GB", "--net", "loose", "-y"])
    assert result.exit_code == 0, result.output
    kw = new_cmd.call_args.kwargs
    assert new_cmd.call_args.args == ()
    assert kw["fork_of"] == "src"
    assert kw["name"] == "myrepo-b"
    assert kw["container_branch"] is None
    assert kw["memory"] == "8GB"
    assert kw["network"] == "loose"
    assert kw["yes"] is True
    # Not forwarded: these stay at new_cmd's defaults.
    assert kw["base"] is None
    assert kw["pr"] is None
    assert kw["mount"] is False
    assert kw["current"] is False
    assert kw["no_clone"] is False


def test_fork_cmd_passes_every_new_cmd_parameter_by_keyword(tmp_path, mocker):
    import inspect

    from jailbee import cli

    _setup(tmp_path, mocker)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(mocker.MagicMock(), "myrepo-src"))
    expected = set(inspect.signature(cli.new_cmd).parameters)
    new_cmd = mocker.patch("jailbee.cli.new_cmd")
    result = _run(["fork", "src", "b"])
    assert result.exit_code == 0, result.output
    assert set(new_cmd.call_args.kwargs) == expected


def test_fork_cmd_branch_flag(tmp_path, mocker):
    _setup(tmp_path, mocker)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(mocker.MagicMock(), "myrepo-src"))
    new_cmd = mocker.patch("jailbee.cli.new_cmd")
    result = _run(["fork", "src", "b", "--branch", "feat/b"])
    assert result.exit_code == 0, result.output
    assert new_cmd.call_args.kwargs["container_branch"] == "feat/b"


def test_fork_cmd_invalid_name_exits_2(tmp_path, mocker):
    _setup(tmp_path, mocker)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(mocker.MagicMock(), "myrepo-src"))
    new_cmd = mocker.patch("jailbee.cli.new_cmd")
    result = _run(["fork", "src", "///"])
    assert result.exit_code == 2, result.output
    new_cmd.assert_not_called()


def test_fork_cmd_missing_name_off_tty_exits_2(tmp_path, mocker):
    _setup(tmp_path, mocker)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(mocker.MagicMock(), "myrepo-src"))
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    new_cmd = mocker.patch("jailbee.cli.new_cmd")
    result = _run(["fork", "src"])
    assert result.exit_code == 2
    new_cmd.assert_not_called()


def test_fork_cmd_missing_name_asks(tmp_path, mocker):
    _setup(tmp_path, mocker)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(mocker.MagicMock(), "myrepo-src"))
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.prompting._ask", return_value="b")
    new_cmd = mocker.patch("jailbee.cli.new_cmd")
    result = _run(["fork", "src"])
    assert result.exit_code == 0, result.output
    assert new_cmd.call_args.kwargs["name"] == "myrepo-b"


@pytest.mark.parametrize(
    "argv",
    [
        ("fork", "src", "b", "--net", "loose"),
        ("fork", "src", "b", "--net=loose"),
    ],
)
def test_remote_fork_net_loose_needs_the_network_switch(argv, monkeypatch):
    from jailbee.config.models_remote import RemoteCommandPolicy
    from jailbee.remote_ssh.router import RemoteUnlocks, RouteError, policy_allows

    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)
    full = RemoteCommandPolicy(mode="full")
    with pytest.raises(RouteError, match=r"remote\.ssh\.network"):
        policy_allows(argv, full)
    assert policy_allows(argv, full, unlocks=RemoteUnlocks(network=True)) == "fork"
    assert policy_allows(("fork", "src", "b"), full) == "fork"
