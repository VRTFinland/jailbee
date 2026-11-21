"""Run the web dashboard in the foreground: real dependencies, uvicorn, the PIN on stdout."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from typing import TYPE_CHECKING

import uvicorn

from jailbee.db import get_engine
from jailbee.web import assets
from jailbee.web.app import WebDeps, create_app
from jailbee.web.auth import PinHolder, RotationReason
from jailbee.web.errors import FrontendNotBuiltError
from jailbee.web.state import state_client_source

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.config.models_remote import RemoteWebConfig

Echo = Callable[[str], None]


def server_url(listen: str, port: int) -> str:
    host = f"[{listen}]" if ipaddress.ip_address(listen).version == 6 else listen
    return f"http://{host}:{port}/"


def serve(config: RemoteWebConfig, *, echo: Echo, static_dir: Path | None = None) -> None:
    """Serve until interrupted. Raises `FrontendNotBuiltError` before binding anything."""
    directory = static_dir if static_dir is not None else assets.default_static_dir()
    if not assets.frontend_built(directory):
        raise FrontendNotBuiltError(directory)

    def announce(new: str, why: RotationReason) -> None:
        lead = "Wrong PIN entered." if why == "wrong guess" else "PIN used."
        echo(f"{lead} New PIN: {new}")

    if not config.auth.enabled:
        # Serving without login (with its loopback-only guard) is a later slice;
        # until then the setting can only make things stricter, never looser.
        echo("remote.web.auth.enabled is false, but this version always requires a login.")
    pin = PinHolder(on_rotate=announce)
    deps = WebDeps(
        snapshot_source=state_client_source,
        engine=get_engine(),
        pin=pin,
        static_dir=directory,
    )
    app = create_app(deps)
    echo(f"JailBee web dashboard: {server_url(config.listen, config.port)}")
    echo(f"PIN: {pin.current}")
    uvicorn.run(app, host=config.listen, port=config.port, log_level="info")
