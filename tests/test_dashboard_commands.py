"""Tests for dashboard command input parsing and completion."""

from __future__ import annotations

import pytest

from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig
from jailbee.dashboard_commands import (
    apply_completion,
    check_dashboard_command,
    command_argv,
    completion_candidates,
    insert_options_before_separator,
    permitted,
)
from jailbee.remote_ssh.router import RemoteUnlocks, RouteError
from jailbee.remote_ssh.session import SSH_EXCLUDED_REPOS_ENV, SSH_SESSION_ENV


def test_completion_respects_default_and_disabled_remote_policy() -> None:
    from jailbee.remote_ssh.router import allowed_command_paths

    full = RemoteSSHConfig()
    disabled = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="disabled"))

    full_paths = allowed_command_paths(full.commands, restrict_host=full.restrict_host)
    disabled_paths = allowed_command_paths(disabled.commands, restrict_host=disabled.restrict_host)

    assert "merge" in completion_candidates("m", (), full_paths)
    assert completion_candidates("m", (), disabled_paths) == ()


@pytest.mark.parametrize(
    ("argv", "policy"),
    [
        (["merge", "alpha"], RemoteSSHConfig(commands=RemoteCommandPolicy(mode="disabled"))),
        (["merge", "alpha"], RemoteSSHConfig(commands=RemoteCommandPolicy(mode="disabled"))),
        (
            ["merge", "alpha"],
            RemoteSSHConfig(
                exec=True,
                commands=RemoteCommandPolicy(mode="allowlist", allow=["git pull"]),
            ),
        ),
        (["config", "edit"], RemoteSSHConfig(exec=True, commands=RemoteCommandPolicy(mode="full"))),
        (["pr", "--yes"], RemoteSSHConfig(exec=True, commands=RemoteCommandPolicy(mode="full"))),
        (
            ["merge", "--config", "/tmp/host.yaml", "alpha"],
            RemoteSSHConfig(exec=True, commands=RemoteCommandPolicy(mode="full")),
        ),
    ],
)
def test_ssh_dashboard_command_refuses_commands_outside_policy(
    argv: list[str], policy: RemoteSSHConfig
) -> None:
    with pytest.raises(RouteError):
        check_dashboard_command(argv, policy, over_ssh=True)


def test_ssh_dashboard_merge_alias_uses_canonical_allowlist() -> None:
    policy = RemoteSSHConfig(
        exec=True, commands=RemoteCommandPolicy(mode="allowlist", allow=["git merge"])
    )
    check_dashboard_command(["merge", "alpha"], policy, over_ssh=True)


@pytest.mark.parametrize("snapshot", [None, "not-json"])
def test_ssh_dashboard_fails_closed_without_valid_scope_snapshot(
    monkeypatch, snapshot: str | None
) -> None:
    monkeypatch.setenv(SSH_SESSION_ENV, "1")
    if snapshot is None:
        monkeypatch.delenv(SSH_EXCLUDED_REPOS_ENV, raising=False)
    else:
        monkeypatch.setenv(SSH_EXCLUDED_REPOS_ENV, snapshot)
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"))

    with pytest.raises(ValueError, match="Invalid SSH repository policy snapshot"):
        check_dashboard_command(["git", "pull"], policy, over_ssh=True)


def test_ssh_dashboard_exclusions_gate_aggregate_option(monkeypatch) -> None:
    monkeypatch.setenv(SSH_SESSION_ENV, "1")
    monkeypatch.setenv(SSH_EXCLUDED_REPOS_ENV, '["secret"]')
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="full"))

    check_dashboard_command(["ls", "--all"], policy, over_ssh=True)
    # Canonical/hidden aliases are classified through the same remote policy.
    check_dashboard_command(["merge", "--into=main"], policy, over_ssh=True)
    # Value-form path options are parsed by Click and rejected by host policy.
    with pytest.raises(RouteError, match="--config"):
        check_dashboard_command(["ls", "--config=/etc/passwd"], policy, over_ssh=True)


def test_ssh_dashboard_commands_policy_is_independent_of_exec_flag() -> None:
    policy = RemoteSSHConfig(
        exec=False,
        commands=RemoteCommandPolicy(mode="allowlist", allow=["git merge"]),
    )
    check_dashboard_command(["merge", "alpha"], policy, over_ssh=True)


def test_ssh_nested_dashboard_cannot_supply_server_policy_transport() -> None:
    policy = RemoteSSHConfig(
        exec=True,
        restrict_host=False,
        commands=RemoteCommandPolicy(mode="full"),
    )
    with pytest.raises(RouteError):
        check_dashboard_command(
            ["dashboard", "--remote-policy-json", '{"exec":true,"commands":{"mode":"full"}}'],
            policy,
            over_ssh=True,
        )


def test_local_dashboard_command_does_not_apply_ssh_policy() -> None:
    check_dashboard_command(["config", "edit"], None, over_ssh=False)


def test_merge_alias_gets_selected_source() -> None:
    assert command_argv("merge --into beta", "alpha") == ["merge", "--into", "beta", "alpha"]


def test_canonical_merge_gets_selected_source() -> None:
    assert command_argv("git merge beta", "alpha") == ["git", "merge", "beta"]


def test_shell_gets_selected_container() -> None:
    assert command_argv("shell", "alpha") == ["shell", "alpha"]


def test_explicit_shell_target_wins() -> None:
    assert command_argv("shell beta", "alpha") == ["shell", "beta"]


def test_new_does_not_get_container_inserted() -> None:
    assert command_argv("new branch", "alpha") == ["new", "branch"]


def test_multiword_command_gets_selected_container() -> None:
    assert command_argv("git diff", "alpha") == ["git", "diff", "alpha"]


