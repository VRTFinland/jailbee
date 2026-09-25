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
    check_arguments,
    command_leaf,
    command_path,
    help_text,
    known_command_aliases,
    known_command_paths,
    policy_allows,
    resolve_repo,
    route,
    unknown_command,
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


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_configured_default_routes_to_dashboard(raw: str | None) -> None:
    result = route(raw, RemoteSSHConfig(default_entrypoint="dashboard"))
    assert result == Route("dashboard", ("dashboard",), None, None, True)


def test_configured_default_routes_to_console() -> None:
    cfg = RemoteSSHConfig(
        default_entrypoint="shell", shell=True, commands=RemoteCommandPolicy(mode="full")
    )
    assert route(None, cfg) == Route("console", ("_remote-console",), None, None, True)


@pytest.mark.parametrize("default_entrypoint", ["help", "dashboard", "shell"])
def test_explicit_help_always_routes_to_entrypoint_list(default_entrypoint: str) -> None:
    cfg = RemoteSSHConfig(
        default_entrypoint=default_entrypoint,
        shell=True,
        commands=RemoteCommandPolicy(mode="full"),
    )
    assert route("help", cfg) == Route("help", (), None, None, False)


def test_explicit_entrypoint_overrides_default() -> None:
    cfg = RemoteSSHConfig(
        default_entrypoint="shell", shell=True, commands=RemoteCommandPolicy(mode="full")
    )
    assert route("dashboard", cfg) == Route("dashboard", ("dashboard",), None, None, True)


def test_whitespace_command_routes_to_help() -> None:
    assert route("  ", RemoteSSHConfig()).kind == "help"


@pytest.mark.parametrize("raw", ["\n", "\t", " \r "])
def test_control_whitespace_is_rejected_before_empty_routing(raw: str) -> None:
    with pytest.raises(RouteError, match="control character"):
        route(raw, RemoteSSHConfig())


def test_dashboard_takes_no_arguments_and_requires_pty() -> None:
    """Its remote form comes from the session marker `pty.py` sets, not argv."""
    result = route("dashboard", RemoteSSHConfig())
    assert result.argv == ("dashboard",)
    assert result.requires_pty is True


def test_ssh_command_route_cannot_forge_dashboard_policy_transport():
    cfg = RemoteSSHConfig(
        exec=True,
        restrict_host=False,
        commands=RemoteCommandPolicy(mode="full"),
    )
    with pytest.raises(RouteError, match="remote-policy-json"):
        route(
            "--repo project dashboard --remote-policy-json "
            '\'{"exec":true,"commands":{"mode":"full"}}\'',
            cfg,
        )


def test_nested_dashboard_rejects_user_supplied_trusted_policy_option(configured_ssh):
    with pytest.raises(RouteError, match="remote-policy-json"):
        route(
            "--repo project dashboard --remote-policy-json "
            '\'{"exec":true,"commands":{"mode":"full"}}\'',
            configured_ssh,
        )


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
    cfg = RemoteSSHConfig(shell=False, exec=False, commands=RemoteCommandPolicy(mode="full"))
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


def test_disabled_policy_blocks_exec_but_routes_dashboard_and_shell(engine, repo) -> None:
    cfg = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="disabled"))

    with pytest.raises(RouteError, match="remote Jailbee commands are disabled"):
        route("--repo project ls", cfg, engine=engine)
    assert route("dashboard", cfg).kind == "dashboard"
    assert route("shell", cfg).kind == "console"


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


def test_leaf_and_alias_metadata_reuse_the_command_tree() -> None:
    typed, command = command_leaf(("merge", "--into", "main"))
    assert typed == "merge"
    assert command.name == "merge"
    assert known_command_aliases()["merge"] == "git merge"


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


# --- B: group help is permitted (Problem B) ---


