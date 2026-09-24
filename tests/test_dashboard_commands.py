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
)
from jailbee.remote_ssh.router import RouteError


@pytest.mark.parametrize(
    ("argv", "policy"),
    [
        (["merge", "alpha"], RemoteSSHConfig(exec=False)),
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
    assert "--mount" not in completion_candidates(
        "new --m", (), allowed, restrict_host=True
    )


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