def test_quoted_arguments_are_argv_not_shell() -> None:
    assert command_argv("merge --branch 'feat/my branch'", "alpha") == [
        "merge",
        "--branch",
        "feat/my branch",
        "alpha",
    ]


@pytest.mark.parametrize("text", ["", "   ", "merge 'unterminated"])
def test_empty_or_malformed_input_is_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        command_argv(text, "alpha")


def test_completion_includes_commands_options_and_repo_containers() -> None:
    paths = completion_candidates("gi", ("alpha", "beta"))
    assert "git" in paths
    assert "git diff" in completion_candidates("git d", ("alpha", "beta"))
    assert "--into" in completion_candidates("merge --in", ("alpha", "beta"))
    assert "alpha" in completion_candidates("merge al", ("alpha", "beta"))
    assert "elsewhere" not in completion_candidates("merge e", ("alpha", "beta"))


def test_completion_tolerates_unfinished_quote() -> None:
    assert "feature branch" in completion_candidates("shell 'feature", ("feature branch",))


def test_completion_tolerates_unfinished_quote_containing_space() -> None:
    assert "feature branch" in completion_candidates("shell 'feature br", ("feature branch",))


def test_editor_completion_preserves_command_prefix_and_quote() -> None:
    assert apply_completion("merge --in", "--into") == "merge --into"
    assert apply_completion("shell 'feature", "feature branch") == "shell 'feature branch'"
    assert apply_completion("shell 'feature'", "feature branch") == "shell 'feature branch'"


def test_completion_for_repo_header_uses_header_containers_without_default() -> None:
    assert "alpha" in completion_candidates("shell al", ("alpha", "beta"))
    assert command_argv("shell", None) == ["shell"]


def test_restricted_completion_filters_host_and_argument_options() -> None:
    allowed = frozenset({"new", "shell"})
    candidates = completion_candidates("co", ("alpha",), allowed)
    assert "config edit" not in candidates
    assert "config" not in candidates
    assert "--mount" not in completion_candidates("new --m", (), allowed, restrict_host=True)


def test_remote_full_completion_hides_host_commands_and_denied_parameters() -> None:
    paths = completion_candidates(
        "", (), frozenset({"config edit", "new", "shell"}), restrict_host=True
    )
    assert "config" not in paths
    assert "--mount" not in completion_candidates(
        "new --m", (), frozenset({"new"}), restrict_host=True
    )


def test_unknown_local_command_is_not_rejected_by_preflight() -> None:
    assert command_argv("unknown-command --mystery", None) == [
        "unknown-command",
        "--mystery",
    ]
    with pytest.raises(RouteError):
        check_dashboard_command(["unknown-command"], None, over_ssh=True)


def test_local_options_are_inserted_before_exec_separator() -> None:
    assert insert_options_before_separator(
        ["exec", "alpha", "--", "echo", "hi"], ["--config", "/repo/config.yaml"]
    ) == ["exec", "alpha", "--config", "/repo/config.yaml", "--", "echo", "hi"]


def test_remote_completion_maps_alias_only_when_canonical_path_allowed() -> None:
    assert "merge" in completion_candidates("me", (), frozenset({"git merge"}))
    assert "merge" not in completion_candidates("me", (), frozenset({"git pull"}))


def test_remote_completion_hides_options_and_containers_for_disallowed_leaf() -> None:
    allowed = frozenset({"git pull"})
    assert "--into" not in completion_candidates("merge --in", ("alpha",), allowed)
    assert "alpha" not in completion_candidates("merge al", ("alpha",), allowed)


@pytest.mark.parametrize(
    ("argv", "over_ssh", "policy", "expected"),
    [
        (["apply"], False, None, True),
        (["apply"], True, RemoteSSHConfig(), False),
        (["doctor"], True, RemoteSSHConfig(), True),
        (
            ["doctor"],
            True,
            RemoteSSHConfig(commands=RemoteCommandPolicy(mode="allowlist", allow=["shell"])),
            False,
        ),
        (["doctor"], True, None, False),
    ],
    ids=[
        "local",
        "ssh-host-command",
        "ssh-container-command",
        "ssh-allowlist-without",
        "no-policy",
    ],
)
def test_permitted_mirrors_check_dashboard_command(argv, over_ssh, policy, expected) -> None:
    assert permitted(argv, policy, over_ssh=over_ssh) is expected


def test_ssh_dashboard_gui_launchers_follow_the_gui_flag() -> None:
    full = RemoteCommandPolicy(mode="full")
    off = RemoteSSHConfig(exec=True, commands=full)
    on = RemoteSSHConfig(exec=True, commands=full, gui=True)

    with pytest.raises(RouteError, match="manages the host"):
        check_dashboard_command(["chrome", "feat-1", "--force"], off, over_ssh=True)
    check_dashboard_command(["chrome", "feat-1", "--force"], on, over_ssh=True)
    with pytest.raises(RouteError, match="manages the host"):
        check_dashboard_command(["gui"], on, over_ssh=True)


def test_gui_flag_adds_the_launchers_to_the_allowed_and_offered_paths() -> None:
    from jailbee.remote_ssh.router import allowed_command_paths

    full = RemoteCommandPolicy(mode="full")

    assert "chrome" not in allowed_command_paths(full)
    on = allowed_command_paths(full, unlocks=RemoteUnlocks(gui=True))
    assert {"chrome", "ide", "apps run"} <= on
    assert "gui" not in on
    assert "chrome" not in completion_candidates("chr", (), on, restrict_host=True)
    assert "chrome" in completion_candidates(
        "chr", (), on, restrict_host=True, unlocks=RemoteUnlocks(gui=True)
    )