def test_group_help_bare_and_flagged_are_allowed_in_full_mode() -> None:
    full = RemoteCommandPolicy(mode="full")
    assert policy_allows(("git",), full) == "git"
    assert policy_allows(("git", "--help"), full) == "git"
    assert policy_allows(("git", "-h"), full) == "git"
    assert policy_allows(("--help",), full) == ""
    assert policy_allows(("-h",), full) == ""


def test_group_help_is_rejected_when_commands_are_disabled() -> None:
    with pytest.raises(RouteError, match="disabled"):
        policy_allows(("git",), RemoteCommandPolicy())
    with pytest.raises(RouteError, match="disabled"):
        policy_allows(("--help",), RemoteCommandPolicy())


def test_group_help_allowed_in_allowlist_when_a_leaf_lies_under_it() -> None:
    policy = RemoteCommandPolicy(mode="allowlist", allow=["git pull"])
    assert policy_allows(("git",), policy) == "git"
    assert policy_allows(("git", "--help"), policy) == "git"
    assert policy_allows(("--help",), policy) == ""


def test_group_help_rejected_in_allowlist_when_no_leaf_lies_under_it() -> None:
    policy = RemoteCommandPolicy(mode="allowlist", allow=["ls"])
    with pytest.raises(RouteError, match="Jailbee command is not allowed: git"):
        policy_allows(("git",), policy)


def test_group_help_still_rejects_options_before_the_group_path() -> None:
    with pytest.raises(RouteError, match="unknown Jailbee command"):
        policy_allows(("--verbose", "git"), RemoteCommandPolicy(mode="full"))


# --- C: unknown commands are handed to `python -m jailbee` (Problem C) ---


def test_unknown_command_is_true_for_a_name_that_matches_nothing() -> None:
    assert unknown_command(("nosuchcmd",), RemoteCommandPolicy(mode="full")) is True


def test_unknown_command_is_false_for_a_hidden_internal_name() -> None:
    assert unknown_command(("_remote-console",), RemoteCommandPolicy(mode="full")) is False


def test_unknown_command_is_false_for_a_leading_option() -> None:
    assert unknown_command(("--verbose", "ls"), RemoteCommandPolicy(mode="full")) is False


def test_unknown_command_is_false_when_commands_are_disabled() -> None:
    assert unknown_command(("nosuchcmd",), RemoteCommandPolicy()) is False


def test_one_shot_exec_rejects_an_unknown_command(engine, repo) -> None:
    cfg = RemoteSSHConfig(exec=True, commands=RemoteCommandPolicy(mode="allowlist", allow=["ls"]))
    with pytest.raises(RouteError, match="unknown Jailbee command"):
        route("--repo project nosuchcmd --flag", cfg, engine=engine)


def test_help_lists_only_configured_entrypoints() -> None:
    dashboard_only = help_text(RemoteSSHConfig(shell=False, exec=False))
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


FULL = RemoteCommandPolicy(mode="full")


@pytest.mark.parametrize(
    "argv",
    [
        ("ls", "--config", "/home/user/.config/gh/hosts.yml"),
        ("ls", "-c", "/etc/passwd"),
        ("ls", "-c/etc/passwd"),
        ("ls", "--config=/etc/passwd"),
        ("git", "pull", "box", "--config", "/tmp/evil.yaml"),
        ("pull", "--config", "/tmp/evil.yaml"),  # a hidden alias of `git pull`
    ],
)
def test_a_remote_command_never_takes_a_host_path(argv) -> None:
    """`--config` reads any host file (its parse errors echo the contents) and
    makes any host directory a repo whose config decides host mounts."""
    with pytest.raises(RouteError, match="may not set"):
        policy_allows(argv, FULL)


@pytest.mark.parametrize(
    "argv",
    [
        ("new", "feat", "--mount"),
        ("new", "-m", "feat"),
        ("new", "-bm", "feat"),  # inside a short-option cluster
        ("new", "--name", "--", "--mount", "feat"),  # `--` consumed as a value
    ],
)
def test_a_remote_new_never_mounts_the_host_repo(argv) -> None:
    """The mount is read-write and includes `.git`: a hook planted there runs
    on the host the next time the host's git touches the repo."""
    with pytest.raises(RouteError, match="may not set --mount: new"):
        policy_allows(argv, FULL)


