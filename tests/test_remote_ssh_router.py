"""Tests for restricted remote SSH command routing."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session

from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig
from jailbee.db.models import RegisteredRepo
from jailbee.remote_ssh.router import (
    Route,
    RouteError,
    command_path,
    help_text,
    known_command_paths,
    policy_allows,
    resolve_repo,
    route,
)


@pytest.fixture
def engine(db_engine: Engine) -> Engine:
    return db_engine


@pytest.fixture
def repo(tmp_path, engine: Engine):
    root = tmp_path / "project"
    root.mkdir()
    with Session(engine) as session:
        session.add(
            RegisteredRepo(
                container_prefix="project",
                repo_root=str(root),
                registered_at=datetime(2026, 9, 17, tzinfo=UTC),
            )
        )
        session.commit()
    return root


@pytest.fixture
def configured_ssh() -> RemoteSSHConfig:
    return RemoteSSHConfig(
        shell=True,
        exec=True,
        commands=RemoteCommandPolicy(mode="full"),
    )


def test_empty_command_routes_to_help() -> None:
    result = route(None, RemoteSSHConfig())
    assert result == Route("help", (), None, None, False)


def test_whitespace_command_routes_to_help() -> None:
    assert route("  ", RemoteSSHConfig()).kind == "help"


@pytest.mark.parametrize("raw", ["\n", "\t", " \r "])
def test_control_whitespace_is_rejected_before_empty_routing(raw: str) -> None:
    with pytest.raises(RouteError, match="control character"):
        route(raw, RemoteSSHConfig())


def test_dashboard_is_registered_only_and_requires_pty() -> None:
    result = route("dashboard", RemoteSSHConfig())
    assert result.argv == ("dashboard", "--registered-only")
    assert result.requires_pty is True


def test_one_shot_resolves_repo_and_drops_remote_selector(engine, repo) -> None:
    cfg = RemoteSSHConfig(
        exec=True,
        commands=RemoteCommandPolicy(mode="allowlist", allow=["ls"]),
    )
    result = route("--repo project ls --all", cfg, engine=engine)
    assert result.kind == "command"
    assert result.argv == ("ls", "--all")
    assert result.repo_root == repo


@pytest.mark.parametrize(
    "raw",
    ["ls", "--repo project", "--repo /tmp ls", "--repo project --repo other ls"],
)
def test_invalid_one_shot_shapes_are_rejected(raw: str, configured_ssh, engine) -> None:
    with pytest.raises(RouteError):
        route(raw, configured_ssh, engine=engine)


def test_malformed_quoting_is_rejected(configured_ssh, engine) -> None:
    with pytest.raises(RouteError, match="parse remote command"):
        route('--repo project ls "unterminated', configured_ssh, engine=engine)


@pytest.mark.parametrize("control", ["\x00", "\n", "\x7f"])
def test_control_characters_are_rejected(control, configured_ssh, engine, repo) -> None:
    with pytest.raises(RouteError, match="control character"):
        raw = f"--repo project ls{control}--all"
        route(raw, configured_ssh, engine=engine)


def test_disabled_dashboard_and_extra_arguments_are_rejected() -> None:
    cfg = RemoteSSHConfig(
        dashboard=False,
        exec=True,
        commands=RemoteCommandPolicy(mode="full"),
    )
    with pytest.raises(RouteError, match="dashboard is disabled"):
        route("dashboard", cfg)
    with pytest.raises(RouteError):
        route("dashboard --all", cfg)


def test_console_routes_with_optional_registered_repo(engine, repo) -> None:
    cfg = RemoteSSHConfig(shell=True, commands=RemoteCommandPolicy(mode="full"))
    assert route("shell", cfg) == Route("console", ("_remote-console",), None, None, True)
    assert route("shell --repo project", cfg, engine=engine) == Route(
        "console",
        ("_remote-console", "--repo", "project"),
        "project",
        repo,
        True,
    )


@pytest.mark.parametrize("raw", ["shell now", "shell --repo", "shell --repo project extra"])
def test_invalid_console_shapes_are_rejected(raw: str, engine) -> None:
    cfg = RemoteSSHConfig(shell=True, commands=RemoteCommandPolicy(mode="full"))
    with pytest.raises(RouteError):
        route(raw, cfg, engine=engine)


def test_disabled_shell_and_exec_entrypoints_are_rejected(engine, repo) -> None:
    cfg = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"))
    with pytest.raises(RouteError, match="shell is disabled"):
        route("shell", cfg)
    with pytest.raises(RouteError, match="shell is disabled"):
        route("shell --repo unregistered", cfg, engine=engine)
    with pytest.raises(RouteError, match="execution is disabled"):
        route("--repo project ls", cfg, engine=engine)


def test_repo_resolution_rejects_unknown_and_missing_directories(engine, repo) -> None:
    with pytest.raises(RouteError, match="unknown registered repo: other"):
        resolve_repo("other", engine=engine)

    repo.rmdir()
    with pytest.raises(RouteError, match="registered repo directory is missing: project"):
        resolve_repo("project", engine=engine)


def test_command_tree_contains_only_public_leaves() -> None:
    paths = known_command_paths()
    assert "ls" in paths
    assert "git pull" in paths
    assert "git" not in paths
    assert "_new-worker" not in paths
    assert "_remote-console" not in paths


def test_command_path_rejects_options_before_the_leaf() -> None:
    assert command_path(("git", "pull", "--ff-only")) == "git pull"
    with pytest.raises(RouteError, match="unknown Jailbee command"):
        command_path(("--verbose", "ls"))


def test_allowlist_matches_the_exact_leaf_path() -> None:
    pull = RemoteCommandPolicy(mode="allowlist", allow=["git pull"])
    assert policy_allows(("git", "pull", "--ff-only"), pull) == "git pull"
    with pytest.raises(RouteError, match="Jailbee command is not allowed: git push"):
        policy_allows(("git", "push"), pull)

    group = RemoteCommandPolicy(mode="allowlist", allow=["git"])
    with pytest.raises(RouteError, match="Jailbee command is not allowed: git pull"):
        policy_allows(("git", "pull"), group)


def test_full_policy_allows_public_leaves_but_never_hidden_commands(engine, repo) -> None:
    cfg = RemoteSSHConfig(exec=True, commands=RemoteCommandPolicy(mode="full"))
    assert route("--repo project git pull --ff-only", cfg, engine=engine).argv == (
        "git",
        "pull",
        "--ff-only",
    )
    with pytest.raises(RouteError, match="unknown Jailbee command"):
        route("--repo project _remote-console", cfg, engine=engine)


def test_disabled_command_policy_rejects_public_commands() -> None:
    with pytest.raises(RouteError, match="remote Jailbee commands are disabled"):
        policy_allows(("ls",), RemoteCommandPolicy())


# --- A: hidden aliases resolve to their canonical public path (Problem A) ---


def test_command_path_resolves_aliases_to_their_canonical_public_leaf() -> None:
    assert command_path(("merge", "--into", "main")) == "git merge"
    assert command_path(("fetch",)) == "git fetch"
    assert command_path(("pull", "--ff-only")) == "git pull"
    assert command_path(("push",)) == "git push"
    assert command_path(("checkout", "main")) == "git checkout"
    assert command_path(("retarget", "main")) == "git retarget"
    assert command_path(("diff",)) == "git diff"
    assert command_path(("egress", "ls")) == "net egress ls"
    assert command_path(("git", "pr")) == "pr"


def test_command_path_still_rejects_hidden_commands_with_no_public_twin() -> None:
    for argv in [
        ("_remote-console",),
        ("_new-worker",),
        ("_destroy-worker",),
        ("_boot-worker",),
        ("_autostart-worker",),
        ("submodule", "checkout"),
        ("claude", "ls"),
        ("chrome-pool", "ls"),
    ]:
        with pytest.raises(RouteError, match="unknown Jailbee command"):
            command_path(argv)


def test_full_policy_allows_every_alias_whose_target_is_public() -> None:
    full = RemoteCommandPolicy(mode="full")
    assert policy_allows(("merge", "--into", "main"), full) == "git merge"
    assert policy_allows(("git", "pr"), full) == "pr"


def test_allowlist_permits_an_alias_whose_canonical_leaf_is_allowed() -> None:
    policy = RemoteCommandPolicy(mode="allowlist", allow=["git merge"])
    assert policy_allows(("merge", "--into", "main"), policy) == "git merge"


def test_allowlist_rejects_an_alias_whose_canonical_leaf_is_not_allowed() -> None:
    policy = RemoteCommandPolicy(mode="allowlist", allow=["git pull"])
    with pytest.raises(RouteError, match="Jailbee command is not allowed: git merge"):
        policy_allows(("merge",), policy)


def test_help_lists_only_configured_entrypoints() -> None:
    dashboard_only = help_text(RemoteSSHConfig())
    assert "  dashboard" in dashboard_only
    assert "  shell [--repo PREFIX]" not in dashboard_only
    assert "  --repo PREFIX COMMAND [ARGS...]" not in dashboard_only

    all_entrypoints = help_text(
        RemoteSSHConfig(
            shell=True,
            exec=True,
            commands=RemoteCommandPolicy(mode="full"),
        )
    )
    assert "  dashboard" in all_entrypoints
    assert "  shell [--repo PREFIX]" in all_entrypoints
    assert "  --repo PREFIX COMMAND [ARGS...]" in all_entrypoints
