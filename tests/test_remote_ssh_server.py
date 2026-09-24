"""Authentication, channel policy, dispatch, and listener lifecycle without sockets."""

from __future__ import annotations

import asyncio
import io
import logging
import signal
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncssh
import pytest
from sqlmodel import Session

from jailbee.config import ConfigError
from jailbee.config.models_remote import RemoteCommandPolicy, RemoteConfig, RemoteSSHConfig
from jailbee.db.models import RegisteredRepo
from jailbee.global_config import GlobalConfig, default_global_config_path
from jailbee.remote_ssh import server
from jailbee.remote_ssh.keys import ssh_paths
from jailbee.remote_ssh.pty import ChildSpec, PTYError

PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBsz47IcK4hPdHS7xOXNGafb/Uw3epmEsD7xIJn434n6"
FINGERPRINT = "SHA256:qLBHzrI/tje39Belv8gH7aaz1iprjQMjKh4sbnQnFT4"
SOURCE = ("192.0.2.10", 43123)


@pytest.fixture
def connection():
    extra = {"peername": SOURCE}
    conn = Mock(spec=asyncssh.SSHServerConnection)
    conn.get_extra_info.side_effect = extra.get
    conn.set_extra_info.side_effect = lambda **values: extra.update(values)
    return conn


@pytest.fixture
def ssh_server(connection):
    instance = server.JailbeeSSHServer()
    instance.connection_made(connection)
    return instance


@pytest.fixture
def authorized_file():
    path = ssh_paths().authorized_keys
    path.parent.mkdir(parents=True)
    path.write_text(PUBLIC_KEY + " owner-comment-must-not-be-logged\n")
    return path


@pytest.fixture
def public_key():
    return asyncssh.import_public_key(PUBLIC_KEY)


@pytest.mark.parametrize("username", ["root", "", "Jailbee", "jailbee "])
def test_only_fixed_username_can_authenticate(ssh_server, authorized_file, public_key, username):
    assert ssh_server.begin_auth(username) is True
    assert ssh_server.validate_public_key(username, public_key) is False


def test_known_key_stores_sha256_fingerprint(ssh_server, connection, authorized_file, public_key):
    assert ssh_server.begin_auth("jailbee") is True
    assert ssh_server.public_key_auth_supported() is True
    assert ssh_server.validate_public_key("jailbee", public_key) is True
    assert connection.get_extra_info("jailbee_key_fingerprint") == FINGERPRINT


def test_incoming_key_requests_sha256_explicitly(ssh_server, authorized_file):
    key = Mock(spec=asyncssh.SSHKey)
    key.get_fingerprint.return_value = FINGERPRINT
    ssh_server.begin_auth("jailbee")
    assert ssh_server.validate_public_key("jailbee", key) is True
    key.get_fingerprint.assert_called_once_with("sha256")


def test_unknown_key_is_rejected(ssh_server, connection, authorized_file):
    ssh_server.begin_auth("jailbee")
    key = asyncssh.generate_private_key("ssh-ed25519")
    assert ssh_server.validate_public_key("jailbee", key) is False
    assert connection.get_extra_info("jailbee_key_fingerprint") is None


def test_each_auth_reads_added_and_removed_keys(
    ssh_server, connection, authorized_file, public_key
):
    replacement = asyncssh.generate_private_key("ssh-ed25519")
    ssh_server.begin_auth("jailbee")
    assert ssh_server.validate_public_key("jailbee", public_key) is True
    authorized_file.write_bytes(replacement.export_public_key())
    ssh_server.begin_auth("jailbee")
    assert connection.get_extra_info("jailbee_key_fingerprint") is None
    assert ssh_server.validate_public_key("jailbee", public_key) is False
    assert ssh_server.validate_public_key("jailbee", replacement) is True


def test_missing_key_file_requires_auth_and_rejects_key(ssh_server, public_key):
    assert ssh_server.begin_auth("jailbee") is True
    assert ssh_server.validate_public_key("jailbee", public_key) is False


@pytest.mark.parametrize(
    "entry",
    [
        'command="secret-command" ' + PUBLIC_KEY,
        'environment="SECRET=value" ' + PUBLIC_KEY,
        'from="127.0.0.1" ' + PUBLIC_KEY,
        "no-pty " + PUBLIC_KEY,
        "restrict " + PUBLIC_KEY,
        "cert-authority " + PUBLIC_KEY,
        "@cert-authority " + PUBLIC_KEY,
        "ssh-ed25519 invalid-base64",
        PUBLIC_KEY.replace("ssh-ed25519", "ssh-rsa"),
        PUBLIC_KEY.replace("ssh-ed25519", "ssh-ed25519-cert-v01@openssh.com"),
    ],
)
def test_manual_invalid_entries_fail_closed_after_previous_auth(
    ssh_server, authorized_file, public_key, connection, entry, caplog
):
    ssh_server.begin_auth("jailbee")
    assert ssh_server.validate_public_key("jailbee", public_key) is True
    authorized_file.write_text(entry + "\n")
    with caplog.at_level(logging.INFO):
        assert ssh_server.begin_auth("jailbee") is True
        assert ssh_server.validate_public_key("jailbee", public_key) is False
    assert connection.get_extra_info("jailbee_key_fingerprint") is None
    assert PUBLIC_KEY not in caplog.text
    assert "secret-command" not in caplog.text
    assert "SECRET=value" not in caplog.text


def test_key_read_error_does_not_reuse_previous_authorization(
    ssh_server, authorized_file, public_key, mocker
):
    ssh_server.begin_auth("jailbee")
    mocker.patch.object(server, "read_authorized_keys", side_effect=PermissionError("private path"))
    assert ssh_server.begin_auth("jailbee") is True
    assert ssh_server.validate_public_key("jailbee", public_key) is False


def test_authentication_state_is_per_connection(ssh_server, authorized_file, public_key):
    ssh_server.begin_auth("jailbee")
    other = server.JailbeeSSHServer()
    other.connection_made(Mock(spec=asyncssh.SSHServerConnection))
    authorized_file.write_text("")
    other.begin_auth("jailbee")
    assert ssh_server.validate_public_key("jailbee", public_key) is True
    assert other.validate_public_key("jailbee", public_key) is False