@pytest.mark.parametrize(
    "argv",
    [
        ("ls", "--all"),
        ("new", "feat", "main", "--shell"),
        ("new", "--", "--mount"),  # a branch named "--mount", not the option
        ("exec", "box", "--", "ls", "-c", "x"),  # the container command's own -c
        ("git", "pull", "box", "--ff-only"),
    ],
)
def test_ordinary_remote_arguments_pass(argv) -> None:
    assert policy_allows(argv, FULL)


def test_the_argument_policy_holds_in_allowlist_mode_too() -> None:
    allow = RemoteCommandPolicy(mode="allowlist", allow=["ls", "new"])

    assert policy_allows(("ls",), allow) == "ls"
    with pytest.raises(RouteError, match="may not set --config"):
        policy_allows(("ls", "--config", "/x"), allow)
    with pytest.raises(RouteError, match="may not set --mount"):
        policy_allows(("new", "x", "-m"), allow)


def test_exec_route_refuses_a_host_path_before_resolving_the_repo(engine, repo) -> None:
    cfg = RemoteSSHConfig(exec=True, commands=FULL)

    with pytest.raises(RouteError, match="may not set --config"):
        route("--repo project ls --config /etc/passwd", cfg, engine=engine)


def test_every_path_typed_parameter_is_covered_without_being_listed() -> None:
    """A future `Path` option is refused on day one: the rule is the type, not
    a list someone must remember to extend."""
    from typer._click.types import File
    from typer.models import TyperPath

    from jailbee.remote_ssh.router import _command_tree, _host_reaching_params

    tree = _command_tree()
    for typed, command in tree.leaf_commands.items():
        canonical = tree.aliases.get(typed, typed)
        path_params = {p.name for p in command.params if isinstance(p.type, TyperPath | File)}
        covered = {p.name for p in _host_reaching_params(command, canonical)}
        assert path_params <= covered, typed
    assert "config" in {p.name for p in _host_reaching_params(tree.leaf_commands["ls"], "ls")}


def test_check_arguments_leaves_help_alone() -> None:
    check_arguments(("new", "--help"))


def test_restrict_host_false_lets_host_arguments_through(monkeypatch) -> None:
    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)

    assert policy_allows(("ls", "--config", "/x"), FULL, restrict_host=False) == "ls"
    assert policy_allows(("new", "feat", "--mount"), FULL, restrict_host=False) == "new"


def test_restrict_host_false_is_ignored_inside_a_restricted_session(monkeypatch) -> None:
    """A nested `serve --no-restrict-host` run from a restricted session."""
    monkeypatch.setenv("JAILBEE_REMOTE_SSH", "1")

    with pytest.raises(RouteError, match="may not set --config"):
        policy_allows(("ls", "--config", "/x"), FULL, restrict_host=False)


def test_exec_route_honours_restrict_host_false(engine, repo, monkeypatch) -> None:
    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)
    cfg = RemoteSSHConfig(exec=True, commands=FULL, restrict_host=False)

    assert route("--repo project ls --config /x", cfg, engine=engine).argv == (
        "ls",
        "--config",
        "/x",
    )


# Commands that run container-side (or only read), which a restricted session
# keeps. Together with `_HOST_COMMANDS` this must cover every public leaf: a
# new command fails `test_every_public_command_is_classified` until it is
# put on one side.


def test_every_public_command_is_classified() -> None:
    from jailbee.remote_ssh.router import _CONTAINER_COMMANDS, is_host_command

    host = {path for path in known_command_paths() if is_host_command(path)}
    unclassified = known_command_paths() - host - _CONTAINER_COMMANDS
    assert not unclassified, f"classify for remote SSH: {sorted(unclassified)}"
    assert not host & _CONTAINER_COMMANDS
    assert not _CONTAINER_COMMANDS - known_command_paths(), "stale entries"


