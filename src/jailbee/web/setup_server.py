"""Run the FIDO2 setup mode in the foreground: the setup app, uvicorn, the setup PIN on stdout.

`jailbee remote web auth fido2 setup` calls `serve_setup`. It binds the
dashboard's own address by default: a key is bound to the domain it was
registered at, so the browser must reach this app through the same reverse
proxy (and domain) it later logs in through. The dashboard itself must
therefore be stopped while setup runs on its port.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import uvicorn

from jailbee.db import get_engine
from jailbee.web import assets, auth_admin
from jailbee.web.auth import PinHolder, RotationReason
from jailbee.web.errors import FrontendNotBuiltError
from jailbee.web.server import server_url
from jailbee.web.setup_app import SETUP_VIEW, SetupDeps, create_setup_app

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.config.models_remote import RemoteWebConfig

Echo = Callable[[str], None]


def serve_setup(config: RemoteWebConfig, *, echo: Echo, static_dir: Path | None = None) -> None:
    """Serve the setup app on ``config.listen``:``config.port`` until interrupted.

    Raises `FrontendNotBuiltError` before binding anything.
    """
    directory = static_dir if static_dir is not None else assets.default_static_dir()
    if not assets.frontend_built(directory):
        raise FrontendNotBuiltError(directory)

    def announce(new: str, why: RotationReason) -> None:
        lead = "Wrong setup PIN entered." if why == "wrong guess" else "Setup PIN used."
        echo(f"{lead} New setup PIN: {new}")

    pin = PinHolder(on_rotate=announce)
    app = create_setup_app(SetupDeps(engine=get_engine(), pin=pin, static_dir=directory))
    echo(f"JailBee FIDO2 setup mode on {server_url(config.listen, config.port)}")
    echo(
        f"Open the dashboard's public HTTPS address with /{SETUP_VIEW} "
        "(the domain you will log in at; a key works only there)."
    )
    echo(f"Setup PIN: {pin.current}")
    echo("The dashboard must not be running on this port. Ctrl-C ends setup mode.")
    http = uvicorn.Server(
        uvicorn.Config(app, host=config.listen, port=config.port, log_level="info")
    )
    with auth_admin.audit_logging():
        http.run()