def test_default_password_keyboard_and_host_auth_remain_unavailable(ssh_server):
    # AsyncSSH's keyboard-interactive default is NotImplemented and delegates
    # to password support. Exercise its actual resolution without a socket.
    conn = SimpleNamespace(_owner=ssh_server, _password_auth=True, _kbdint_auth=True)
    assert asyncssh.SSHServerConnection.password_auth_supported(conn) is False
    assert asyncssh.SSHServerConnection.kbdint_auth_supported(conn) is False
    assert ssh_server.host_based_auth_supported() is False


@pytest.mark.parametrize(
    ("callback", "arguments"),
    [
        ("connection_requested", ("target.example", 80, "192.0.2.10", 43123)),
        ("server_requested", ("0.0.0.0", 8080)),
        ("unix_connection_requested", ("/tmp/target.sock",)),
        ("unix_server_requested", ("/tmp/listen.sock",)),
    ],
)
def test_forwarding_is_explicitly_refused_even_if_upstream_default_changes(
    ssh_server, monkeypatch, callback, arguments
):
    monkeypatch.setattr(asyncssh.SSHServer, callback, lambda *args: True)
    assert getattr(ssh_server, callback)(*arguments) is False


@pytest.mark.parametrize("callback", ["tun_requested", "tap_requested"])
def test_tunnel_requests_remain_refused(ssh_server, callback):
    assert getattr(ssh_server, callback)(None) is False


@pytest.mark.parametrize("failure", [None, ConnectionResetError("private disconnect message")])
def test_connection_audit_identifies_source_key_and_safe_disconnect_reason(
    ssh_server, authorized_file, public_key, caplog, failure
):
    with caplog.at_level(logging.INFO):
        ssh_server.begin_auth("jailbee")
        assert ssh_server.validate_public_key("jailbee", public_key) is True
        ssh_server.connection_lost(failure)
    assert "192.0.2.10" in caplog.text
    assert FINGERPRINT in caplog.text
    assert ("closed" if failure is None else "ConnectionResetError") in caplog.text
    assert PUBLIC_KEY not in caplog.text
    assert "owner-comment-must-not-be-logged" not in caplog.text
    assert "private disconnect message" not in caplog.text


def actual_process(command=None, *, term=None, env=None, raw_env=None, subsystem=None):
    """Keep AsyncSSH process/stream APIs real and mock only the transport channel."""
    channel = Mock(spec=asyncssh.SSHServerChannel)
    channel.get_encoding.return_value = (None, "strict")
    channel.get_loop.return_value = asyncio.get_running_loop()
    channel.get_recv_window.return_value = 2097152
    channel.get_read_datatypes.return_value = [None]
    channel.get_write_datatypes.return_value = [None, 1]
    channel.get_terminal_type.return_value = term
    channel.get_terminal_size.return_value = (80, 24, 0, 0)
    channel.get_command.return_value = command
    channel.get_environment.return_value = env or {}
    channel.get_environment_bytes.return_value = raw_env or {}
    channel.get_subsystem.return_value = subsystem
    channel.get_extra_info.side_effect = {
        "peername": SOURCE,
        "jailbee_key_fingerprint": FINGERPRINT,
    }.get
    process = asyncssh.SSHServerProcess(lambda _: None, None, 3, False)
    process.connection_made(channel)
    process._start_process(
        asyncssh.SSHReader(process, channel),
        asyncssh.SSHWriter(process, channel),
        asyncssh.SSHWriter(process, channel, 1),
    )
    return process, channel


def session(command=None, overrides=None, **kwargs):
    async def run():
        process, channel = actual_process(command, **kwargs)
        original_exit, original_signal = process.exit, process.exit_with_signal
        await server.handle_process(process, overrides)
        assert process.exit == original_exit
        assert process.exit_with_signal == original_signal
        return process, channel

    return asyncio.run(run())


def output(channel, datatype=None):
    return b"".join(
        data
        for data, stream in (item.args for item in channel.write.call_args_list)
        if stream == datatype
    )


@pytest.fixture
def child(mocker):
    async def completed(process, spec):
        process.exit(7)

    return mocker.patch.object(server, "run_child", side_effect=completed)


@pytest.fixture
def configured(mocker):
    ssh = RemoteSSHConfig(shell=True, exec=True, commands=RemoteCommandPolicy(mode="full"))
    config = GlobalConfig(remote=RemoteConfig(ssh=ssh))
    return mocker.patch.object(server, "load_global_config", return_value=(config, []))


