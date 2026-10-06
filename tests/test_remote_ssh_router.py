"""Tests for restricted remote SSH command routing."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session

from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig
from jailbee.db.models import RegisteredRepo
from jailbee.remote_ssh import router
from jailbee.remote_ssh.repo_scope import RemoteRepoScope
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
        console=True,
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
        default_entrypoint="console", console=True, commands=RemoteCommandPolicy(mode="full")
    )
    assert route(None, cfg) == Route("console", ("_remote-console",), None, None, True)


@pytest.mark.parametrize("default_entrypoint", ["help", "dashboard", "console"])
def test_explicit_help_always_routes_to_entrypoint_list(default_entrypoint: str) -> None:
    cfg = RemoteSSHConfig(
        default_entrypoint=default_entrypoint,
        console=True,
        commands=RemoteCommandPolicy(mode="full"),
    )
    assert route("help", cfg) == Route("help", (), None, None, False)


def test_explicit_entrypoint_overrides_default() -> None:
    cfg = RemoteSSHConfig(
        default_entrypoint="console", console=True, commands=RemoteCommandPolicy(mode="full")
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


@pytest.mark.parametrize("raw", ["--repo project ls", "shell --repo project"])
def test_excluded_repo_is_indistinguishable_from_unknown(raw: str, engine, repo) -> None:
    cfg = RemoteSSHConfig(
        exec=True,
        console=True,
        excluded_repos=["project"],
        commands=RemoteCommandPolicy(mode="full"),
    )
    with pytest.raises(RouteError) as excluded:
        route(raw, cfg, engine=engine)
    assert str(excluded.value) == "unknown registered repo: project"


@pytest.mark.parametrize(
    "raw",
    [
        "--repo project",
        "--repo /tmp ls",
        "--repo project --repo other ls",
        "ls --repo",
        "ls --repo project --repo other",
    ],
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
    cfg = RemoteSSHConfig(console=True, commands=RemoteCommandPolicy(mode="full"))
    assert route("shell", cfg) == Route("console", ("_remote-console",), None, None, True)
    assert route("shell --repo project", cfg, engine=engine) == Route(
        "console",
        ("_remote-console", "--repo", "project"),
        "project",
        repo,
        True,
    )


@pytest.mark.parametrize("raw", ["console now", "console --repo", "console --repo project extra"])
def test_invalid_console_shapes_are_rejected(raw: str, engine) -> None:
    cfg = RemoteSSHConfig(console=True, commands=RemoteCommandPolicy(mode="full"))
    with pytest.raises(RouteError):
        route(raw, cfg, engine=engine)


def test_disabled_shell_and_exec_entrypoints_are_rejected(engine, repo) -> None:
    cfg = RemoteSSHConfig(console=False, exec=False, commands=RemoteCommandPolicy(mode="full"))
    with pytest.raises(RouteError, match="console is disabled"):
        route("shell", cfg)
    with pytest.raises(RouteError, match="console is disabled"):
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


def test_exclusions_fail_closed_for_unclassified_command_but_allow_scoped_aggregate() -> None:
    policy = RemoteCommandPolicy(mode="full")
    with pytest.raises(RouteError, match="unavailable when SSH repository exclusions"):
        policy_allows(("version",), policy, scope=RemoteRepoScope(frozenset({"secret"})))
    scope = RemoteRepoScope(frozenset({"secret"}))
    assert policy_allows(("ls", "--all"), policy, scope=scope) == "ls"
    assert policy_allows(("base", "usage", "--all"), policy, scope=scope) == "base usage"
    assert (
        policy_allows(
            ("job", "ls", "--all-repos"),
            policy,
            scope=RemoteRepoScope(frozenset({"secret"})),
        )
        == "job ls"
    )


@pytest.mark.parametrize(
    "argv", [("config", "show", "--layer", "global"), ("config", "show", "--layer=global")]
)
def test_global_config_layer_is_denied_with_active_exclusions(argv) -> None:
    with pytest.raises(RouteError, match="config show --layer global is unavailable"):
        policy_allows(
            argv,
            RemoteCommandPolicy(mode="full"),
            scope=RemoteRepoScope(frozenset({"hidden"})),
            allow_scoped_aggregates=True,
        )


def test_repo_config_layer_remains_available_with_active_exclusions() -> None:
    assert (
        policy_allows(
            ("config", "show", "--layer=repo"),
            RemoteCommandPolicy(mode="full"),
            scope=RemoteRepoScope(frozenset({"hidden"})),
            allow_scoped_aggregates=True,
        )
        == "config show"
    )


@pytest.mark.parametrize("argv", [("account", "ls"), ("account", "group", "ls"), ("doctor",)])
def test_host_wide_views_are_denied_with_active_exclusions(argv) -> None:
    with pytest.raises(RouteError, match="unavailable when SSH repository exclusions"):
        policy_allows(
            argv,
            RemoteCommandPolicy(mode="full"),
            scope=RemoteRepoScope(frozenset({"hidden"})),
            allow_scoped_aggregates=True,
        )


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
        ("_state-service",),
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
    dashboard_only = help_text(RemoteSSHConfig(console=False, exec=False))
    assert "  dashboard" in dashboard_only
    assert "  console [--repo PREFIX]" not in dashboard_only
    assert "  COMMAND [ARGS...] [--repo PREFIX]" not in dashboard_only

    all_entrypoints = help_text(
        RemoteSSHConfig(
            console=True,
            exec=True,
            commands=RemoteCommandPolicy(mode="full"),
        )
    )
    assert "  dashboard" in all_entrypoints
    assert "  console [--repo PREFIX]" in all_entrypoints
    assert "  COMMAND [ARGS...] [--repo PREFIX]" in all_entrypoints


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
        ("exec", "-d", "--gui", "box", "--", "firefox"),  # GUI launch, shared display
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

    with pytest.raises(RouteError, match="--config and --repo"):
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

    assert route("ls --config /x", cfg, engine=engine).argv == (
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


@pytest.mark.parametrize("command", ["up", "down", "login", "logout", "logs"])
def test_litellm_host_actions_are_denied_remotely(command: str) -> None:
    with pytest.raises(RouteError, match="manages the host"):
        policy_allows(("litellm", command), RemoteCommandPolicy(mode="full"))


def test_litellm_status_is_allowed_remotely() -> None:
    assert (
        policy_allows(("litellm", "status"), RemoteCommandPolicy(mode="full")) == "litellm status"
    )


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


def test_completion_paths_respect_exclusions_like_command_gate() -> None:
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope
    from jailbee.remote_ssh.router import allowed_command_paths

    paths = allowed_command_paths(
        RemoteCommandPolicy(mode="full"), scope=RemoteRepoScope(frozenset({"secret"}))
    )

    assert "version" not in paths
    assert "ls" in paths
    assert "base usage" in paths


def test_nested_dashboard_and_tui_are_never_commands() -> None:
    for path in ("dashboard", "tui"):
        with pytest.raises(RouteError, match="reserved"):
            policy_allows([path], FULL, restrict_host=False)


def test_local_console_is_never_a_remote_command() -> None:
    """`jb console` runs unrestricted, so no remote policy may let it through."""
    allowlist = RemoteCommandPolicy(mode="allowlist", allow=["console"])
    for policy in (FULL, allowlist):
        with pytest.raises(RouteError, match="reserved"):
            policy_allows(["console"], policy, restrict_host=False)


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


@pytest.mark.parametrize("leaf", ["browse", "ls", "show", "drop", "apply"])
def test_outbox_leaves_follow_policy(leaf):
    argv = ("outbox", leaf)
    if leaf in {"show", "drop", "apply"}:
        argv += ("box", "pr/a.json")
    assert command_path(argv) == f"outbox {leaf}"
    assert policy_allows(argv, FULL) == f"outbox {leaf}"
    assert (
        policy_allows(argv, RemoteCommandPolicy(mode="allowlist", allow=[f"outbox {leaf}"]))
        == f"outbox {leaf}"
    )


@pytest.mark.parametrize(
    "argv", [("outbox",), ("outbox", "feature"), ("outbox", "lss"), ("outbox", "browse", "apply")]
)
def test_outbox_shorthand_is_browse_not_help(argv):
    assert command_path(argv) == "outbox browse"
    assert command_leaf(argv)[0] == "outbox browse"
    assert policy_allows(argv, FULL) == "outbox browse"
    with pytest.raises(RouteError, match="not allowed: outbox browse"):
        policy_allows(argv, RemoteCommandPolicy(mode="allowlist", allow=["outbox apply"]))
    with pytest.raises(RouteError, match="disabled"):
        policy_allows(argv, RemoteCommandPolicy())


@pytest.mark.parametrize(
    "options", [("--yes",), ("-y",), ("-yy",), ("--yes=true",), ("--yes=false",), ("-fy",)]
)
def test_outbox_apply_never_skips_confirmation(options):
    argv = ("outbox", "apply", "box", "pr/a.json", *options)
    for policy in (FULL, RemoteCommandPolicy(mode="allowlist", allow=["outbox apply"])):
        with pytest.raises(RouteError):
            policy_allows(argv, policy)
    with pytest.raises(RouteError):
        check_arguments(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ("outbox", "feature", "-c/x"),
        ("outbox", "browse", "--config=/x"),
        ("outbox", "apply", "box", "pr/a.json", "-yc/x"),
        ("outbox", "--config", "ls", "apply", "box", "pr/a.json", "-y"),
        ("outbox", "--config=/x", "feature"),
        ("outbox", "-c/x"),
    ],
)
def test_outbox_host_config_is_denied(argv):
    with pytest.raises(RouteError):
        policy_allows(argv, FULL)
    with pytest.raises(RouteError):
        check_arguments(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ("outbox",),
        ("outbox", "feature"),
        ("outbox", "ls", "--all-repos"),
        ("outbox", "show", "secret-box", "pr/a.json"),
        ("outbox", "drop", "box", "pr/a.json"),
        ("outbox", "apply", "box", "pr/a.json"),
    ],
)
def test_outbox_exclusions_remain_fail_closed(argv):
    with pytest.raises(RouteError, match="unavailable when SSH repository exclusions"):
        policy_allows(argv, FULL, scope=RemoteRepoScope(frozenset({"secret"})))


def test_outbox_group_help_remains_available():
    policy = RemoteCommandPolicy(mode="allowlist", allow=["outbox apply"])
    assert policy_allows(("outbox", "--help"), policy) == "outbox"
    assert policy_allows(("outbox", "apply", "--help"), policy) == "outbox apply"


@pytest.mark.parametrize("options", [("--yes",), ("-y",), ("-yy",)])
def test_outbox_confirmation_denial_uses_real_parameter_source(options):
    from typer._click.core import ParameterSource

    argv = ("outbox", "apply", "box", "pr/a.json", *options)
    _, command = command_leaf(argv)
    with command.make_context("apply", list(argv[2:])) as ctx:
        assert ctx.params["yes"] is True
        assert ctx.get_parameter_source("yes") is ParameterSource.COMMANDLINE
    with pytest.raises(RouteError, match="may not set --yes: outbox apply"):
        policy_allows(argv, FULL)


def test_outbox_normalized_argument_is_not_an_option():
    assert policy_allows(("outbox", "browse", "--", "--config=/x"), FULL) == "outbox browse"
    assert (
        policy_allows(("outbox", "apply", "box", "pr/a.json", "--dry-run", "--force"), FULL)
        == "outbox apply"
    )


def test_outbox_shorthand_route_preserves_argv(engine, repo, configured_ssh):
    result = route("--repo project outbox feature", configured_ssh, engine=engine)
    assert result.argv == ("outbox", "feature")
    assert result.repo_root == repo


@pytest.mark.parametrize("restricted", [True, False])
@pytest.mark.parametrize("args", [[], ["feature"], ["browse"], ["browse", "apply"]])
def test_ssh_outbox_browser_is_read_only_through_real_cli(
    restricted, args, monkeypatch, mocker, make_cfg, tmp_path
):
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.incus import Incus
    from jailbee.remote_ssh.session import child_environment

    env = child_environment({}, restricted=restricted)
    for key in ("JAILBEE_REMOTE_SSH", "JAILBEE_SSH_SESSION", "JAILBEE_SSH_EXCLUDED_REPOS"):
        monkeypatch.delenv(key, raising=False)
        if key in env:
            monkeypatch.setenv(key, env[key])
    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.config.load_repo_config", return_value=cfg)
    mocker.patch("jailbee.incus.Incus", return_value=mocker.Mock(spec=Incus))
    mocker.patch("jailbee.outbox_io.JournalStore")
    overview = mocker.patch("jailbee.outbox.commands.show_overview", return_value=0)
    drop = mocker.patch("jailbee.outbox.commands.drop_selected")
    publish = mocker.patch("jailbee.outbox.commands.apply_selected")
    policy = RemoteCommandPolicy(mode="allowlist", allow=["outbox browse"])
    assert policy_allows(["outbox", *args], policy, restrict_host=restricted) == "outbox browse"

    result = CliRunner().invoke(app, ["outbox", *args])

    assert result.exit_code == 0, result.output
    assert "read-only" in result.output
    assert "outbox drop" in result.output and "outbox apply" in result.output
    from jailbee.cli_outbox import browser_read_only

    assert browser_read_only() is True
    assert overview.call_count == 1
    assert "confirm" not in overview.call_args.kwargs
    drop.assert_not_called()
    publish.assert_not_called()
    for leaf in ("drop", "apply"):
        with pytest.raises(RouteError, match=f"not allowed: outbox {leaf}"):
            policy_allows(["outbox", leaf, "box", "pr/a.json"], policy)


def test_local_outbox_browser_policy_is_writable(monkeypatch):
    from jailbee.cli_outbox import browser_read_only

    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)
    monkeypatch.delenv("JAILBEE_SSH_SESSION", raising=False)
    assert browser_read_only() is False


@pytest.mark.parametrize("restrict_host", [True, False])
def test_outbox_exclusions_hold_for_unrestricted_ssh_children(
    restrict_host, monkeypatch, engine, repo, mocker
):
    from jailbee.remote_ssh.repo_scope import scope_for_session
    from jailbee.remote_ssh.session import child_environment

    env = child_environment({}, restricted=restrict_host, excluded_repos=["secret"])
    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    scope = scope_for_session()
    assert scope == RemoteRepoScope(frozenset({"secret"}))
    # Config validation forbids this combination; the scope gate must still
    # hold for an inherited snapshot and callers with host restrictions off.
    cfg = RemoteSSHConfig(exec=True, excluded_repos=["secret"], commands=FULL).model_copy(
        update={"restrict_host": restrict_host}
    )
    resolve = mocker.patch("jailbee.remote_ssh.router.resolve_repo")
    for args in (
        "outbox",
        "outbox secret-box",
        "outbox ls --all-repos",
        "outbox show secret-box pr/a.json",
        "outbox drop box pr/a.json",
        "outbox apply box pr/a.json",
    ):
        with pytest.raises(RouteError, match="unavailable when SSH repository exclusions"):
            route(f"--repo project {args}", cfg, engine=engine)
        with pytest.raises(RouteError, match="unavailable when SSH repository exclusions"):
            policy_allows(args.split(), FULL, restrict_host=restrict_host, scope=scope)
    resolve.assert_not_called()


@pytest.mark.parametrize("leaf", ["drop", "apply"])
def test_explicit_outbox_mutation_remains_allowed_without_browser_privileges(leaf):
    argv = ["outbox", leaf, "box", "pr/a.json"]
    policy = RemoteCommandPolicy(mode="allowlist", allow=[f"outbox {leaf}"])
    assert policy_allows(argv, policy) == f"outbox {leaf}"
    _, command = command_leaf(argv)
    with command.make_context(leaf, argv[2:]) as ctx:
        assert ctx.params["yes"] is False
    if leaf == "apply":
        with pytest.raises(RouteError, match="may not set --yes"):
            policy_allows([*argv, "-y"], policy)
    else:
        assert policy_allows([*argv, "-y"], policy) == "outbox drop"


def test_display_up_and_down_manage_the_host_and_status_does_not():
    from jailbee.remote_ssh.router import is_host_command

    assert is_host_command("display up") is True
    assert is_host_command("display down") is True
    assert is_host_command("display attach") is True
    assert is_host_command("display status") is False


GUI_APPS = ["ide", "chrome", "firefox", "browser"]


@pytest.mark.parametrize("path", GUI_APPS)
def test_gui_apps_are_host_commands_unless_the_feature_is_on(path: str) -> None:
    from jailbee.remote_ssh.router import RemoteUnlocks

    policy = RemoteCommandPolicy(mode="full")

    with pytest.raises(RouteError, match="manages the host"):
        policy_allows([path], policy, restrict_host=True)
    assert (
        policy_allows([path], policy, restrict_host=True, unlocks=RemoteUnlocks(gui=True)) == path
    )


def test_apps_run_follows_the_same_rule() -> None:
    from jailbee.remote_ssh.router import RemoteUnlocks

    policy = RemoteCommandPolicy(mode="full")

    with pytest.raises(RouteError, match="manages the host"):
        policy_allows(["apps", "run", "x"], policy, restrict_host=True)
    assert (
        policy_allows(
            ["apps", "run", "x"], policy, restrict_host=True, unlocks=RemoteUnlocks(gui=True)
        )
        == "apps run"
    )


def test_the_qt_dashboard_launcher_stays_a_host_command_even_with_gui_on() -> None:
    from jailbee.remote_ssh.router import RemoteUnlocks

    policy = RemoteCommandPolicy(mode="full")

    with pytest.raises(RouteError, match="manages the host"):
        policy_allows(["gui"], policy, restrict_host=True, unlocks=RemoteUnlocks(gui=True))


def test_gui_apps_still_respect_an_allowlist() -> None:
    from jailbee.remote_ssh.router import RemoteUnlocks

    policy = RemoteCommandPolicy(mode="allowlist", allow=["ls"])

    with pytest.raises(RouteError, match="not allowed"):
        policy_allows(["chrome"], policy, restrict_host=True, unlocks=RemoteUnlocks(gui=True))


def test_every_leaf_is_classified_with_gui_on_too() -> None:
    from jailbee.remote_ssh import router

    gui_paths = {
        p
        for p in known_command_paths()
        if not router.is_host_command(p, unlocks=router.RemoteUnlocks(gui=True))
    }
    assert {"ide", "chrome", "firefox", "browser", "apps run"} <= gui_paths
    assert gui_paths <= (router._CONTAINER_COMMANDS | router._GUI_APP_COMMANDS)


def test_route_threads_the_gui_flag_to_the_command_policy(repo, engine: Engine) -> None:
    off = RemoteSSHConfig(exec=True, commands=RemoteCommandPolicy(mode="full"))
    on = off.model_copy(update={"gui": True})

    with pytest.raises(RouteError, match="manages the host"):
        route("--repo project chrome feat", off, engine=engine)
    assert route("--repo project chrome feat", on, engine=engine).kind == "command"


def test_remote_unlocks_default_unlocks_nothing() -> None:
    from jailbee.remote_ssh.router import RemoteUnlocks

    assert RemoteUnlocks().commands() == frozenset()
    assert RemoteUnlocks.of(None) == RemoteUnlocks()


def test_remote_unlocks_gui_unlocks_the_app_launchers_only() -> None:
    from jailbee.remote_ssh.router import RemoteUnlocks

    unlocks = RemoteUnlocks.of(RemoteSSHConfig(gui=True))
    assert unlocks.commands() == frozenset({"ide", "chrome", "firefox", "browser", "apps run"})
    assert "gui" not in unlocks.commands()


@pytest.mark.parametrize(
    "argv",
    [
        ("net", "loose", "box"),
        ("net", "egress", "add", "pypi.org", "box"),
        ("egress", "add", "pypi.org", "box"),  # hidden alias
    ],
)
def test_network_widening_is_a_host_command_unless_the_feature_is_on(argv, monkeypatch) -> None:
    from jailbee.remote_ssh.router import RemoteUnlocks

    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)
    with pytest.raises(RouteError, match="manages the host itself"):
        policy_allows(argv, FULL)
    assert policy_allows(argv, FULL, unlocks=RemoteUnlocks(network=True)) in {
        "net loose",
        "net egress add",
    }


@pytest.mark.parametrize(
    "argv",
    [
        ("net", "strict", "box"),
        ("net", "egress", "rm", "pypi.org", "box"),
        ("egress", "rm", "pypi.org", "box"),  # hidden alias
    ],
)
def test_network_narrowing_is_always_allowed(argv, monkeypatch) -> None:
    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)
    assert policy_allows(argv, FULL) in {"net strict", "net egress rm"}


@pytest.mark.parametrize(
    "argv",
    [
        ("net", "egress", "add", "pypi.org", "--repo"),
        ("net", "egress", "add", "--repo", "pypi.org"),
        ("egress", "add", "pypi.org", "--repo"),
        ("egress", "rm", "pypi.org", "--repo"),
        ("net", "egress", "rm", "pypi.org", "--repo"),
        ("net", "egress", "rm", "--repo", "pypi.org"),
    ],
)
def test_repo_scope_egress_is_refused_even_with_network_on(argv, monkeypatch) -> None:
    from jailbee.remote_ssh.router import RemoteUnlocks

    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)
    with pytest.raises(RouteError, match="may not set --repo"):
        policy_allows(argv, FULL, unlocks=RemoteUnlocks(network=True))


def test_network_widening_still_respects_an_allowlist(monkeypatch) -> None:
    from jailbee.remote_ssh.router import RemoteUnlocks

    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)
    allow = RemoteCommandPolicy(mode="allowlist", allow=["net strict"])
    with pytest.raises(RouteError, match="not allowed"):
        policy_allows(("net", "loose", "box"), allow, unlocks=RemoteUnlocks(network=True))


def test_unrestricted_sessions_keep_every_network_command(monkeypatch) -> None:
    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)
    assert policy_allows(("net", "loose", "box"), FULL, restrict_host=False) == "net loose"
    assert (
        policy_allows(("net", "egress", "add", "x.org", "--repo"), FULL, restrict_host=False)
        == "net egress add"
    )


def test_allowed_command_paths_follow_the_network_switch() -> None:
    from jailbee.remote_ssh.router import RemoteUnlocks, allowed_command_paths

    off = allowed_command_paths(FULL)
    on = allowed_command_paths(FULL, unlocks=RemoteUnlocks(network=True))
    assert {"net loose", "net egress add"}.isdisjoint(off)
    assert {"net strict", "net egress rm"} <= off
    assert {"net loose", "net egress add", "net strict", "net egress rm"} <= on


def test_remote_unlocks_network_unlocks_widening_only() -> None:
    from jailbee.remote_ssh.router import RemoteUnlocks

    assert RemoteUnlocks.of(RemoteSSHConfig(network=True)).commands() == frozenset(
        {"net loose", "net egress add"}
    )
    both = RemoteUnlocks(gui=True, network=True).commands()
    assert {"chrome", "net loose"} <= both


def test_route_threads_the_network_flag_to_the_command_policy(repo, engine: Engine) -> None:
    off = RemoteSSHConfig(exec=True, commands=RemoteCommandPolicy(mode="full"))
    on = off.model_copy(update={"network": True})

    with pytest.raises(RouteError, match="manages the host"):
        route("--repo project net loose feat", off, engine=engine)
    assert route("--repo project net loose feat", on, engine=engine).kind == "command"


def test_every_leaf_is_classified_with_network_on_too() -> None:
    from jailbee.remote_ssh import router

    unlocks = router.RemoteUnlocks(network=True)
    paths = {p for p in known_command_paths() if not router.is_host_command(p, unlocks=unlocks)}
    assert {"net loose", "net egress add"} <= paths
    assert paths <= (router._CONTAINER_COMMANDS | router._NETWORK_WIDENING_COMMANDS)


_NEW_LOOSE = [
    ("new", "feat", "--net", "loose"),
    ("new", "--net", "loose", "feat"),  # leading position
    ("new", "feat", "--net=loose"),
    ("new", "--net=loose", "feat"),
    ("new", "feat", "--net", "Loose"),
    ("new", "feat", "--net=LOOSE"),
    ("new", "feat", "--net", " loose "),
]


@pytest.mark.parametrize("argv", _NEW_LOOSE)
def test_new_net_loose_needs_the_network_switch(argv, monkeypatch) -> None:
    from jailbee.remote_ssh.router import RemoteUnlocks

    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)
    with pytest.raises(RouteError, match=r"--net=loose unless remote\.ssh\.network is on: new"):
        policy_allows(argv, FULL)
    with pytest.raises(RouteError, match=r"remote\.ssh\.network"):
        policy_allows(argv, FULL, unlocks=RemoteUnlocks(gui=True))
    assert policy_allows(argv, FULL, unlocks=RemoteUnlocks(network=True)) == "new"


@pytest.mark.parametrize("argv", _NEW_LOOSE)
def test_new_net_loose_is_refused_in_allowlist_mode_too(argv, monkeypatch) -> None:
    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)
    allow = RemoteCommandPolicy(mode="allowlist", allow=["new"])
    with pytest.raises(RouteError, match=r"remote\.ssh\.network"):
        policy_allows(argv, allow)


@pytest.mark.parametrize(
    "argv",
    [
        ("new", "feat"),
        ("new", "feat", "--net", "strict"),
        ("new", "feat", "--net=strict"),
        ("new", "feat", "--net", "loosest"),
        ("new", "--", "--net", "loose"),  # branch names, not the option
    ],
)
def test_new_without_a_widening_net_value_is_allowed(argv, monkeypatch) -> None:
    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)
    assert policy_allows(argv, FULL) == "new"


def test_new_net_loose_is_unchanged_when_the_host_is_unrestricted(monkeypatch) -> None:
    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)
    argv = ("new", "feat", "--net", "loose")
    assert policy_allows(argv, FULL, restrict_host=False) == "new"


def test_dashboard_new_net_loose_follows_the_network_switch(monkeypatch) -> None:
    from jailbee.dashboard_commands import permitted

    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)
    off = RemoteSSHConfig(commands=FULL)
    on = off.model_copy(update={"network": True})
    argv = ["new", "feat", "--net", "loose"]
    assert not permitted(argv, off, over_ssh=True)
    assert permitted(argv, on, over_ssh=True)
    assert permitted(["new", "feat", "--net", "strict"], off, over_ssh=True)
    assert permitted(argv, off, over_ssh=False)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("chrome", "c"), True),
        (("apps", "run", "foot", "c"), True),
        (("ide", "c"), True),
        (("gui",), False),
        (("ls",), False),
        (("nonsense",), False),
    ],
)
def test_is_gui_app_command(argv, expected):
    assert router.is_gui_app_command(argv) is expected


def test_console_is_a_host_command() -> None:
    from jailbee.remote_ssh.router import is_host_command

    assert is_host_command("console")


@pytest.mark.parametrize("word", ["console", "shell"])
def test_console_entry_point_accepts_both_names(word: str, engine) -> None:
    cfg = RemoteSSHConfig(console=True)
    assert route(word, cfg, engine=engine).kind == "console"


def test_help_text_names_console() -> None:
    assert "  console [--repo PREFIX]" in help_text(RemoteSSHConfig())
    assert "  shell\n" not in help_text(RemoteSSHConfig()).split("\n\n")[0] + "\n"


def test_trailing_repo_routes_like_leading_form(engine, repo, configured_ssh):
    expected = Route("command", ("ls", "--all"), "project", repo, False)
    assert route("--repo project ls --all", configured_ssh, engine=engine) == expected
    assert route("ls --all --repo project", configured_ssh, engine=engine) == expected


def test_missing_repo_asks_child_to_pick(configured_ssh, engine):
    cfg = configured_ssh.model_copy(update={"gui": True})
    assert route("chrome feat", cfg, engine=engine) == Route(
        "command", ("chrome", "feat"), None, None, False, pick_repo=True
    )


@pytest.mark.parametrize("raw", ["git pull --help", "git --help", "git", "--help", "-h"])
def test_help_requests_never_pick_repo(raw, configured_ssh, engine):
    result = route(raw, configured_ssh, engine=engine)
    assert result.kind == "command"
    assert result.pick_repo is False


@pytest.mark.parametrize(
    "raw",
    [
        "--pick-repo ls",
        "ls --pick-repo",
        "--repo project --pick-repo ls",
        "--pick-repo=true ls",
        "ls --pick-repo=false",
        "git --pick-repo= ls",
        "--repo project ls --pick-repo=true --help",
    ],
)
def test_pick_repo_transport_cannot_be_forged(raw, configured_ssh, engine, repo):
    with pytest.raises(RouteError, match="--pick-repo is internal"):
        route(raw, configured_ssh, engine=engine)


def test_pick_repo_after_separator_is_opaque(configured_ssh, engine, repo):
    result = route(
        "exec feat --repo project -- --pick-repo=true --repo other --help",
        configured_ssh,
        engine=engine,
    )
    assert result == Route(
        "command",
        ("exec", "feat", "--", "--pick-repo=true", "--repo", "other", "--help"),
        "project",
        repo,
        False,
    )
    assert route("exec feat -- --help", configured_ssh, engine=engine).pick_repo is True


def test_trailing_excluded_repo_is_indistinguishable_from_unknown(engine, repo):
    cfg = RemoteSSHConfig(
        exec=True, excluded_repos=["project"], commands=RemoteCommandPolicy(mode="full")
    )
    with pytest.raises(RouteError) as excluded:
        route("ls --repo project", cfg, engine=engine)
    assert str(excluded.value) == "unknown registered repo: project"


def test_leaf_owned_repo_still_meets_check_arguments(engine, repo):
    cfg = RemoteSSHConfig(exec=True, network=True, commands=RemoteCommandPolicy(mode="full"))
    with pytest.raises(RouteError, match="may not set --repo"):
        route("net egress add example.com --repo", cfg, engine=engine)
    assert route("net --repo project egress add example.com", cfg, engine=engine).argv == (
        "net",
        "egress",
        "add",
        "example.com",
    )


def test_legacy_shell_shapes_only_are_console(engine, repo, configured_ssh):
    assert route("shell", configured_ssh).kind == "console"
    assert route("shell --repo project", configured_ssh, engine=engine).kind == "console"
    assert route("shell feat-1 --repo project", configured_ssh, engine=engine) == Route(
        "command", ("shell", "feat-1"), "project", repo, False
    )
    assert route("--repo project shell", configured_ssh, engine=engine).kind == "command"


def test_repos_lists_scoped_repositories(engine, repo, tmp_path):
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    with Session(engine) as session:
        session.add(
            RegisteredRepo(
                container_prefix="hidden",
                repo_root=str(hidden),
                registered_at=datetime(2026, 10, 5, tzinfo=UTC),
            )
        )
        session.commit()
    cfg = RemoteSSHConfig(
        exec=True, excluded_repos=["hidden"], commands=RemoteCommandPolicy(mode="full")
    )
    assert route("repos", cfg, engine=engine) == Route("repos", (), None, None, False)
    assert router.repos_text(cfg, engine=engine) == f"project\t{repo}\n"


def test_repos_needs_exec():
    cfg = RemoteSSHConfig(exec=False, console=True, commands=RemoteCommandPolicy(mode="full"))
    with pytest.raises(RouteError, match="execution is disabled"):
        route("repos", cfg)


def test_help_lists_session_commands():
    cfg = RemoteSSHConfig(
        exec=True, commands=RemoteCommandPolicy(mode="allowlist", allow=["ls", "git pull"])
    )
    entry, commands = help_text(cfg).split("\n\n")
    assert entry.splitlines() == [
        "Available remote commands:",
        "  help",
        "  dashboard",
        "  console [--repo PREFIX]",
        "  repos",
        "  COMMAND [ARGS...] [--repo PREFIX]",
    ]
    lines = commands.splitlines()
    assert len(lines) == 3
    assert lines[0] == "Commands this session may run:"
    assert lines[1].startswith("  git pull  ")
    assert lines[2].startswith("  ls")


POLICIES = [
    RemoteSSHConfig(exec=True, commands=RemoteCommandPolicy(mode="full")),
    RemoteSSHConfig(
        exec=True, commands=RemoteCommandPolicy(mode="allowlist", allow=["ls", "git pull"])
    ),
    RemoteSSHConfig(exec=True, restrict_host=False, commands=RemoteCommandPolicy(mode="full")),
    RemoteSSHConfig(exec=True, excluded_repos=["other"], commands=RemoteCommandPolicy(mode="full")),
    RemoteSSHConfig(exec=True, gui=True, network=True, commands=RemoteCommandPolicy(mode="full")),
]


@pytest.mark.parametrize("cfg", POLICIES)
def test_one_shot_decides_every_command_like_console(cfg, engine, repo):
    scope = RemoteRepoScope(frozenset(cfg.excluded_repos))
    paths = known_command_paths()
    assert "ls" in paths and "git pull" in paths and "shell" in paths
    checked = set()
    for path in sorted(paths):
        if path.split()[0] in {"help", "repos", "dashboard", "console", "shell"}:
            continue
        argv = path.split()
        try:
            policy_allows(
                argv,
                cfg.commands,
                restrict_host=cfg.restrict_host,
                scope=scope,
                allow_scoped_aggregates=True,
                unlocks=router.RemoteUnlocks.of(cfg),
            )
            expected = None
        except RouteError as error:
            expected = str(error)
        raw = (
            f"--repo project {path}"
            if router.leaf_owns_option(path, "--repo")
            else f"{path} --repo project"
        )
        try:
            result = route(raw, cfg, engine=engine)
            assert result.argv == tuple(argv), path
            actual = None
        except RouteError as error:
            actual = str(error)
        assert actual == expected, path
        checked.add(path)
    assert checked == paths - {
        p for p in paths if p.split()[0] in {"help", "repos", "dashboard", "console", "shell"}
    }
    try:
        policy_allows(
            ["shell", "feat"],
            cfg.commands,
            restrict_host=cfg.restrict_host,
            scope=scope,
            allow_scoped_aggregates=True,
            unlocks=router.RemoteUnlocks.of(cfg),
        )
        expected = None
    except RouteError as error:
        expected = str(error)
    try:
        result = route("shell feat --repo project", cfg, engine=engine)
        assert result == Route("command", ("shell", "feat"), "project", repo, False)
        actual = None
    except RouteError as error:
        actual = str(error)
    assert actual == expected, "shell feat"


@pytest.mark.parametrize(
    "option",
    ["-c /tmp/beta.yaml", "--config=/tmp/beta.yaml", "-c/tmp/beta.yaml", "-c=/tmp/beta.yaml"],
)
@pytest.mark.parametrize("restrict", [False, True])
def test_route_config_conflict_is_rejected_before_child(option, restrict, engine, repo):
    cfg = RemoteSSHConfig(
        exec=True, restrict_host=restrict, commands=RemoteCommandPolicy(mode="full")
    )
    with pytest.raises(RouteError, match="--config and --repo"):
        route(f"ls {option} --repo project", cfg, engine=engine)


@pytest.mark.parametrize("argv", ["outbox feat", "outbox", "git --help", "--help"])
def test_help_and_implicit_outbox_selector_parity(argv, configured_ssh, engine, repo):
    router.policy_allows(
        argv.split(), configured_ssh.commands, restrict_host=configured_ssh.restrict_host
    )
    result = route(f"{argv} --repo project", configured_ssh, engine=engine)
    assert result.argv == tuple(argv.split())
    assert result.repo_root == repo
    assert result.repo_prefix == "project"


def test_route_config_in_payload_is_opaque(configured_ssh, engine, repo):
    result = route(
        "exec feat --repo project -- tool --config=/tmp/beta.yaml", configured_ssh, engine=engine
    )
    assert result.argv == ("exec", "feat", "--", "tool", "--config=/tmp/beta.yaml")


@pytest.mark.parametrize("restrict", [False, True])
def test_route_clustered_config_is_rejected_before_child(restrict, engine, repo):
    cfg = RemoteSSHConfig(
        exec=True, restrict_host=restrict, commands=RemoteCommandPolicy(mode="full")
    )
    with pytest.raises(RouteError, match="--config and --repo"):
        route("pr feat -yc/tmp/beta.yaml --repo project", cfg, engine=engine)


@pytest.mark.parametrize("restrict", [False, True])
@pytest.mark.parametrize("value", ["-changes", "--config=/tmp/beta.yaml"])
def test_route_config_looking_title_is_not_a_config(restrict, value, engine, repo):
    cfg = RemoteSSHConfig(
        exec=True, restrict_host=restrict, commands=RemoteCommandPolicy(mode="full")
    )
    result = route(f"pr feat --title '{value}' --repo project", cfg, engine=engine)
    assert result.argv == ("pr", "feat", "--title", value)
    assert result.repo_root == repo
    assert result.repo_prefix == "project"
