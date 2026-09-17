"""Authentication, channel policy, dispatch, and listener lifecycle without sockets."""

from __future__ import annotations

import asyncio
import io
import logging
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import asyncssh
import pytest
from sqlmodel import Session

from jailbee.config import ConfigError
from jailbee.config.models_remote import RemoteCommandPolicy, RemoteConfig, RemoteSSHConfig
from jailbee.global_config import GlobalConfig, default_global_config_path
from jailbee.remote_ssh import server
from jailbee.remote_ssh.keys import ssh_paths
from jailbee.remote_ssh.pty import ChildSpec, PTYError
from jailbee.db.models import RegisteredRepo

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


def test_each_auth_reads_added_and_removed_keys(ssh_server, connection, authorized_file, public_key):
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


def session(command=None, **kwargs):
    async def run():
        process, channel = actual_process(command, **kwargs)
        original_exit, original_signal = process.exit, process.exit_with_signal
        await server.handle_process(process)
        assert process.exit == original_exit
        assert process.exit_with_signal == original_signal
        return process, channel

    return asyncio.run(run())


def output(channel, datatype=None):
    return b"".join(
        data for data, stream in (item.args for item in channel.write.call_args_list)
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
        db.add(RegisteredRepo(
            container_prefix="project",
            repo_root=str(root),
            registered_at=datetime(2026, 9, 18, tzinfo=UTC),
        ))
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


def test_each_process_loads_fresh_config_for_help_and_policy(child, repo):
    path = default_global_config_path()
    path.parent.mkdir(parents=True)
    path.write_text("remote:\n  ssh:\n    exec: true\n    commands:\n      mode: full\n")
    _, first = session("--repo project ls")
    first.exit.assert_called_once_with(7)
    path.write_text("remote:\n  ssh:\n    dashboard: false\n    shell: true\n    commands:\n      mode: full\n")
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
        ("dashboard", ("dashboard", "--registered-only"), True, False),
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
    child.assert_awaited_once_with(process, ChildSpec(
        argv=(sys.executable, "-m", "jailbee", *arguments),
        cwd=repo if has_repo else fallback,
        requires_pty=requires_pty,
    ))
    configured.assert_called_once_with(default_global_config_path())
    channel.exit.assert_called_once_with(7)


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
    mocker.patch.object(server, "load_global_config", return_value=(
        GlobalConfig(remote=RemoteConfig(ssh=config)), [],
    ))
    _, channel = session(command, term="xterm")
    assert message in output(channel, 1)
    channel.exit.assert_called_once_with(2)
    child.assert_not_awaited()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"subsystem": "sftp"}, b"subsystem"),
        ({"subsystem": ""}, b"subsystem"),
        ({"env": {"LD_PRELOAD": "client-secret"}}, b"environment"),
        ({"env": {"TERM": "xterm"}}, b"environment"),
        ({"env": {"LANG": ""}}, b"environment"),
        ({"raw_env": {b"INVALID": b"\xff"}}, b"environment"),
    ],
)
def test_subsystems_and_environment_are_rejected_before_dispatch(kwargs, message, child):
    _, channel = session("dashboard", term="xterm", **kwargs)
    assert message in output(channel, 1).lower()
    channel.exit.assert_called_once_with(2)
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
            process.exit_with_signal("TERM", core_dumped=True, msg="private signal detail", lang="fi")
        else:
            process.exit(status=23)

    child.side_effect = completed
    with caplog.at_level(logging.INFO):
        _, channel = session('--repo project ls "sensitive-argument"')
    if signaled:
        channel.exit_with_signal.assert_called_once_with("TERM", True, "private signal detail", "fi")
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
    config = RemoteSSHConfig(exec=True, commands=RemoteCommandPolicy(mode="allowlist", allow=["ls"]))
    mocker.patch.object(server, "load_global_config", return_value=(
        GlobalConfig(remote=RemoteConfig(ssh=config)), [],
    ))
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
    mocker.patch.object(server, "load_global_config", side_effect=ConfigError("invalid configuration"))
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
    value = SimpleNamespace(wait_closed=AsyncMock(), close=Mock())
    listen = mocker.patch.object(server.asyncssh, "listen", new_callable=AsyncMock, return_value=value)
    return value, listen


def test_listener_exposes_only_binary_session_capabilities(listener):
    value, listen = listener
    asyncio.run(server.serve_async(RemoteSSHConfig(listen="127.0.0.2", port=8123)))
    listen.assert_awaited_once_with(
        "127.0.0.2", 8123,
        server_factory=server.JailbeeSSHServer,
        process_factory=server.handle_process,
        server_host_keys=[str(ssh_paths().host_key)],
        encoding=None,
        agent_forwarding=False,
        x11_forwarding=False,
        sftp_factory=None,
        allow_scp=False,
        gss_auth=False,
        gss_kex=False,
        gss_host=None,
    )
    value.wait_closed.assert_awaited_once_with()
    value.close.assert_called_once_with()


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


@pytest.mark.parametrize("failure", [OSError("address already in use"), ValueError("invalid host key")])
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


@pytest.mark.parametrize("existing_handler", [False, True])
def test_sync_serve_establishes_audit_visibility_without_library_command_logs(
    listener, caplog, capsys, monkeypatch, existing_handler
):
    value, listen = listener
    root = logging.getLogger()
    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    initial_handlers = [handler] if existing_handler else []

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
            text = captured.getvalue() if existing_handler else capsys.readouterr().err
            assert "SSH session" in text
            assert FINGERPRINT in text
            assert "status=0" in text
            assert "full-secret-argv" not in text
            if existing_handler:
                assert root.handlers == [handler]
                assert root.level == logging.WARNING
        finally:
            for installed in root.handlers:
                installed.close()
