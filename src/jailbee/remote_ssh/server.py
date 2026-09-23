"""Public-key authentication and restricted Jailbee sessions over AsyncSSH."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import shlex
import signal
import sys
from typing import TYPE_CHECKING

import asyncssh

from jailbee.config import ConfigError
from jailbee.db import state_dir
from jailbee.global_config import default_global_config_path, load_global_config
from jailbee.remote_ssh.keys import AuthorizedKey, SSHKeyError, read_authorized_keys, ssh_paths
from jailbee.remote_ssh.pty import ChildSpec, PTYError, run_child
from jailbee.remote_ssh.router import RouteError, command_path, help_text, route

if TYPE_CHECKING:
    from jailbee.config.models_remote import RemoteSSHConfig

log = logging.getLogger(__name__)


class JailbeeSSHServer(asyncssh.SSHServer):
    """Keep authentication state local to one connection and refuse forwarding."""

    def __init__(self, live: set[asyncssh.SSHServerConnection] | None = None) -> None:
        # `live`, when given, is a shared registry `serve_async` uses to hang
        # up every connection on SIGTERM. It is `None` for tests and any other
        # caller that constructs a server directly.
        self._live = live

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        self._conn = conn
        self._keys: list[AuthorizedKey] = []
        if self._live is not None:
            self._live.add(conn)
        log.info("SSH connected source=%r", conn.get_extra_info("peername"))

    def connection_lost(self, exc: Exception | None) -> None:
        if self._live is not None:
            self._live.discard(self._conn)
        # Transport exception messages can contain client-supplied text.
        log.info(
            "SSH disconnected source=%r fingerprint=%s reason=%s",
            self._conn.get_extra_info("peername"),
            self._conn.get_extra_info("jailbee_key_fingerprint"),
            type(exc).__name__ if exc is not None else "closed",
        )

    def begin_auth(self, username: str) -> bool:
        self._keys = []
        self._conn.set_extra_info(jailbee_key_fingerprint=None)
        try:
            self._keys = read_authorized_keys()
        except (OSError, SSHKeyError) as exc:
            log.warning(
                "SSH authorization unavailable source=%r reason=%s",
                self._conn.get_extra_info("peername"),
                type(exc).__name__,
            )
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        fingerprint = key.get_fingerprint("sha256")
        allowed = username == "jailbee" and any(
            item.fingerprint == fingerprint for item in self._keys
        )
        if allowed:
            self._conn.set_extra_info(jailbee_key_fingerprint=fingerprint)
        log.info(
            "SSH key source=%r fingerprint=%s decision=%s",
            self._conn.get_extra_info("peername"),
            fingerprint,
            "allowed" if allowed else "rejected",
        )
        return allowed

    def connection_requested(
        self, dest_host: str, dest_port: int, orig_host: str, orig_port: int
    ) -> bool:
        return False

    def server_requested(self, listen_host: str, listen_port: int) -> bool:
        return False

    def unix_connection_requested(self, dest_path: str) -> bool:
        return False

    def unix_server_requested(self, listen_path: str) -> bool:
        return False


def _request_fields(raw: str | None) -> tuple[str, str | None, str | None]:
    """Identify audit fields even for denied routes, without retaining arguments."""
    try:
        argv = shlex.split(raw or "")
    except ValueError:
        return "unknown", None, None
    if not argv:
        return "help", None, None
    if argv[0] == "dashboard":
        return "dashboard", None, "dashboard"
    if argv[0] == "shell":
        prefix = argv[2] if len(argv) >= 3 and argv[1] == "--repo" else None
        return "console", prefix, "shell"
    if len(argv) >= 3 and argv[0] == "--repo":
        try:
            path = command_path(argv[2:])
        except RouteError:
            path = None
        return "command", argv[1], path
    return "unknown", None, None


async def handle_process(process: asyncssh.SSHServerProcess[bytes]) -> None:
    """Reload channel policy, dispatch one route, and audit its final outcome."""
    kind, prefix, path = "unknown", None, None
    decision = "rejected"
    reason = "completed"
    outcome: int | str | None = None
    original_exit = process.exit
    original_signal = process.exit_with_signal

    def exit_status(status: int) -> None:
        nonlocal outcome
        outcome = status
        original_exit(status)

    def exit_signal(
        signal: str, core_dumped: bool = False, msg: str = "", lang: str = "en-US"
    ) -> None:
        nonlocal outcome
        outcome = "signal:" + signal
        original_signal(signal, core_dumped, msg, lang)

    def audit(event: str) -> None:
        log.info(
            "SSH session source=%r fingerprint=%s route=%s repo=%r command=%r "
            "decision=%s reason=%s status=%s",
            process.get_extra_info("peername"),
            process.get_extra_info("jailbee_key_fingerprint"),
            kind,
            prefix,
            path,
            decision,
            event,
            outcome,
        )

    # Server processes have no exit-status getter. Observe their public exit
    # callbacks while run_child owns the process, and always restore them.
    process.exit = exit_status  # type: ignore[method-assign]
    process.exit_with_signal = exit_signal  # type: ignore[method-assign]
    try:
        if process.subsystem is not None:
            raise RouteError("SSH subsystems are not supported")
        # Client environment requests (e.g. OpenSSH's default `SendEnv LANG
        # LC_* ...`) are accepted by the protocol but never consulted: the
        # child's environment is built from this service's own os.environ in
        # pty.py, never from process.env or the channel's raw environment
        # bytes. Rejecting the session over an env request broke every stock
        # OpenSSH client (see the SSH server final review, finding C1).
        kind, prefix, path = _request_fields(process.command)
        global_config, _ = load_global_config(default_global_config_path())
        config = global_config.remote.ssh
        selected = route(process.command, config)
        kind, prefix = selected.kind, selected.repo_prefix
        if selected.requires_pty and process.term_type is None:
            raise PTYError("This entry point requires a PTY; retry with ssh -t.")
        decision = "allowed"
        if selected.kind == "help":
            process.stdout.write(help_text(config).encode("utf-8"))
            process.exit(0)
            return
        spec = ChildSpec(
            argv=(sys.executable, "-m", "jailbee", *selected.argv),
            cwd=selected.repo_root or state_dir(),
            requires_pty=selected.requires_pty,
        )
        if selected.repo_root is None:
            spec.cwd.mkdir(parents=True, exist_ok=True)
        audit("started")
        await run_child(process, spec)
    except (ConfigError, RouteError, PTYError) as exc:
        reason = type(exc).__name__
        process.stderr.write((str(exc) + "\n").encode("utf-8"))
        process.exit(2)
    except ConnectionError as exc:
        reason = type(exc).__name__
    except asyncio.CancelledError:
        reason = "cancelled"
        raise
    except Exception as exc:
        # Exceptions from child setup may include argv or other private data.
        reason = type(exc).__name__
        process.stderr.write(b"Remote Jailbee session failed.\n")
        process.exit(1)
    finally:
        # Restore the two public callback methods overridden for this channel.
        process.exit = original_exit  # type: ignore[method-assign]
        process.exit_with_signal = original_signal  # type: ignore[method-assign]
        audit(reason)


def _bind_display(host: str, port: int) -> str:
    """Format a listen address for display, bracketing IPv6 literals."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        return f"{host}:{port}"
    return f"[{host}]:{port}" if literal.version == 6 else f"{host}:{port}"