@pytest.fixture
def repo(tmp_path, db_engine, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    with Session(db_engine) as db:
        db.add(
            RegisteredRepo(
                container_prefix="project",
                repo_root=str(root),
                registered_at=datetime(2026, 9, 18, tzinfo=UTC),
            )
        )
        db.commit()
    monkeypatch.setattr("jailbee.remote_ssh.router.get_engine", lambda: db_engine)
    return root


@pytest.mark.parametrize("command", [None, "", "  "])
def test_missing_command_prints_enabled_binary_help_and_succeeds(command, child):
    _, channel = session(command)
    assert output(channel) == b"Available remote commands:\n  dashboard\n"
    assert output(channel, 1) == b""
    channel.exit.assert_called_once_with(0)
    child.assert_not_awaited()


def test_commandless_help_uses_crlf_when_a_pty_was_negotiated(child):
    """Regression: a raw client terminal has no kernel pty to expand ONLCR.

    `ssh -t ...` (or a plain `ssh ...` login, which OpenSSH auto-requests a
    PTY for) left every line of the server's own help text with no carriage
    return, stacking output diagonally. Only server-written text needs this;
    child process output goes through a real pty in `pty.py` and already
    gets CRLF for free.
    """
    _, channel = session(None, term="xterm")
    assert output(channel) == b"Available remote commands:\r\n  dashboard\r\n"
    channel.exit.assert_called_once_with(0)


def test_commandless_help_stays_bare_lf_without_a_pty(child):
    """`ssh -T ...` (no PTY at all): output must remain byte-exact."""
    _, channel = session(None)
    assert output(channel) == b"Available remote commands:\n  dashboard\n"
    assert b"\r\n" not in output(channel)


def test_each_process_loads_fresh_config_for_help_and_policy(child, repo):
    path = default_global_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("remote:\n  ssh:\n    exec: true\n    commands:\n      mode: full\n")
    _, first = session("--repo project ls")
    first.exit.assert_called_once_with(7)
    path.write_text(
        "remote:\n  ssh:\n    dashboard: false\n    shell: true\n    commands:\n      mode: full\n"
    )
    _, help_channel = session()
    assert output(help_channel) == b"Available remote commands:\n  shell [--repo PREFIX]\n"
    _, last = session("--repo project ls")
    last.exit.assert_called_once_with(2)
    assert b"execution is disabled" in output(last, 1)
    assert child.await_count == 1


@pytest.mark.parametrize("command", ["dashboard", "shell", "shell --repo project"])
def test_interactive_routes_require_pty(command, child, configured, repo):
    _, channel = session(command)
    assert output(channel, 1) == b"This entry point requires a PTY; retry with ssh -t.\n"
    assert output(channel) == b""
    channel.exit.assert_called_once_with(2)
    child.assert_not_awaited()


@pytest.mark.parametrize(
    ("command", "arguments", "requires_pty", "has_repo"),
    [
        ("dashboard", ("dashboard",), True, False),
        ("shell", ("_remote-console",), True, False),
        ("shell --repo project", ("_remote-console", "--repo", "project"), True, True),
        ("--repo project ls --all", ("ls", "--all"), False, True),
        ('--repo project new "literal ; $(value)"', ("new", "literal ; $(value)"), False, True),
    ],
)
def test_dispatch_uses_current_python_literal_argv_and_selected_cwd(
    command, arguments, requires_pty, has_repo, child, configured, repo, tmp_path, mocker
):
    fallback = tmp_path / "state"
    mocker.patch.object(server, "state_dir", return_value=fallback)
    process, channel = session(command, term="xterm" if requires_pty else None)
    expected_argv = (sys.executable, "-m", "jailbee", *arguments)
    if arguments[0] == "_remote-console":
        # `configured`'s own ssh policy, serialized: see
        # test_console_receives_the_session_effective_policy for the
        # dedicated regression test on this argument's *content*.
        policy = RemoteSSHConfig(shell=True, exec=True, commands=RemoteCommandPolicy(mode="full"))
        expected_argv = (*expected_argv, "--policy-json", policy.model_dump_json())
    child.assert_awaited_once_with(
        process,
        ChildSpec(
            argv=expected_argv,
            cwd=repo if has_repo else fallback,
            requires_pty=requires_pty,
        ),
    )
    configured.assert_called_once_with(default_global_config_path())
    channel.exit.assert_called_once_with(7)


def test_console_receives_the_session_effective_policy_including_overrides(child, mocker):
    """Regression for the bug where the console reloaded `global.yaml` itself.

    The console child must be handed the session's EFFECTIVE policy — after
    `jb remote ssh serve` overrides are merged onto `global.yaml` — not the
    raw `global.yaml` policy. Here `global.yaml` alone would keep
    `commands.mode: disabled`; only the override, carried into the spawned
    console's `--policy-json`, allows commands at all. If `handle_process`
    stopped passing `config` (the merged one) and passed the raw reload
    instead, this would observe `commands.mode == "disabled"` and fail.
    """
    from jailbee.remote_ssh.overrides import ServeOverrides

    raw = RemoteSSHConfig(
        dashboard=True, shell=False, commands=RemoteCommandPolicy(mode="disabled")
    )
    mocker.patch.object(
        server,
        "load_global_config",
        return_value=(GlobalConfig(remote=RemoteConfig(ssh=raw)), []),
    )
    overrides = ServeOverrides(shell=True, commands_mode="full")

    session("shell", term="xterm", overrides=overrides)

    argv = child.await_args.args[1].argv
    assert argv[:4] == (sys.executable, "-m", "jailbee", "_remote-console")
    assert argv[4] == "--policy-json"
    sent = RemoteSSHConfig.model_validate_json(argv[5])
    assert sent.shell is True
    assert sent.commands.mode == "full"


def test_optional_pty_remains_available_to_one_shot_child(child, configured, repo):
    process, channel = session("--repo project ls", term="xterm")
    assert child.await_args.args[0] is process
    assert process.term_type == "xterm"
    assert child.await_args.args[1].requires_pty is False
    channel.exit.assert_called_once_with(7)


@pytest.mark.parametrize("command", ["dashboard", "shell"])
def test_fallback_state_cwd_exists_before_interactive_child_starts(
    command, child, configured, tmp_path, mocker
):
    fallback = tmp_path / "new-state" / "jailbee"
    mocker.patch.object(server, "state_dir", return_value=fallback)
    observed = []

    async def completed(process, spec):
        observed.append(spec.cwd.is_dir())
        process.exit(0)

    child.side_effect = completed
    _, channel = session(command, term="xterm")
    assert observed == [True]
    assert fallback.is_dir()
    channel.exit.assert_called_once_with(0)


def test_missing_registered_repo_is_not_recreated(child, configured, repo):
    repo.rmdir()
    _, channel = session("shell --repo project", term="xterm")
    assert not repo.exists()
    assert b"directory is missing" in output(channel, 1)
    channel.exit.assert_called_once_with(2)
    child.assert_not_awaited()


def test_route_rejection_uses_crlf_when_a_pty_was_negotiated(child, configured):
    """Same CRLF fix, exercised on a rejection message rather than help text."""
    _, channel = session("shell --repo missing", term="xterm")
    assert output(channel, 1) == b"unknown registered repo: missing\r\n"
    channel.exit.assert_called_once_with(2)


def test_route_rejection_stays_bare_lf_without_a_pty(child, configured, repo):
    """A hidden internal command (no public alias twin) is still rejected."""
    _, channel = session("--repo project _remote-console")
    err = output(channel, 1)
    assert err == b"unknown Jailbee command\n"
    assert b"\r\n" not in err


def test_unknown_command_is_delegated_to_the_child_for_its_own_error(child, configured, repo):
    """Problem C: a name matching nothing at all — public, hidden, or

    otherwise — is handed to the child as-is instead of being rejected by
    this router, so `python -m jailbee` reports its own "No such command"
    error (with suggestions) in its own style.
    """
    _, channel = session("--repo project no-such-command")
    child.assert_awaited_once()
    assert child.await_args.args[1].argv == (
        sys.executable,
        "-m",
        "jailbee",
        "no-such-command",
    )
    assert output(channel, 1) == b""
    channel.exit.assert_called_once_with(7)


@pytest.mark.parametrize(
    ("command", "settings", "message"),
    [
        ("dashboard", {"dashboard": False, "shell": True}, b"dashboard is disabled"),
        ("shell", {}, b"shell is disabled"),
        ("--repo project ls", {}, b"execution is disabled"),
    ],
)
def test_disabled_entrypoint_cannot_spawn(command, settings, message, child, mocker):
    config = RemoteSSHConfig(**settings, commands=RemoteCommandPolicy(mode="full"))
    mocker.patch.object(
        server,
        "load_global_config",
        return_value=(
            GlobalConfig(remote=RemoteConfig(ssh=config)),
            [],
        ),
    )
    _, channel = session(command, term="xterm")
    assert message in output(channel, 1)
    channel.exit.assert_called_once_with(2)
    child.assert_not_awaited()


def test_overrides_are_reapplied_after_a_per_session_global_config_reload(child, mocker):
    """`jb remote ssh serve` overrides must survive a `global.yaml` edit mid-run.

    Each session reloads `global.yaml` fresh (see `handle_process`); an
    override given on the command line must keep winning on every one of
    those reloads, not just the first. Session two's raw reloaded config
    alone disables `dashboard` — only the override, reapplied, keeps the
    route working.
    """
    from jailbee.remote_ssh.overrides import ServeOverrides

    overrides = ServeOverrides(dashboard=True)
    first_raw = RemoteSSHConfig(
        dashboard=True, shell=True, commands=RemoteCommandPolicy(mode="full")
    )
    second_raw = RemoteSSHConfig(
        dashboard=False, shell=True, commands=RemoteCommandPolicy(mode="full")
    )
    load = mocker.patch.object(
        server,
        "load_global_config",
        side_effect=[
            (GlobalConfig(remote=RemoteConfig(ssh=first_raw)), []),
            (GlobalConfig(remote=RemoteConfig(ssh=second_raw)), []),
        ],
    )

    _, first_channel = session("dashboard", term="xterm", overrides=overrides)
    _, second_channel = session("dashboard", term="xterm", overrides=overrides)

    assert load.call_count == 2
    first_channel.exit.assert_called_once_with(7)
    second_channel.exit.assert_called_once_with(7)
    assert child.await_count == 2


def test_without_overrides_a_reloaded_config_change_takes_effect_immediately(child, mocker):
    """The `overrides=None` default must not change today's reload behaviour."""
    first_raw = RemoteSSHConfig(
        dashboard=True, shell=True, commands=RemoteCommandPolicy(mode="full")
    )
    second_raw = RemoteSSHConfig(
        dashboard=False, shell=True, commands=RemoteCommandPolicy(mode="full")
    )
    mocker.patch.object(
        server,
        "load_global_config",
        side_effect=[
            (GlobalConfig(remote=RemoteConfig(ssh=first_raw)), []),
            (GlobalConfig(remote=RemoteConfig(ssh=second_raw)), []),
        ],
    )

    _, first_channel = session("dashboard", term="xterm")
    _, second_channel = session("dashboard", term="xterm")

    first_channel.exit.assert_called_once_with(7)
    assert b"remote dashboard is disabled" in output(second_channel, 1)
    second_channel.exit.assert_called_once_with(2)
    child.assert_awaited_once()


def test_invalid_override_combination_rejects_the_session_like_a_broken_config(child):
    """An override that fails validation is handled like a broken `global.yaml`."""
    from jailbee.remote_ssh.overrides import ServeOverrides

    _, channel = session("dashboard", overrides=ServeOverrides(shell=True))

    assert b"commands" in output(channel, 1).lower()
    channel.exit.assert_called_once_with(2)
    child.assert_not_awaited()


@pytest.mark.parametrize("subsystem", ["sftp", ""])
def test_subsystems_are_rejected_before_dispatch(subsystem, child):
    _, channel = session("dashboard", term="xterm", subsystem=subsystem)
    assert b"subsystem" in output(channel, 1).lower()
    channel.exit.assert_called_once_with(2)
    child.assert_not_awaited()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"env": {"LD_PRELOAD": "client-secret"}},
        {"env": {"TERM": "xterm"}},
        {"env": {"LANG": ""}},
        {"raw_env": {b"INVALID": b"\xff"}},
    ],
)
def test_client_environment_requests_are_ignored_not_rejected(kwargs, child, configured, repo):
    # Regression for finding C1: stock OpenSSH clients send `SendEnv` values
    # (typically LANG/LC_*) on every connection. Rejecting the session over
    # them broke the service for every default client; the session must
    # dispatch normally instead, with the client's environment never reaching
    # the child (pty.py/run_child build the child's env from os.environ only).
    _, channel = session("--repo project ls", **kwargs)
    channel.exit.assert_called_once_with(7)
    child.assert_awaited_once()
    spec = child.await_args.args[1]
    assert spec.argv == (sys.executable, "-m", "jailbee", "ls")