def test_policy_refuses_unclassified_commands_unless_host_unrestricted(monkeypatch) -> None:
    from jailbee.remote_ssh import router

    monkeypatch.setattr(router, "_CONTAINER_COMMANDS", router._CONTAINER_COMMANDS - {"ls"})
    with pytest.raises(RouteError, match="not classified"):
        router.policy_allows(["ls"], FULL)
    assert router.policy_allows(["ls"], FULL, restrict_host=False) == "ls"


def test_allowed_paths_uses_policy_and_host_restriction() -> None:
    from jailbee.remote_ssh.router import allowed_command_paths

    policy = RemoteCommandPolicy(mode="allowlist", allow=["git merge", "config edit"])
    assert allowed_command_paths(policy) == frozenset({"git merge"})
    assert allowed_command_paths(policy, restrict_host=False) == frozenset(
        {"git merge", "config edit"}
    )


def test_nested_dashboard_and_tui_are_never_commands() -> None:
    for path in ("dashboard", "tui"):
        with pytest.raises(RouteError, match="reserved"):
            policy_allows([path], FULL, restrict_host=False)


@pytest.mark.parametrize(
    "argv",
    [
        ("config", "edit", "--global"),
        ("remote", "ssh", "key", "add", "-"),
        ("remote", "ssh", "serve", "--no-restrict-host"),
        ("setup",),
        ("apply",),
        ("net", "egress", "add", "box", "10.0.0.1"),
        ("net", "refresh", "--repo", "/home/user"),
        ("egress", "add", "box", "10.0.0.1"),  # hidden alias of `net egress add`
        ("port", "to-container", "box", "5432"),
        ("mount", "aws", "box"),
        ("account", "use", "someone"),
        ("ide", "box"),
        ("apps", "run", "figma", "box"),
    ],
)
def test_a_restricted_session_never_manages_the_host_even_in_full_mode(argv, monkeypatch):
    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)

    with pytest.raises(RouteError, match="manages the host itself"):
        policy_allows(argv, FULL)


def test_host_commands_are_refused_in_allowlist_mode_too() -> None:
    allow = RemoteCommandPolicy(mode="allowlist", allow=["config edit", "ls"])

    assert policy_allows(("ls",), allow) == "ls"
    with pytest.raises(RouteError, match="manages the host itself"):
        policy_allows(("config", "edit"), allow)


def test_restrict_host_false_allows_host_commands(monkeypatch) -> None:
    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)

    assert policy_allows(("config", "edit"), FULL, restrict_host=False) == "config edit"


def test_host_command_group_help_stays_available() -> None:
    """Help changes nothing; refusing it would only hide what is refused."""
    assert policy_allows(("remote", "--help"), FULL) == "remote"


@pytest.mark.parametrize(
    ("argv", "flag"),
    [
        (("pr", "box", "--open"), "--open"),
        (("pr", "box", "--web"), "--web"),
        (("pr", "box", "--yes"), "--yes"),
        (("submodule", "pr", "libs/x", "--web"), "--web"),
        (("submodule", "pr", "libs/x", "-y"), "--yes"),
        (("review", "apply", "box", "--yes"), "--yes"),
        (("issue", "apply", "box", "-y"), "--yes"),
    ],
)
def test_publishing_is_confirmed_and_never_opens_a_host_browser(argv, flag, monkeypatch) -> None:
    """Publishing with the host's GitHub identity stays allowed, but each
    action is confirmed: `--yes` from the remote user is not the review the
    outbox exists for. `--web`/`--open` would start a browser on the host."""
    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)

    with pytest.raises(RouteError, match=f"may not set {flag}"):
        policy_allows(argv, FULL)


@pytest.mark.parametrize(
    "argv",
    [
        ("pr", "box"),
        ("review", "apply", "box"),
        ("issue", "apply", "box"),
        ("pr", "box", "--force"),
    ],
)
def test_publishing_itself_stays_allowed(argv, monkeypatch) -> None:
    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)

    assert policy_allows(argv, FULL)