def _enabled_entry_points(config: RemoteSSHConfig) -> str:
    points = [
        name
        for name, enabled in (
            ("dashboard", config.dashboard),
            ("shell", config.shell),
            ("exec", config.exec),
        )
        if enabled
    ]
    return ", ".join(points) if points else "none"


def _connect_example(host: str, port: int, config: RemoteSSHConfig) -> str:
    try:
        display_host = "localhost" if ipaddress.ip_address(host).is_loopback else host
    except ValueError:
        display_host = host
    if config.dashboard:
        return f"ssh -t -p {port} jailbee@{display_host} dashboard"
    if config.shell:
        return f"ssh -t -p {port} jailbee@{display_host} shell"
    return f"ssh -p {port} jailbee@{display_host}"


def _startup_summary(config: RemoteSSHConfig, listener: asyncssh.SSHAcceptor) -> str:
    """Describe the running listener for the audit log, without secrets.

    The port comes from the listener itself (not `config.port`) so a
    configured port of 0 is reported as the port the kernel actually chose.
    """
    port = listener.get_port()
    try:
        fingerprint = asyncssh.read_private_key(str(ssh_paths().host_key)).get_fingerprint("sha256")
    except (OSError, asyncssh.KeyImportError) as exc:
        fingerprint = f"could not be read ({type(exc).__name__})"
    try:
        key_count = len(read_authorized_keys())
        keys_line = f"{key_count} authorized client key{'' if key_count == 1 else 's'}"
        if key_count == 0:
            keys_line += " -- add one with: jb remote ssh key add"
    except (OSError, SSHKeyError) as exc:
        keys_line = f"authorized keys could not be read ({type(exc).__name__})"
    return "\n".join(
        [
            f"Jailbee SSH server listening on {_bind_display(config.listen, port)}",
            f"  entry points: {_enabled_entry_points(config)} (commands: {config.commands.mode})",
            f"  host key fingerprint: {fingerprint}",
            f"  {keys_line}",
            f"  connect example: {_connect_example(config.listen, port, config)}",
        ]
    )