def test_client_environment_requests_do_not_block_commandless_help(child):
    _, channel = session(None, env={"LANG": "C.UTF-8"})
    assert output(channel) == b"Available remote commands:\n  dashboard\n"
    channel.exit.assert_called_once_with(0)
    child.assert_not_awaited()


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ("arbitrary-host-shell", b"require --repo"),
        ("--repo project _new-worker", b"unknown Jailbee command"),
        ('--repo project ls "unterminated', b"cannot parse"),
        ("--repo project ls\n", b"control character"),
        ("--repo absent ls", b"unknown registered repo"),
    ],
)
def test_route_errors_use_stderr_and_nonzero_exit(command, message, child, configured, repo):
    _, channel = session(command)
    assert output(channel) == b""
    assert message in output(channel, 1)
    channel.exit.assert_called_once_with(2)
    child.assert_not_awaited()


def test_child_binary_streams_and_exit_status_pass_through(child, configured, repo):
    async def completed(process, spec):
        process.stdout.write(b"\x00\xffoutput\r\n")
        process.stderr.write(b"\xfeerror\n")
        process.exit(19)

    child.side_effect = completed
    _, channel = session("--repo project ls")
    assert output(channel) == b"\x00\xffoutput\r\n"
    assert output(channel, 1) == b"\xfeerror\n"
    channel.exit.assert_called_once_with(19)


