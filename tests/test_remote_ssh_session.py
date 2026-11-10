"""The remote-session marker every SSH child carries, and its reader."""

from __future__ import annotations

from jailbee.remote_ssh.session import (
    REMOTE_EXCLUDED_REPOS_ENV,
    REMOTE_GUI_PORT_ENV,
    REMOTE_RESTRICTED_ENV,
    REMOTE_SESSION_ENV,
    REMOTE_TRANSPORT_ENV,
    TRANSPORT_SSH,
    TRANSPORT_WEB,
    WAYPIPE_ATTACH_ENV,
    WAYPIPE_COMPRESS_ENV,
    WAYPIPE_SESSION_ENV,
    WaypipeSession,
    child_environment,
    host_restricted,
    is_remote_session,
    is_restricted_session,
    is_shared_display_session,
    is_ssh_session,
    remote_transport,
    shared_display_port,
    waypipe_attach,
    waypipe_session,
)


def test_child_environment_marks_the_session_and_disarms_less() -> None:
    env = child_environment({"PATH": "/bin"}, transport=TRANSPORT_SSH, term="xterm")

    assert env == {
        "PATH": "/bin",
        REMOTE_SESSION_ENV: "1",
        REMOTE_TRANSPORT_ENV: "ssh",
        REMOTE_RESTRICTED_ENV: "1",
        "LESSSECURE": "1",
        "TERM": "xterm",
        "JAILBEE_REMOTE_EXCLUDED_REPOS": "[]",
    }


def test_child_environment_overrides_an_inherited_lesssecure_and_leaves_base_alone() -> None:
    base = {"LESSSECURE": "0", "TERM": "old"}

    env = child_environment(base, transport=TRANSPORT_SSH)

    assert env["LESSSECURE"] == "1"
    assert env["TERM"] == "old"
    assert base == {"LESSSECURE": "0", "TERM": "old"}


def test_is_remote_session_reads_the_marker() -> None:
    assert is_restricted_session({REMOTE_RESTRICTED_ENV: "1"}) is True
    assert is_restricted_session({}) is False
    assert is_restricted_session({REMOTE_RESTRICTED_ENV: ""}) is False


def test_is_remote_session_fails_closed_on_any_value() -> None:
    assert is_restricted_session({REMOTE_RESTRICTED_ENV: "0"}) is True


def test_is_remote_session_defaults_to_the_process_environment(monkeypatch) -> None:
    monkeypatch.setenv(REMOTE_RESTRICTED_ENV, "1")
    assert is_restricted_session() is True
    monkeypatch.delenv(REMOTE_RESTRICTED_ENV)
    assert is_restricted_session() is False


def test_an_unrestricted_child_carries_only_the_ssh_session_marker() -> None:
    env = child_environment(
        {"PATH": "/bin"}, transport=TRANSPORT_SSH, term="xterm", restricted=False
    )

    assert env == {
        "PATH": "/bin",
        "TERM": "xterm",
        REMOTE_SESSION_ENV: "1",
        REMOTE_TRANSPORT_ENV: "ssh",
        "JAILBEE_REMOTE_EXCLUDED_REPOS": "[]",
    }


def test_is_remote_session_covers_restricted_and_unrestricted_sessions() -> None:
    assert is_remote_session({REMOTE_SESSION_ENV: "1"}) is True
    assert is_remote_session({REMOTE_TRANSPORT_ENV: "ssh"}) is True
    assert is_remote_session({REMOTE_RESTRICTED_ENV: "1"}) is True
    assert is_remote_session({}) is False
    assert is_restricted_session({REMOTE_TRANSPORT_ENV: "ssh"}) is False


def test_is_ssh_session_is_a_transport_name_check() -> None:
    assert is_ssh_session({REMOTE_TRANSPORT_ENV: "ssh"}) is True
    assert is_ssh_session({REMOTE_TRANSPORT_ENV: "web"}) is False
    assert is_ssh_session({REMOTE_SESSION_ENV: "1"}) is False
    assert is_ssh_session({}) is False


def test_remote_transport_reads_the_name() -> None:
    assert remote_transport({REMOTE_TRANSPORT_ENV: "web"}) == "web"
    assert remote_transport({}) is None


def test_a_web_child_is_a_remote_session_without_any_ssh_behaviour() -> None:
    env = child_environment(
        {"PATH": "/bin"},
        transport=TRANSPORT_WEB,
        excluded_repos=("secret",),
        gui_port=2222,
        waypipe=WaypipeSession(id="0a1b2c3d", compress="lz4"),
        waypipe_attach=True,
    )

    assert is_remote_session(env) is True
    assert is_restricted_session(env) is True
    assert remote_transport(env) == "web"
    assert is_ssh_session(env) is False
    assert "LESSSECURE" not in env
    assert REMOTE_GUI_PORT_ENV not in env
    assert shared_display_port(env) is None
    assert is_shared_display_session(env) is False
    assert waypipe_session(env) is None
    assert waypipe_attach(env) is False
    assert not {WAYPIPE_SESSION_ENV, WAYPIPE_COMPRESS_ENV, WAYPIPE_ATTACH_ENV} & env.keys()
    assert env[REMOTE_EXCLUDED_REPOS_ENV] == '["secret"]'


def test_an_unrestricted_web_child_is_not_restricted() -> None:
    env = child_environment({}, transport=TRANSPORT_WEB, restricted=False)

    assert is_remote_session(env) is True
    assert is_restricted_session(env) is False


def test_child_environment_replaces_an_inherited_transport() -> None:
    env = child_environment({REMOTE_TRANSPORT_ENV: "ssh"}, transport=TRANSPORT_WEB)

    assert remote_transport(env) == "web"