def _shut_down(listener: asyncssh.SSHAcceptor, live: set[asyncssh.SSHServerConnection]) -> None:
    """Stop accepting connections and hang up every live one; the SIGTERM handler.

    Each connection's own session tasks observe the resulting disconnect
    through `process.wait_closed()` and run their existing HUP/grace/kill
    cleanup in pty.py; this function only initiates that, it does not wait
    for it.
    """
    listener.close()
    for conn in list(live):
        conn.close()


async def serve_async(config: RemoteSSHConfig) -> None:
    """Serve until listener shutdown, propagating bind and configuration errors."""
    # AsyncSSH logs complete commands at INFO and packet/input data at DEBUG.
    # This dedicated service supplies its own bounded audit fields instead.
    library_log = logging.getLogger("asyncssh")
    previous_level = library_log.level
    library_log.setLevel(max(previous_level, logging.WARNING))
    live: set[asyncssh.SSHServerConnection] = set()

    def server_factory() -> JailbeeSSHServer:
        return JailbeeSSHServer(live)

    try:
        listener = await asyncssh.listen(
            config.listen,
            config.port,
            server_factory=server_factory,
            process_factory=handle_process,
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
        log.info(_startup_summary(config, listener))
        loop = asyncio.get_running_loop()
        # `KillMode=process` in the unit leaves this SIGTERM handling to us,
        # so that `--background` workers (a different process group in the
        # same cgroup) survive `jb remote ssh restart`/`disable` and ordinary
        # `systemctl --user stop`.
        loop.add_signal_handler(signal.SIGTERM, _shut_down, listener, live)
        try:
            await listener.wait_closed()
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
            listener.close()
        log.info("Jailbee SSH server stopped")
    finally:
        library_log.setLevel(previous_level)


def serve(config: RemoteSSHConfig) -> None:
    """Synchronous entry point for the CLI and user service."""
    audit_handler = logging.StreamHandler()
    audit_handler.setLevel(logging.INFO)
    previous_level = log.level
    previous_propagate = log.propagate
    log.addHandler(audit_handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        asyncio.run(serve_async(config))
    finally:
        log.removeHandler(audit_handler)
        audit_handler.close()
        log.setLevel(previous_level)
        log.propagate = previous_propagate
