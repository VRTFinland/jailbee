"""Run the web dashboard in the foreground: real dependencies, uvicorn, the PIN on stdout."""

from __future__ import annotations

import ipaddress
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

import uvicorn

from jailbee import __version__
from jailbee.db import get_engine
from jailbee.remote_ssh.repo_scope import RemoteRepoScope
from jailbee.remote_ssh.running import installed_version as _installed_version
from jailbee.web import assets, auth_admin, open_link
from jailbee.web.app import WebDeps, create_app
from jailbee.web.auth import PinHolder, RotationReason
from jailbee.web.errors import (
    FrontendNotBuiltError,
    ServiceUpdatedError,
    UnauthenticatedBindError,
)
from jailbee.web.state import state_client_source
from jailbee.web.update_watch import watch_for_update

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.config.models_remote import RemoteWebConfig

Echo = Callable[[str], None]


def server_url(listen: str, port: int) -> str:
    host = f"[{listen}]" if ipaddress.ip_address(listen).version == 6 else listen
    return f"http://{host}:{port}/"


VERSION_POLL_SECONDS = 30.0


def serve(
    config: RemoteWebConfig,
    *,
    echo: Echo,
    static_dir: Path | None = None,
    installed_version: Callable[[], str | None] = _installed_version,
    poll_interval: float = VERSION_POLL_SECONDS,
) -> None:
    """Serve until interrupted, or until JailBee is upgraded under the server.

    Raises `FrontendNotBuiltError` before binding anything, and
    `ServiceUpdatedError` after a clean shutdown when the installed version
    no longer matches the one this process started from.
    """
    directory = static_dir if static_dir is not None else assets.default_static_dir()
    if not assets.frontend_built(directory):
        raise FrontendNotBuiltError(directory)

    def announce(new: str, why: RotationReason) -> None:
        lead = "Wrong PIN entered." if why == "wrong guess" else "PIN used."
        echo(f"{lead} New PIN: {new}")

    if not config.auth.enabled:
        if (
            not open_link.is_loopback(config.listen)
            and not config.auth.allow_unauthenticated_remote
        ):
            raise UnauthenticatedBindError(config.listen)
        echo(
            "WARNING: login is off (remote.web.auth.enabled: false). Anyone who can reach "
            f"port {config.port} sees the dashboard; a reverse proxy or SSH tunnel to the port "
            "bypasses auth."
        )
    pin = PinHolder(on_rotate=announce)
    deps = WebDeps(
        snapshot_source=state_client_source,
        engine=get_engine(),
        pin=pin,
        auth_enabled=config.auth.enabled,
        static_dir=directory,
        repo_scope=RemoteRepoScope.of(config),
    )
    app = create_app(deps)
    echo(f"JailBee web dashboard: {server_url(config.listen, config.port)}")
    if config.auth.enabled:
        if auth_admin.pin_enabled(deps.engine):
            echo(f"PIN: {pin.current}")
        else:
            echo(
                "PIN login is off. To allow it: `jailbee remote web auth pin on`, "
                "then restart the server to see a PIN."
            )
    http = uvicorn.Server(
        uvicorn.Config(app, host=config.listen, port=config.port, log_level="info")
    )

    def stop_server(_new_version: str) -> None:
        http.should_exit = True

    finished = threading.Event()
    replaced: list[str] = []

    def watch() -> None:
        new = watch_for_update(
            __version__,
            stop_server,
            installed=installed_version,
            wait=finished.wait,
            interval=poll_interval,
        )
        if new is not None:
            replaced.append(new)

    watcher = threading.Thread(target=watch, name="web-version-watch", daemon=True)
    watcher.start()
    try:
        with auth_admin.audit_logging():
            http.run()
    finally:
        finished.set()
        watcher.join()
    if replaced:
        raise ServiceUpdatedError(__version__, replaced[0])
