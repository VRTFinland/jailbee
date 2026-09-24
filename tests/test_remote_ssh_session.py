"""The remote-session marker every SSH child carries, and its reader."""

from __future__ import annotations

from jailbee.remote_ssh.session import (
    REMOTE_SESSION_ENV,
    child_environment,
    host_restricted,
    is_remote_session,
)


def test_child_environment_marks_the_session_and_disarms_less() -> None:
    env = child_environment({"PATH": "/bin"}, term="xterm")

    assert env == {
        "PATH": "/bin",
        REMOTE_SESSION_ENV: "1",
        "LESSSECURE": "1",
        "TERM": "xterm",
    }


def test_child_environment_overrides_an_inherited_lesssecure_and_leaves_base_alone() -> None:
    base = {"LESSSECURE": "0", "TERM": "old"}

    env = child_environment(base)

    assert env["LESSSECURE"] == "1"
    assert env["TERM"] == "old"
    assert base == {"LESSSECURE": "0", "TERM": "old"}


def test_is_remote_session_reads_the_marker() -> None:
    assert is_remote_session({REMOTE_SESSION_ENV: "1"}) is True
    assert is_remote_session({}) is False
    assert is_remote_session({REMOTE_SESSION_ENV: ""}) is False


def test_is_remote_session_fails_closed_on_any_value() -> None:
    assert is_remote_session({REMOTE_SESSION_ENV: "0"}) is True


def test_is_remote_session_defaults_to_the_process_environment(monkeypatch) -> None:
    monkeypatch.setenv(REMOTE_SESSION_ENV, "1")
    assert is_remote_session() is True
    monkeypatch.delenv(REMOTE_SESSION_ENV)
    assert is_remote_session() is False


def test_an_unrestricted_child_gets_its_base_environment_unchanged() -> None:
    env = child_environment({"PATH": "/bin"}, term="xterm", restricted=False)

    assert env == {"PATH": "/bin", "TERM": "xterm"}


def test_an_unrestricted_child_keeps_a_marker_its_server_already_carries() -> None:
    """A server started inside a restricted session cannot unmark its children."""
    env = child_environment({REMOTE_SESSION_ENV: "1"}, restricted=False)

    assert env[REMOTE_SESSION_ENV] == "1"


def test_host_restricted_follows_the_setting_outside_a_remote_session() -> None:
    assert host_restricted(True, {}) is True
    assert host_restricted(False, {}) is False


def test_host_restricted_cannot_be_lifted_inside_a_restricted_session() -> None:
    assert host_restricted(False, {REMOTE_SESSION_ENV: "1"}) is True