@pytest.mark.parametrize("signaled", [False, True])
def test_child_exit_audit_preserves_status_signal_and_original_callbacks(
    child, configured, repo, caplog, signaled
):
    async def completed(process, spec):
        if signaled:
            process.exit_with_signal(
                "TERM", core_dumped=True, msg="private signal detail", lang="fi"
            )
        else:
            process.exit(status=23)

    child.side_effect = completed
    with caplog.at_level(logging.INFO):
        _, channel = session('--repo project ls "sensitive-argument"')
    if signaled:
        channel.exit_with_signal.assert_called_once_with(
            "TERM", True, "private signal detail", "fi"
        )
        channel.exit.assert_not_called()
        assert "status=signal:TERM" in caplog.text
    else:
        channel.exit.assert_called_once_with(23)
        assert "status=23" in caplog.text
    assert "192.0.2.10" in caplog.text
    assert FINGERPRINT in caplog.text
    assert "route=command" in caplog.text
    assert "repo='project'" in caplog.text
    assert "command='ls'" in caplog.text
    assert "decision=allowed" in caplog.text
    assert "sensitive-argument" not in caplog.text
    assert "private signal detail" not in caplog.text


def test_rejected_command_audit_records_only_public_path(child, repo, caplog, mocker):
    config = RemoteSSHConfig(
        exec=True, commands=RemoteCommandPolicy(mode="allowlist", allow=["ls"])
    )
    mocker.patch.object(
        server,
        "load_global_config",
        return_value=(
            GlobalConfig(remote=RemoteConfig(ssh=config)),
            [],
        ),
    )
    with caplog.at_level(logging.INFO):
        _, channel = session('--repo project git pull "credential-secret"')
    assert b"not allowed: git pull" in output(channel, 1)
    assert "decision=rejected" in caplog.text
    assert "command='git pull'" in caplog.text
    assert "repo='project'" in caplog.text
    assert "status=2" in caplog.text
    assert "credential-secret" not in caplog.text
    child.assert_not_awaited()


def test_unknown_command_and_environment_values_are_not_audited(child, configured, caplog):
    with caplog.at_level(logging.INFO):
        session("--repo project secret-unknown-command --password secret-value")
        session(env={"SECRET_ENV": "secret-environment"})
    for secret in ("secret-unknown-command", "secret-value", "SECRET_ENV", "secret-environment"):
        assert secret not in caplog.text


@pytest.mark.parametrize(
    ("failure", "status", "message"),
    [
        (PTYError("invalid terminal dimensions"), 2, b"invalid terminal dimensions"),
        (OSError("private child argv"), 1, b"session failed"),
        (RuntimeError("private child argv"), 1, b"session failed"),
    ],
)
def test_child_failures_close_only_channel_and_restore_exit_callbacks(
    child, configured, repo, caplog, failure, status, message
):
    child.side_effect = failure
    with caplog.at_level(logging.INFO):
        _, channel = session("--repo project ls")
    channel.exit.assert_called_once_with(status)
    assert message in output(channel, 1)
    assert type(failure).__name__ in caplog.text
    assert "private child argv" not in caplog.text


def test_config_failure_rejects_channel_without_dispatch(child, mocker):
    mocker.patch.object(
        server, "load_global_config", side_effect=ConfigError("invalid configuration")
    )
    _, channel = session("dashboard", term="xterm")
    assert b"invalid configuration" in output(channel, 1)
    channel.exit.assert_called_once_with(2)
    child.assert_not_awaited()


def test_disconnect_audit_does_not_write_to_closed_channel(child, configured, repo, caplog):
    child.side_effect = ConnectionError("private transport details")
    with caplog.at_level(logging.INFO):
        _, channel = session("--repo project ls")
    channel.write.assert_not_called()
    channel.exit.assert_not_called()
    assert "ConnectionError" in caplog.text
    assert "private transport details" not in caplog.text


def test_cancellation_restores_callbacks_and_does_not_log_terminal_input(
    child, configured, repo, caplog
):
    async def scenario():
        entered = asyncio.Event()

        async def pending(process, spec):
            entered.set()
            await asyncio.Future()

        child.side_effect = pending
        process, channel = actual_process("--repo project ls")
        process.data_received(b"terminal-input-secret", None)
        original_exit, original_signal = process.exit, process.exit_with_signal
        task = asyncio.create_task(server.handle_process(process))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.exit == original_exit
        assert process.exit_with_signal == original_signal
        channel.exit.assert_not_called()
        channel.write.assert_not_called()

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert "cancelled" in caplog.text
    assert "terminal-input-secret" not in caplog.text


@pytest.fixture
def listener(mocker):
    value = SimpleNamespace(wait_closed=AsyncMock(), close=Mock(), get_port=Mock(return_value=8022))
    listen = mocker.patch.object(
        server.asyncssh, "listen", new_callable=AsyncMock, return_value=value
    )
    return value, listen


def test_listener_exposes_only_binary_session_capabilities(listener):
    value, listen = listener
    asyncio.run(server.serve_async(RemoteSSHConfig(listen="127.0.0.2", port=8123)))
    listen.assert_awaited_once()
    args, kwargs = listen.call_args
    assert args == ("127.0.0.2", 8123)
    # `server_factory` is a per-run closure (it shares a live-connection
    # registry with the SIGTERM handler), not the class directly.
    assert isinstance(kwargs.pop("server_factory")(), server.JailbeeSSHServer)
    # Also a per-run closure: it hands every session the run's UpdateWatch.
    assert callable(kwargs.pop("process_factory"))
    assert kwargs == {
        "server_host_keys": [str(ssh_paths().host_key)],
        "encoding": None,
        "agent_forwarding": False,
        "x11_forwarding": False,
        "sftp_factory": None,
        "allow_scp": False,
        "gss_auth": False,
        "gss_kex": False,
        "gss_host": None,
    }
    value.wait_closed.assert_awaited_once_with()
    value.close.assert_called_once_with()


def test_sigterm_closes_listener_and_hangs_up_every_live_connection(listener, monkeypatch):
    """Regression for final-review finding I2.

    `KillMode=process` in the unit means systemd sends SIGTERM to only this
    process on stop/restart, not the whole cgroup, so `--background` workers
    survive. This process must itself react to that SIGTERM by closing the
    listener and every live connection, driving each session's existing
    pty.py HUP/grace/kill cleanup through the normal disconnect path.
    """
    value, listen = listener

    async def scenario():
        entered, finished = asyncio.Event(), asyncio.Event()

        async def waiting():
            entered.set()
            await finished.wait()

        value.wait_closed.side_effect = waiting

        loop = asyncio.get_running_loop()
        captured: dict[int, tuple] = {}
        monkeypatch.setattr(
            loop, "add_signal_handler", lambda sig, cb, *a: captured.__setitem__(sig, (cb, a))
        )
        monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: captured.pop(sig, None))

        task = asyncio.create_task(server.serve_async(RemoteSSHConfig()))
        await entered.wait()

        assert signal.SIGTERM in captured
        factory = listen.call_args.kwargs["server_factory"]
        instance = factory()
        conn = Mock(spec=asyncssh.SSHServerConnection)
        instance.connection_made(conn)

        callback, args = captured[signal.SIGTERM]
        callback(*args)

        value.close.assert_called_once_with()
        conn.close.assert_called_once_with()

        finished.set()
        await task

    asyncio.run(scenario())


