"""Tests for the console entry point's top-level error handling."""

from __future__ import annotations

import sys

import pytest

from jailbee.config import ConfigError
from jailbee.entry import main, rewrite_app_argv
from jailbee.incus import IncusError
from jailbee.repo_option import RepoOptionError


def test_incus_error_is_reported_as_a_message_not_a_traceback(mocker, capsys):
    """An IncusError reaching the top is jailbee's own diagnosis, not a crash.

    It already names the failing command and carries whatever incus wrote to
    stderr, so Typer's traceback hook adds a screenful of jailbee internals
    on top and buries the one line that matters. A host with no `incus`
    binary hit this on every command.
    """
    mocker.patch("jailbee.macos.maybe_delegate")
    # An option-shaped argv takes rewrite_app_argv's early-return branch, so
    # this test doesn't depend on however the real test runner was invoked.
    mocker.patch.object(sys, "argv", ["jailbee", "--help"])
    mocker.patch(
        "jailbee.cli.app",
        side_effect=IncusError("`incus` not found in PATH — Incus is not installed"),
    )

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "not found in PATH" in err
    assert "Traceback" not in err


def test_successful_run_is_left_alone(mocker):
    """The handler must not swallow the normal path or its exit code."""
    mocker.patch("jailbee.macos.maybe_delegate")
    mocker.patch.object(sys, "argv", ["jailbee", "--help"])
    app = mocker.patch("jailbee.cli.app")

    main()

    app.assert_called_once_with()


def test_main_applies_the_rewrite_before_running_the_app(mocker):
    """`main()` must actually route sys.argv through rewrite_app_argv.

    The two tests above deliberately use `--help`-shaped argv, which takes
    `rewrite_app_argv`'s early-return branch — they would still pass even if
    `main()` never called it at all. This one exercises the rewrite path
    itself, so removing the wiring line in `main()` fails it.
    """
    mocker.patch("jailbee.macos.maybe_delegate")
    mocker.patch("jailbee.entry._command_names", return_value=set())
    mocker.patch("jailbee.entry._top_level_app_names", return_value={"figma"})
    mocker.patch.object(sys, "argv", ["jailbee", "figma", "--flag"])
    app = mocker.patch("jailbee.cli.app")

    main()

    app.assert_called_once_with()
    assert sys.argv == ["jailbee", "apps", "run", "figma", "--flag"]


def test_a_registered_command_is_left_alone(mocker):
    load = mocker.patch("jailbee.entry._top_level_app_names")
    assert rewrite_app_argv(["ls", "--all"]) == ["ls", "--all"]
    # A known command must not even reach the config load.
    assert not load.called


def test_an_option_is_left_alone(mocker):
    load = mocker.patch("jailbee.entry._top_level_app_names")
    assert rewrite_app_argv(["--help"]) == ["--help"]
    assert not load.called


def test_empty_argv_is_left_alone():
    assert rewrite_app_argv([]) == []


def test_a_top_level_app_is_rewritten(mocker):
    mocker.patch("jailbee.entry._top_level_app_names", return_value={"figma"})
    assert rewrite_app_argv(["figma", "--flag"]) == ["apps", "run", "figma", "--flag"]


def test_the_container_option_survives_the_rewrite(mocker):
    """The documented way to name a container for a promoted app.

    `apps run` takes the container as an *option*, so the docs must teach
    `jailbee <app> --container c1` and never `jailbee <app> c1` — the latter
    lands in the app's variadic `args` and launches in the default
    container. This pins the spelling four documents now describe: a
    rewrite that reordered, swallowed or reinterpreted the argv after the
    app name would fail here.
    """
    mocker.patch("jailbee.entry._top_level_app_names", return_value={"figma"})
    assert rewrite_app_argv(["figma", "--container", "c1"]) == [
        "apps",
        "run",
        "figma",
        "--container",
        "c1",
    ]


