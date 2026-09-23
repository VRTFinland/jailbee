"""Public-key authentication and restricted Jailbee sessions over AsyncSSH."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import shlex
import signal
import sys
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

import asyncssh

from jailbee.config import ConfigError
from jailbee.db import state_dir
from jailbee.global_config import default_global_config_path, load_global_config
from jailbee.remote_ssh.keys import AuthorizedKey, SSHKeyError, read_authorized_keys, ssh_paths
from jailbee.remote_ssh.overrides import ServeOverrides, apply_ssh_overrides, describe_overrides
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


_LONE_LF_RE = re.compile(r"(?<!\r)\n")


def _server_text(text: str, *, pty: bool) -> bytes:
    """Encode text this server writes itself, translating LF to CRLF over a PTY.

    A PTY session has no intervening kernel pty applying ONLCR for us (unlike
    child process output relayed through `pty.py`, which runs behind a real
    pty and needs no such translation): the client terminal is left raw, so a
    bare "\\n" produces no carriage return and lines stack up diagonally
    (e.g. a commandless `ssh -t ...` login's help text). A non-PTY session
    (`ssh -T ...`, or a one-shot command) keeps output byte-exact.
    """
    if pty:
        text = _LONE_LF_RE.sub("\r\n", text)
    return text.encode("utf-8")


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


async def handle_process(
    process: asyncssh.SSHServerProcess[bytes], overrides: ServeOverrides | None = None
) -> None:
    """Reload channel policy, dispatch one route, and audit its final outcome.

    `overrides` (from `jb remote ssh serve`'s command-line flags) is
    reapplied on top of every fresh `load_global_config` reload below, so a
    given flag always wins even after a `global.yaml` edit lands mid-run.
    `None` (the default, used by the systemd service and every plain
    `serve()` call) skips this entirely — identical to before overrides
    existed. A rejected merge raises `ConfigError`, handled the same as a
    broken `global.yaml` already is, just below.
    """
    kind, prefix, path = "unknown", None, None
    decision = "rejected"
    reason = "completed"
    outcome: int | str | None = None
    original_exit = process.exit
    original_signal = process.exit_with_signal
    # Known as soon as the channel opens (a pty request precedes any exec/shell
    # request in the SSH protocol), so this is safe to read anywhere below,
    # including the exception handlers around routing/config failures.
    pty = process.term_type is not None

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
        if overrides is not None:
            config = apply_ssh_overrides(config, overrides)
        selected = route(process.command, config)
        kind, prefix = selected.kind, selected.repo_prefix
        if selected.requires_pty and process.term_type is None:
            raise PTYError("This entry point requires a PTY; retry with ssh -t.")
        decision = "allowed"
        if selected.kind == "help":
            process.stdout.write(_server_text(help_text(config), pty=pty))
            process.exit(0)
            return
        argv = (sys.executable, "-m", "jailbee", *selected.argv)
        if selected.kind == "console":
            # The console child re-validates this itself (never trusting it
            # as-is) via `RemoteSSHConfig.model_validate_json` — see
            # `console._load_policy`. Passing the already-merged `config`
            # (global.yaml + any `jb remote ssh serve` overrides) here is
            # what fixes the console silently reloading global.yaml on its
            # own and ignoring every override flag (e.g. `--commands full`,
            # `--shell`), including its own `dashboard` check.
            argv = (*argv, "--policy-json", config.model_dump_json())
        spec = ChildSpec(
            argv=argv,
            cwd=selected.repo_root or state_dir(),
            requires_pty=selected.requires_pty,
        )
        if selected.repo_root is None:
            spec.cwd.mkdir(parents=True, exist_ok=True)
        audit("started")
        await run_child(process, spec)
    except (ConfigError, RouteError, PTYError) as exc:
        reason = type(exc).__name__
        process.stderr.write(_server_text(str(exc) + "\n", pty=pty))
        process.exit(2)
    except ConnectionError as exc:
        reason = type(exc).__name__
    except asyncio.CancelledError:
        reason = "cancelled"
        raise
    except Exception as exc:
        # Exceptions from child setup may include argv or other private data.
        reason = type(exc).__name__
        process.stderr.write(_server_text("Remote Jailbee session failed.\n", pty=pty))
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


def _startup_summary(
    config: RemoteSSHConfig,
    listener: asyncssh.SSHAcceptor,
    overrides: ServeOverrides | None = None,
) -> str:
    """Describe the running listener for the audit log, without secrets.

    The port comes from the listener itself (not `config.port`) so a
    configured port of 0 is reported as the port the kernel actually chose.
    `overrides`, when given and non-empty, adds one extra line naming the
    command-line flags that are not (only) coming from `global.yaml`.
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
    lines = [
        f"Jailbee SSH server listening on {_bind_display(config.listen, port)}",
        f"  entry points: {_enabled_entry_points(config)} (commands: {config.commands.mode})",
        f"  host key fingerprint: {fingerprint}",
        f"  {keys_line}",
        f"  connect example: {_connect_example(config.listen, port, config)}",
    ]
    overrides_line = describe_overrides(overrides) if overrides is not None else None
    if overrides_line is not None:
        lines.append(f"  {overrides_line}")
    return "\n".join(lines)


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


async def serve_async(config: RemoteSSHConfig, overrides: ServeOverrides | None = None) -> None:
    """Serve until listener shutdown, propagating bind and configuration errors.

    `overrides` is bind-time only for `config.listen`/`config.port` (the
    caller already merged those into `config` before this runs — a bound
    listener cannot move itself). For every other field it is carried into
    `handle_process` so it is reapplied on each session's own
    `load_global_config` reload; see `handle_process`.
    """
    # AsyncSSH logs complete commands at INFO and packet/input data at DEBUG.
    # This dedicated service supplies its own bounded audit fields instead.
    library_log = logging.getLogger("asyncssh")
    previous_level = library_log.level
    library_log.setLevel(max(previous_level, logging.WARNING))
    live: set[asyncssh.SSHServerConnection] = set()

    def server_factory() -> JailbeeSSHServer:
        return JailbeeSSHServer(live)

    process_factory: Callable[[asyncssh.SSHServerProcess[bytes]], Coroutine[Any, Any, None]]
    if overrides is None or overrides.is_empty():
        process_factory = handle_process
    else:

        def _process_factory(
            process: asyncssh.SSHServerProcess[bytes],
        ) -> Coroutine[Any, Any, None]:
            return handle_process(process, overrides)

        process_factory = _process_factory

    try:
        listener = await asyncssh.listen(
            config.listen,
            config.port,
            server_factory=server_factory,
            process_factory=process_factory,
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
        log.info(_startup_summary(config, listener, overrides))
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


def serve(config: RemoteSSHConfig, overrides: ServeOverrides | None = None) -> None:
    """Synchronous entry point for the CLI and user service.

    The unit's `ExecStart` never passes `overrides`; only `jb remote ssh
    serve`'s own CLI flags build one.
    """
    audit_handler = logging.StreamHandler()
    audit_handler.setLevel(logging.INFO)
    previous_level = log.level
    previous_propagate = log.propagate
    log.addHandler(audit_handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        try:
            asyncio.run(serve_async(config, overrides))
        except KeyboardInterrupt:
            # No custom SIGINT handler is installed (only SIGTERM, above), so
            # asyncio.run's default cancellation-on-interrupt path runs the
            # listener/connection cleanup already registered there, then
            # re-raises this once foreground Ctrl-C. Convert it to a clean
            # exit instead of the interpreter's default KeyboardInterrupt
            # traceback and non-conventional exit code.
            log.info("Jailbee SSH server stopped (Ctrl-C)")
            raise SystemExit(130) from None
    finally:
        log.removeHandler(audit_handler)
        audit_handler.close()
        log.setLevel(previous_level)
        log.propagate = previous_propagate