def test_disconnected_connection_is_not_closed_twice_on_sigterm(listener, monkeypatch):
    value, listen = listener

    async def scenario():
        entered, finished = asyncio.Event(), asyncio.Event()

        async def waiting():
            entered.set()
            await finished.wait()

        value.wait_closed.side_effect = waiting

        loop = asyncio.get_running_loop()
        captured: dict[int, tuple] = {}
        monkeypatch.setattr(
            loop, "add_signal_handler", lambda sig, cb, *a: captured.__setitem__(sig, (cb, a))
        )
        monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: captured.pop(sig, None))

        task = asyncio.create_task(server.serve_async(RemoteSSHConfig()))
        await entered.wait()

        factory = listen.call_args.kwargs["server_factory"]
        instance = factory()
        conn = Mock(spec=asyncssh.SSHServerConnection)
        instance.connection_made(conn)
        instance.connection_lost(None)

        callback, args = captured[signal.SIGTERM]
        callback(*args)
        conn.close.assert_not_called()

        finished.set()
        await task

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [OSError("listener failed"), asyncio.CancelledError()])
def test_listener_closes_when_wait_raises(listener, failure):
    value, _ = listener
    value.wait_closed.side_effect = failure
    with pytest.raises(type(failure)):
        asyncio.run(server.serve_async(RemoteSSHConfig()))
    value.close.assert_called_once_with()


def test_listener_stays_alive_until_closed_then_closes_once(listener):
    value, _ = listener

    async def scenario():
        entered, finished = asyncio.Event(), asyncio.Event()

        async def waiting():
            entered.set()
            await finished.wait()

        value.wait_closed.side_effect = waiting
        task = asyncio.create_task(server.serve_async(RemoteSSHConfig()))
        await entered.wait()
        assert not task.done()
        value.close.assert_not_called()
        finished.set()
        await task

    asyncio.run(scenario())
    value.close.assert_called_once_with()


@pytest.mark.parametrize(
    "failure", [OSError("address already in use"), ValueError("invalid host key")]
)
def test_bind_or_configuration_failure_reaches_sync_caller(listener, failure):
    value, listen = listener
    listen.side_effect = failure
    with pytest.raises(type(failure), match=str(failure)):
        server.serve(RemoteSSHConfig())
    value.wait_closed.assert_not_awaited()
    value.close.assert_not_called()


def test_sync_serve_runs_and_finishes_listener(listener):
    value, listen = listener
    assert server.serve(RemoteSSHConfig()) is None
    listen.assert_awaited_once()
    value.wait_closed.assert_awaited_once()
    value.close.assert_called_once()


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("prior_level", [logging.NOTSET, logging.DEBUG, logging.ERROR])
def test_listener_suppresses_library_command_logs_and_restores_level(
    listener, caplog, failure, prior_level
):
    value, listen = listener
    library_log = logging.getLogger("asyncssh")

    def log_private_details():
        library_log.info("Command: --repo project ls private-argument")
        library_log.debug("Env: TOKEN=private-environment")
        library_log.warning("transport warning")
        library_log.error("transport error")

    async def start(*args, **kwargs):
        log_private_details()
        if failure:
            raise OSError("bind failed")
        return value

    async def wait_closed():
        log_private_details()

    listen.side_effect = start
    value.wait_closed.side_effect = wait_closed
    with caplog.at_level(logging.DEBUG), caplog.at_level(prior_level, logger="asyncssh"):
        if failure:
            with pytest.raises(OSError, match="bind failed"):
                server.serve(RemoteSSHConfig())
        else:
            server.serve(RemoteSSHConfig())
        assert library_log.level == prior_level
    assert "private-argument" not in caplog.text
    assert "private-environment" not in caplog.text
    if prior_level <= logging.WARNING:
        assert "transport warning" in caplog.text
    assert "transport error" in caplog.text


@pytest.mark.parametrize("handler_level", [None, logging.NOTSET, logging.WARNING])
def test_sync_serve_establishes_audit_visibility_without_library_command_logs(
    listener, caplog, capsys, monkeypatch, handler_level
):
    value, listen = listener
    root = logging.getLogger()
    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    if handler_level is not None:
        handler.setLevel(handler_level)
    initial_handlers = [handler] if handler_level is not None else []

    async def start(*args, **kwargs):
        process, _ = actual_process()
        await server.handle_process(process)
        logging.getLogger("asyncssh").info("Command: full-secret-argv")
        return value

    listen.side_effect = start
    with caplog.at_level(logging.WARNING), caplog.at_level(logging.NOTSET, logger=server.__name__):
        monkeypatch.setattr(root, "handlers", initial_handlers.copy())
        try:
            server.serve(RemoteSSHConfig())
            text = capsys.readouterr().err
            assert text.count("SSH session") == 1
            assert FINGERPRINT in text
            assert "status=0" in text
            assert "full-secret-argv" not in text
            assert captured.getvalue() == ""
            assert root.handlers == initial_handlers
            assert root.level == logging.WARNING
            if handler_level is not None:
                assert handler.level == handler_level
        finally:
            for installed in root.handlers:
                installed.close()


def test_startup_announces_real_port_entry_points_mode_fingerprint_and_key_count(
    listener, caplog, tmp_path, monkeypatch
):
    # XDG_DATA_HOME is overridden here (not just XDG_CONFIG_HOME, already
    # test-isolated by an autouse fixture) because it is session-scoped in
    # conftest.py; writing the real host key under it would leak across tests.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    value, _ = listener
    value.get_port.return_value = 19999
    paths = ssh_paths()
    paths.config_dir.mkdir(parents=True)
    paths.data_dir.mkdir(parents=True)
    other = asyncssh.generate_private_key("ssh-ed25519")
    other_public_line = other.export_public_key().decode("ascii").strip()
    paths.authorized_keys.write_text(PUBLIC_KEY + " one\n" + other_public_line + " two\n")
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    paths.host_key.write_bytes(host_key.export_private_key("openssh"))
    expected_fingerprint = host_key.get_fingerprint("sha256")
    config = RemoteSSHConfig(
        listen="198.51.100.7",
        port=8123,
        dashboard=True,
        shell=True,
        exec=True,
        commands=RemoteCommandPolicy(mode="full"),
    )
    with caplog.at_level(logging.INFO, logger=server.__name__):
        asyncio.run(server.serve_async(config))
    text = caplog.text
    assert "198.51.100.7:19999" in text
    assert "198.51.100.7:8123" not in text
    assert "entry points: dashboard, shell, exec" in text
    assert "commands: full" in text
    assert f"host key fingerprint: {expected_fingerprint}" in text
    assert "2 authorized client keys" in text
    assert "connect example: ssh -t -p 19999 jailbee@198.51.100.7 dashboard" in text
    assert "overrides (not from global.yaml)" not in text