def test_the_documented_spellings_bind_the_container_option_not_args(mocker):
    """Parse the rewritten argv the way Typer will, and assert where each
    token lands.

    `test_the_container_option_survives_the_rewrite` pins the rewrite;
    this pins the *consequence* — that click binds `--container` to the
    option and a bare positional to `args`. It is the failure the four
    documents taught: no error, the app just launches in the wrong
    container. Asserting on the parse rather than on the argv list is what
    makes this test able to notice `apps run` growing a positional
    container slot (which would make the old docs correct again, and this
    test wrong on purpose).
    """
    import typer.main

    from jailbee.cli import app
    from jailbee.entry import rewrite_app_argv

    mocker.patch("jailbee.entry._top_level_app_names", return_value={"figma"})
    group = typer.main.get_command(app)
    apps_group = group.commands["apps"]
    run_cmd = apps_group.commands["run"]

    def parse(argv: list[str]) -> dict[str, object]:
        ctx = run_cmd.make_context("run", rewrite_app_argv(argv)[2:], resilient_parsing=True)
        return ctx.params

    documented = parse(["figma", "--container", "c1"])
    assert documented["container"] == "c1"
    assert documented["args"] == ()

    broken = parse(["figma", "c1"])
    assert broken["container"] is None
    assert broken["args"] == ("c1",)


def test_an_unknown_name_falls_through_to_typers_own_error(mocker):
    mocker.patch("jailbee.entry._top_level_app_names", return_value={"figma"})
    assert rewrite_app_argv(["lss"]) == ["lss"]


def test_a_broken_config_does_not_break_unrelated_commands(mocker):
    # Outside a repo, or with a config that fails validation, an unknown
    # first argument must still reach Typer's own error rather than a
    # traceback from the config loader. ConfigError is what the loader
    # actually raises for both of those cases (see config/loader.py) — the
    # same exception `cli._run_dashboard` catches from the identical
    # `load_repo_config(Path.cwd())` call.
    mocker.patch("jailbee.entry._top_level_app_names", side_effect=ConfigError("boom"))
    assert rewrite_app_argv(["lss"]) == ["lss"]


def test_an_unrelated_internal_error_propagates(mocker):
    """A failure that is not one of the config loader's own documented modes
    must surface, not be silently treated as "not an app".

    This is the property a narrow `except (ConfigError, OSError)` buys over
    a bare `except Exception`: a bug in this module's own code, or an
    unrelated internal error, must still produce a traceback rather than
    quietly falling through to Typer's unknown-command error.
    """
    mocker.patch("jailbee.entry._top_level_app_names", side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        rewrite_app_argv(["lss"])


def test_command_names_come_from_the_live_typer_app():
    from jailbee.entry import _command_names

    names = _command_names()
    assert {"ls", "new", "shell", "exec", "apps"} <= names


def test_prepare_moves_a_trailing_repo_to_the_front(mocker, tmp_path):
    from jailbee.entry import prepare_argv

    mocker.patch("jailbee.repo_option.resolve_repo_root", return_value=tmp_path)
    mocker.patch("jailbee.entry._top_level_app_names", return_value=set())
    assert prepare_argv(["ls", "--repo", "x"]) == ["--repo", "x", "ls"]


def test_prepare_reads_top_level_apps_from_the_named_repo(mocker, tmp_path):
    from jailbee.entry import prepare_argv

    mocker.patch("jailbee.repo_option.resolve_repo_root", return_value=tmp_path)
    apps = mocker.patch("jailbee.entry._top_level_app_names", return_value={"figma"})
    assert prepare_argv(["figma", "feat", "--repo", "x"]) == [
        "--repo",
        "x",
        "apps",
        "run",
        "figma",
        "feat",
    ]
    apps.assert_called_once_with(tmp_path)


@pytest.mark.parametrize(
    "argv",
    [
        ["lss", "--repo", "x"],
        ["lss", "--repo"],
        ["lss", "--repo="],
        ["lss", "--repo", "x", "--repo", "y"],
        ["--repo", "x", "lss", "--repo", "y"],
        ["--repo", "x", "lss", "--repo"],
    ],
)
def test_prepare_preserves_unknown_commands(mocker, tmp_path, argv):
    from jailbee.entry import prepare_argv

    mocker.patch("jailbee.repo_option.resolve_repo_root", return_value=tmp_path)
    mocker.patch("jailbee.entry._top_level_app_names", return_value={"figma"})
    assert prepare_argv(argv) == argv


def test_prepare_does_not_use_cwd_apps_for_an_unresolved_trailing_repo(mocker):
    from jailbee.entry import prepare_argv

    mocker.patch("jailbee.repo_option.resolve_repo_root", side_effect=RepoOptionError("unknown"))
    mocker.patch("jailbee.entry._top_level_app_names", return_value={"figma"})
    assert prepare_argv(["figma", "--repo", "missing"]) == ["figma", "--repo", "missing"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--repo", "x", "ls", "--repo", "y"],
        ["figma", "--repo", "x", "--repo", "y"],
        ["--repo", "x", "figma", "--repo", "y"],
        ["figma", "--repo"],
    ],
)
def test_prepare_rejects_malformed_repo_for_known_commands_and_apps(mocker, tmp_path, argv):
    from jailbee.entry import prepare_argv

    mocker.patch("jailbee.repo_option.resolve_repo_root", return_value=tmp_path)
    mocker.patch("jailbee.entry._top_level_app_names", return_value={"figma"})
    with pytest.raises(RepoOptionError):
        prepare_argv(argv)