def test_an_unrestricted_child_keeps_a_marker_its_server_already_carries() -> None:
    """A server started inside a restricted session cannot unmark its children."""
    env = child_environment({REMOTE_RESTRICTED_ENV: "1"}, transport=TRANSPORT_SSH, restricted=False)

    assert env[REMOTE_RESTRICTED_ENV] == "1"


def test_host_restricted_follows_the_setting_outside_a_remote_session() -> None:
    assert host_restricted(True, {}) is True
    assert host_restricted(False, {}) is False


def test_host_restricted_cannot_be_lifted_inside_a_restricted_session() -> None:
    assert host_restricted(False, {REMOTE_RESTRICTED_ENV: "1"}) is True


_WP = WaypipeSession(id="0a1b2c3d", compress="zstd=5")


def test_a_waypipe_child_carries_the_session_and_its_compression() -> None:
    env = child_environment({"PATH": "/bin"}, transport=TRANSPORT_SSH, gui_port=2222, waypipe=_WP)

    assert env[WAYPIPE_SESSION_ENV] == "0a1b2c3d"
    assert env[WAYPIPE_COMPRESS_ENV] == "zstd=5"
    assert WAYPIPE_ATTACH_ENV not in env
    assert waypipe_session(env) == _WP


def test_inherited_waypipe_markers_never_survive_into_a_plain_child() -> None:
    """A server started from inside a waypipe session must not mark its own children."""
    base = {WAYPIPE_SESSION_ENV: "deadbeef", WAYPIPE_COMPRESS_ENV: "lz4", WAYPIPE_ATTACH_ENV: "1"}

    env = child_environment(base, transport=TRANSPORT_SSH, gui_port=2222)

    assert WAYPIPE_SESSION_ENV not in env
    assert WAYPIPE_COMPRESS_ENV not in env
    assert WAYPIPE_ATTACH_ENV not in env


def test_attach_is_set_only_when_asked() -> None:
    env = child_environment(
        {}, transport=TRANSPORT_SSH, gui_port=2222, waypipe=_WP, waypipe_attach=True
    )

    assert env[WAYPIPE_ATTACH_ENV] == "1"
    assert waypipe_attach(env) is True
    assert (
        waypipe_attach(child_environment({}, transport=TRANSPORT_SSH, gui_port=2222, waypipe=_WP))
        is False
    )


def test_waypipe_session_needs_the_ssh_marker_and_a_well_formed_id() -> None:
    env = child_environment({}, transport=TRANSPORT_SSH, gui_port=2222, waypipe=_WP)

    assert (
        waypipe_session({k: v for k, v in env.items() if not k.startswith("JAILBEE_REMOTE")})
        is None
    )
    assert waypipe_session({**env, WAYPIPE_SESSION_ENV: "../../etc"}) is None
    assert waypipe_session({**env, WAYPIPE_COMPRESS_ENV: ""}) is None
    assert waypipe_session({}) is None


def test_attach_without_a_waypipe_session_is_false() -> None:
    assert waypipe_attach({WAYPIPE_ATTACH_ENV: "1"}) is False


def test_terminal_identity_keeps_only_valid_allowlisted_names() -> None:
    from jailbee.remote_ssh.session import terminal_identity

    client = {
        "LC_TERMINAL": "iTerm2",
        "LC_TERMINAL_VERSION": "3.5.10",
        "TERM_PROGRAM": "iTerm.app",
        "COLORTERM": "truecolor",
        "LANG": "en_US.UTF-8",
        "LD_PRELOAD": "/tmp/evil.so",
        "TERM_PROGRAM_VERSION": "3.5\n; rm -rf /",
    }

    assert terminal_identity(client) == {
        "LC_TERMINAL": "iTerm2",
        "LC_TERMINAL_VERSION": "3.5.10",
        "TERM_PROGRAM": "iTerm.app",
        "COLORTERM": "truecolor",
    }


def test_terminal_identity_skips_non_text_entries() -> None:
    from jailbee.remote_ssh.session import terminal_identity

    assert terminal_identity({"LC_TERMINAL": b"iTerm2", "COLORTERM": b"\xff"}) == {}


def test_child_environment_carries_the_client_terminal_not_the_servers() -> None:
    # Textual identifies iTerm2 by LC_TERMINAL/TERM_PROGRAM and leaves its
    # buggy pixel-mouse mode off; over SSH only the client can say which
    # terminal is at the other end, and the server's own value is wrong.
    base = {"PATH": "/bin", "LC_TERMINAL": "server-term", "TERM_PROGRAM": "server-app"}

    env = child_environment(base, transport=TRANSPORT_SSH, terminal={"LC_TERMINAL": "iTerm2"})

    assert env["LC_TERMINAL"] == "iTerm2"
    assert "TERM_PROGRAM" not in env
    assert base["LC_TERMINAL"] == "server-term"


def test_child_environment_without_a_client_terminal_drops_the_servers() -> None:
    env = child_environment(
        {"PATH": "/bin", "COLORTERM": "truecolor"}, transport=TRANSPORT_SSH, restricted=False
    )

    assert "COLORTERM" not in env


def test_child_environment_carries_the_client_and_never_inherits_one() -> None:
    from jailbee.remote_ssh.session import REMOTE_CLIENT_ENV, remote_client

    env = child_environment({}, transport=TRANSPORT_SSH, client="SHA256:abc")
    assert env[REMOTE_CLIENT_ENV] == "SHA256:abc"
    assert remote_client(env) == "SHA256:abc"
    stale = child_environment({REMOTE_CLIENT_ENV: "someone-else"}, transport=TRANSPORT_SSH)
    assert REMOTE_CLIENT_ENV not in stale
    assert remote_client(stale) is None
