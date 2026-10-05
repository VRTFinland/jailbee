"""The remote-session marker every SSH child carries, and its reader."""

from __future__ import annotations

from jailbee.remote_ssh.session import (
    REMOTE_SESSION_ENV,
    SSH_GUI_ENV,
    SSH_SESSION_ENV,
    WAYPIPE_ATTACH_ENV,
    WAYPIPE_COMPRESS_ENV,
    WAYPIPE_SESSION_ENV,
    WaypipeSession,
    child_environment,
    host_restricted,
    is_remote_session,
    is_ssh_session,
    waypipe_attach,
    waypipe_session,
)


def test_child_environment_marks_the_session_and_disarms_less() -> None:
    env = child_environment({"PATH": "/bin"}, term="xterm")

    assert env == {
        "PATH": "/bin",
        SSH_SESSION_ENV: "1",
        REMOTE_SESSION_ENV: "1",
        "LESSSECURE": "1",
        "TERM": "xterm",
        "JAILBEE_SSH_EXCLUDED_REPOS": "[]",
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


def test_an_unrestricted_child_carries_only_the_ssh_session_marker() -> None:
    env = child_environment({"PATH": "/bin"}, term="xterm", restricted=False)

    assert env == {
        "PATH": "/bin",
        "TERM": "xterm",
        SSH_SESSION_ENV: "1",
        "JAILBEE_SSH_EXCLUDED_REPOS": "[]",
    }


def test_is_ssh_session_covers_restricted_and_unrestricted_sessions() -> None:
    assert is_ssh_session({SSH_SESSION_ENV: "1"}) is True
    assert is_ssh_session({REMOTE_SESSION_ENV: "1"}) is True
    assert is_ssh_session({}) is False
    assert is_remote_session({SSH_SESSION_ENV: "1"}) is False


def test_an_unrestricted_child_keeps_a_marker_its_server_already_carries() -> None:
    """A server started inside a restricted session cannot unmark its children."""
    env = child_environment({REMOTE_SESSION_ENV: "1"}, restricted=False)

    assert env[REMOTE_SESSION_ENV] == "1"


def test_host_restricted_follows_the_setting_outside_a_remote_session() -> None:
    assert host_restricted(True, {}) is True
    assert host_restricted(False, {}) is False


def test_host_restricted_cannot_be_lifted_inside_a_restricted_session() -> None:
    assert host_restricted(False, {REMOTE_SESSION_ENV: "1"}) is True


_WP = WaypipeSession(id="0a1b2c3d", compress="zstd=5")


def test_a_waypipe_child_carries_the_session_and_its_compression() -> None:
    env = child_environment({"PATH": "/bin"}, gui_port=2222, waypipe=_WP)

    assert env[WAYPIPE_SESSION_ENV] == "0a1b2c3d"
    assert env[WAYPIPE_COMPRESS_ENV] == "zstd=5"
    assert WAYPIPE_ATTACH_ENV not in env
    assert waypipe_session(env) == _WP


def test_inherited_waypipe_markers_never_survive_into_a_plain_child() -> None:
    """A server started from inside a waypipe session must not mark its own children."""
    base = {WAYPIPE_SESSION_ENV: "deadbeef", WAYPIPE_COMPRESS_ENV: "lz4", WAYPIPE_ATTACH_ENV: "1"}

    env = child_environment(base, gui_port=2222)

    assert WAYPIPE_SESSION_ENV not in env
    assert WAYPIPE_COMPRESS_ENV not in env
    assert WAYPIPE_ATTACH_ENV not in env


def test_attach_is_set_only_when_asked() -> None:
    env = child_environment({}, gui_port=2222, waypipe=_WP, waypipe_attach=True)

    assert env[WAYPIPE_ATTACH_ENV] == "1"
    assert waypipe_attach(env) is True
    assert waypipe_attach(child_environment({}, gui_port=2222, waypipe=_WP)) is False


def test_waypipe_session_needs_the_gui_marker_and_a_well_formed_id() -> None:
    env = child_environment({}, gui_port=2222, waypipe=_WP)

    assert waypipe_session({k: v for k, v in env.items() if k != SSH_GUI_ENV}) is None
    assert waypipe_session({**env, WAYPIPE_SESSION_ENV: "../../etc"}) is None
    assert waypipe_session({**env, WAYPIPE_COMPRESS_ENV: ""}) is None
    assert waypipe_session({}) is None


def test_attach_without_a_waypipe_session_is_false() -> None:
    assert waypipe_attach({WAYPIPE_ATTACH_ENV: "1"}) is False