def test_main_reports_a_bad_repo_option_and_exits_2(mocker, capsys):
    mocker.patch("jailbee.macos.maybe_delegate")
    mocker.patch.object(sys, "argv", ["jailbee", "ls", "--repo"])
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 2
    assert "--repo needs" in capsys.readouterr().err


def test_prepare_uses_selected_repo_apps_not_cwd_apps(mocker, tmp_path, make_cfg):
    from jailbee.config import AppEntry
    from jailbee.entry import prepare_argv

    cfg = make_cfg(tmp_path)
    selected = cfg.model_copy(
        update={"apps": {"figma": AppEntry(command=["figma"], top_level=True)}}
    )
    elsewhere = cfg.model_copy(
        update={"apps": {"other": AppEntry(command=["other"], top_level=True)}}
    )
    mocker.patch("jailbee.repo_option.resolve_repo_root", return_value=tmp_path)
    mocker.patch(
        "jailbee.config.load_repo_config",
        side_effect=lambda root: selected if root == tmp_path else elsewhere,
    )
    assert prepare_argv(["figma", "--repo=x"]) == ["--repo", "x", "apps", "run", "figma"]
    assert prepare_argv(["other", "--repo=x"]) == ["other", "--repo=x"]


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["figma", "--", "--repo", "x"], ["apps", "run", "figma", "--", "--repo", "x"]),
        (["--repo=x", "lss", "--repo=y"], ["--repo", "x", "lss", "--repo=y"]),
        (
            ["figma", "--container", "c1", "--repo=x"],
            ["--repo", "x", "apps", "run", "figma", "--container", "c1"],
        ),
        (
            ["net", "egress", "add", "example.com", "--repo"],
            ["net", "egress", "add", "example.com", "--repo"],
        ),
    ],
)
def test_prepare_preserves_literal_payloads_and_leaf_options(mocker, tmp_path, argv, expected):
    from jailbee.entry import prepare_argv

    mocker.patch("jailbee.repo_option.resolve_repo_root", return_value=tmp_path)
    mocker.patch("jailbee.entry._top_level_app_names", return_value={"figma"})
    assert prepare_argv(argv) == expected


def test_main_lifts_repo_before_running_the_app(mocker, tmp_path):
    from jailbee.remote_ssh.router import _command_tree

    _command_tree.cache_clear()
    mocker.patch("jailbee.macos.maybe_delegate")
    mocker.patch("jailbee.repo_option.resolve_repo_root", return_value=tmp_path)
    mocker.patch.object(sys, "argv", ["jailbee", "ls", "--repo", "x"])
    mocker.patch("typer.Typer.__call__")
    main()
    assert sys.argv == ["jailbee", "--repo", "x", "ls"]


def test_module_entry_lifts_a_trailing_repo(mocker):
    import runpy

    from jailbee.remote_ssh.router import _command_tree

    _command_tree.cache_clear()
    mocker.patch.object(sys, "argv", ["jailbee", "version", "--repo", "x"])
    mocker.patch("typer.Typer.__call__")
    runpy.run_module("jailbee", run_name="__main__")
    assert sys.argv == ["jailbee", "--repo", "x", "version"]


def test_module_entry_reports_a_malformed_repo(mocker, capsys):
    import runpy

    from jailbee.remote_ssh.router import _command_tree

    _command_tree.cache_clear()
    mocker.patch.object(sys, "argv", ["jailbee", "version", "--repo"])
    mocker.patch("typer.Typer.__call__")
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("jailbee", run_name="__main__")
    assert excinfo.value.code == 2
    assert "--repo needs" in capsys.readouterr().err