def test_startup_prints_the_overrides_line_only_when_overrides_are_given(
    listener, caplog, tmp_path, monkeypatch
):
    from jailbee.remote_ssh.overrides import ServeOverrides

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    paths = ssh_paths()
    paths.config_dir.mkdir(parents=True)
    paths.data_dir.mkdir(parents=True)
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    paths.host_key.write_bytes(host_key.export_private_key("openssh"))
    config = RemoteSSHConfig(shell=True, commands=RemoteCommandPolicy(mode="full"))
    overrides = ServeOverrides(shell=True, commands_mode="allowlist", allow=["ls", "new"])
    with caplog.at_level(logging.INFO, logger=server.__name__):
        asyncio.run(server.serve_async(config, overrides))
    text = caplog.text
    assert "overrides (not from global.yaml): shell=on, commands=allowlist [ls, new]" in text


def test_listener_uses_a_closure_process_factory_when_overrides_are_given(listener):
    from jailbee.remote_ssh.overrides import ServeOverrides

    overrides = ServeOverrides(dashboard=False)
    config = RemoteSSHConfig(shell=True, commands=RemoteCommandPolicy(mode="full"))
    asyncio.run(server.serve_async(config, overrides))
    _, listen = listener
    factory = listen.call_args.kwargs["process_factory"]
    assert factory is not server.handle_process


def test_startup_brackets_ipv6_listen_address_and_uses_localhost_in_example(
    listener, caplog, tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    value, _ = listener
    value.get_port.return_value = 8022
    paths = ssh_paths()
    paths.config_dir.mkdir(parents=True)
    paths.data_dir.mkdir(parents=True)
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    paths.host_key.write_bytes(host_key.export_private_key("openssh"))
    config = RemoteSSHConfig(listen="::1", port=8022)
    with caplog.at_level(logging.INFO, logger=server.__name__):
        asyncio.run(server.serve_async(config))
    text = caplog.text
    assert "[::1]:8022" in text
    assert "connect example: ssh -t -p 8022 jailbee@localhost dashboard" in text


def test_startup_reports_zero_keys_with_add_hint(listener, caplog, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    paths = ssh_paths()
    paths.data_dir.mkdir(parents=True)
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    paths.host_key.write_bytes(host_key.export_private_key("openssh"))
    with caplog.at_level(logging.INFO, logger=server.__name__):
        asyncio.run(server.serve_async(RemoteSSHConfig()))
    text = caplog.text
    assert "0 authorized client keys" in text
    assert "jb remote ssh key add" in text


def test_startup_survives_unreadable_authorized_keys(listener, caplog, mocker):
    value, _ = listener
    paths = ssh_paths()
    paths.data_dir.mkdir(parents=True)
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    paths.host_key.write_bytes(host_key.export_private_key("openssh"))
    mocker.patch.object(server, "read_authorized_keys", side_effect=PermissionError("denied"))
    with caplog.at_level(logging.INFO, logger=server.__name__):
        asyncio.run(server.serve_async(RemoteSSHConfig()))
    text = caplog.text
    assert "authorized keys could not be read" in text
    assert "PermissionError" in text
    value.wait_closed.assert_awaited_once()


def test_startup_survives_unreadable_host_key(listener, caplog, mocker):
    mocker.patch.object(
        server.asyncssh, "read_private_key", side_effect=OSError("no such host key")
    )
    with caplog.at_level(logging.INFO, logger=server.__name__):
        asyncio.run(server.serve_async(RemoteSSHConfig()))
    text = caplog.text
    assert "host key fingerprint: could not be read" in text
    assert "OSError" in text


def test_clean_shutdown_logs_stopped_message(listener, caplog):
    with caplog.at_level(logging.INFO, logger=server.__name__):
        asyncio.run(server.serve_async(RemoteSSHConfig()))
    assert "Jailbee SSH server stopped" in caplog.text


def test_ctrl_c_exits_130_closes_listener_and_logs_stopped_without_traceback(listener, capsys):
    value, _ = listener
    value.wait_closed.side_effect = KeyboardInterrupt()
    with pytest.raises(SystemExit) as excinfo:
        server.serve(RemoteSSHConfig())
    assert excinfo.value.code == 130
    value.close.assert_called_once_with()
    err = capsys.readouterr().err
    assert "stopped" in err.lower()
    assert "Traceback" not in err


@pytest.mark.parametrize("outcome", ["closed", "startup_error", "wait_error", "cancelled"])
@pytest.mark.parametrize("prior_level,propagate", [(logging.NOTSET, True), (logging.ERROR, False)])
def test_sync_serve_removes_only_its_audit_handler_and_restores_logger(
    listener, caplog, capsys, monkeypatch, mocker, outcome, prior_level, propagate
):
    value, listen = listener
    existing = logging.StreamHandler(io.StringIO())
    existing.setLevel(logging.WARNING)
    existing_close = mocker.spy(existing, "close")
    installed = []

    async def start(*args, **kwargs):
        added = [handler for handler in server.log.handlers if handler is not existing]
        assert len(added) == 1
        audit_handler = added[0]
        installed.append((audit_handler, mocker.spy(audit_handler, "close")))
        assert audit_handler.level == logging.INFO
        assert server.log.level == logging.INFO
        assert server.log.propagate is False
        process, _ = actual_process()
        await server.handle_process(process)
        if outcome == "startup_error":
            raise OSError("bind failed")
        return value

    async def wait_closed():
        if outcome == "wait_error":
            raise OSError("wait failed")
        if outcome == "cancelled":
            raise asyncio.CancelledError

    listen.side_effect = start
    value.wait_closed.side_effect = wait_closed
    with caplog.at_level(prior_level, logger=server.__name__):
        monkeypatch.setattr(server.log, "handlers", [existing])
        monkeypatch.setattr(server.log, "propagate", propagate)
        try:
            if outcome == "closed":
                server.serve(RemoteSSHConfig())
            else:
                error = asyncio.CancelledError if outcome == "cancelled" else OSError
                with pytest.raises(error):
                    server.serve(RemoteSSHConfig())
            assert capsys.readouterr().err.count("SSH session") == 1
            assert server.log.handlers == [existing]
            assert server.log.level == prior_level
            assert server.log.propagate is propagate
            assert len(installed) == 1
            installed[0][1].assert_called_once_with()
            existing_close.assert_not_called()
            assert existing.level == logging.WARNING
        finally:
            existing.close()


def test_restrict_host_false_reaches_the_child_spec(child, mocker, repo):
    ssh = RemoteSSHConfig(exec=True, commands=RemoteCommandPolicy(mode="full"), restrict_host=False)
    mocker.patch.object(
        server,
        "load_global_config",
        return_value=(GlobalConfig(remote=RemoteConfig(ssh=ssh)), []),
    )

    session("--repo project ls")

    assert child.call_args.args[1].restrict_host is False


def test_restrict_host_override_reaches_the_child_spec(child, configured, repo):
    from jailbee.remote_ssh.overrides import ServeOverrides

    session("--repo project ls", overrides=ServeOverrides(restrict_host=False))

    assert child.call_args.args[1].restrict_host is False


def test_default_child_spec_is_restricted(child, configured, repo):
    session("--repo project ls")

    assert child.call_args.args[1].restrict_host is True


@pytest.mark.parametrize(
    ("restrict_host", "marker", "want"),
    [
        (True, None, "host restrictions: on"),
        (False, None, "host restrictions: OFF"),
        (False, "1", "host restrictions: on"),  # nested in a restricted session
    ],
)
def test_startup_reports_host_restrictions(
    listener, caplog, monkeypatch, restrict_host, marker, want
):
    if marker is None:
        monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)
    else:
        monkeypatch.setenv("JAILBEE_REMOTE_SSH", marker)

    with caplog.at_level(logging.INFO, logger=server.__name__):
        asyncio.run(server.serve_async(RemoteSSHConfig(restrict_host=restrict_host)))

    assert want in caplog.text


def test_startup_names_allowlisted_host_commands_that_stay_refused(listener, caplog, monkeypatch):
    monkeypatch.delenv("JAILBEE_REMOTE_SSH", raising=False)
    config = RemoteSSHConfig(
        exec=True,
        commands=RemoteCommandPolicy(mode="allowlist", allow=["ls", "config edit", "setup"]),
    )

    with caplog.at_level(logging.INFO, logger=server.__name__):
        asyncio.run(server.serve_async(config))

    assert "allowlisted but refused while host restrictions are on: config edit, setup" in (
        caplog.text
    )


def test_process_factory_passes_the_run_update_watch(listener, mocker):
    # A plain Mock: the factory's coroutine is never awaited here.
    handle = mocker.patch.object(server, "handle_process", new=Mock())
    asyncio.run(server.serve_async(RemoteSSHConfig()))
    _, listen = listener
    factory = listen.call_args.kwargs["process_factory"]
    process = object()

    factory(process)

    args, kwargs = handle.call_args
    assert args == (process, None)
    assert isinstance(kwargs["update"], server.UpdateWatch)
    assert kwargs["update"].running == server.__version__


def test_update_watch_reports_the_replacing_version_once_it_differs():
    installed = iter(["1.0.0", "1.1.0", "1.2.0"])
    stop = Mock()
    watch = server.UpdateWatch("1.0.0", stop, installed=lambda: next(installed))

    assert watch.changed() is None
    assert watch.changed() == "1.1.0"
    assert watch.changed() == "1.1.0"  # latched: not re-read once changed
    stop.assert_not_called()
    watch.stop()
    stop.assert_called_once_with()


def test_a_session_on_an_upgraded_server_is_refused_and_stops_the_server(child, configured):
    stop = Mock()
    watch = server.UpdateWatch("1.0.0", stop, installed=lambda: "1.1.0")

    async def run():
        process, channel = actual_process("--repo project ls")
        await server.handle_process(process, None, update=watch)
        return channel

    channel = asyncio.run(run())

    assert b"JailBee was updated to 1.1.0" in output(channel, 1)
    channel.exit.assert_called_once_with(server.SERVICE_UPDATED_EXIT)
    child.assert_not_awaited()
    configured.assert_not_called()
    stop.assert_called_once_with()


def test_a_session_on_a_current_server_runs_normally(child, configured, repo):
    watch = server.UpdateWatch("1.0.0", Mock(), installed=lambda: "1.0.0")

    async def run():
        process, channel = actual_process("--repo project ls")
        await server.handle_process(process, None, update=watch)
        return channel

    channel = asyncio.run(run())

    child.assert_awaited_once()
    channel.exit.assert_called_once_with(7)


def test_serve_async_stops_and_raises_when_the_poll_sees_an_upgrade(listener, mocker):
    value, _ = listener
    closed = asyncio.Event()

    async def wait_closed():
        await closed.wait()

    value.wait_closed.side_effect = wait_closed
    value.close.side_effect = closed.set
    mocker.patch.object(server, "__version__", "1.0.0")
    versions = iter(["1.0.0", "1.0.0"])
    mocker.patch.object(server, "installed_version", side_effect=lambda: next(versions, "1.1.0"))

    with pytest.raises(server.ServiceUpdatedError) as info:
        asyncio.run(server.serve_async(RemoteSSHConfig(), update_poll_seconds=0))

    assert (info.value.running, info.value.installed) == ("1.0.0", "1.1.0")
    value.close.assert_called()


def test_serve_exits_75_after_an_upgrade(mocker):
    mocker.patch.object(
        server, "serve_async", side_effect=server.ServiceUpdatedError("1.0.0", "1.1.0")
    )

    with pytest.raises(SystemExit) as info:
        server.serve(RemoteSSHConfig())

    assert info.value.code == 75


def test_serve_async_records_itself_while_running_and_clears_after(listener, mocker):
    record = mocker.patch.object(server, "record_running")
    clear = mocker.patch.object(server, "clear_running")

    asyncio.run(server.serve_async(RemoteSSHConfig()))

    record.assert_called_once_with(server.__version__)
    clear.assert_called_once_with()
