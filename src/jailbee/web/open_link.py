"""`jailbee remote web open`: the browser, already logged in through a one-time link.

Imports no FastAPI. The link is `/#/login?link=<token>`: the SPA reads the
query, exchanges the token (`POST /api/auth`, method `link`) and replaces the
history entry, so the token never stays in the address bar.
"""

from __future__ import annotations

import ipaddress
import socket
import webbrowser
from typing import TYPE_CHECKING

from jailbee.web import login_link
from jailbee.web.auth import Clock, utcnow
from jailbee.web.errors import ServerNotRunningError

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from jailbee.config.models_remote import RemoteWebConfig


def is_loopback(listen: str) -> bool:
    return ipaddress.ip_address(listen).is_loopback


def browser_host(listen: str) -> str:
    """The host a browser on this machine uses to reach a server bound to ``listen``."""
    address = ipaddress.ip_address(listen)
    if address.is_unspecified or address == ipaddress.ip_address("127.0.0.1"):
        return "localhost"
    return f"[{listen}]" if address.version == 6 else listen


def server_running(host: str, port: int, *, timeout: float = 1.0) -> bool:
    """Whether something accepts TCP connections at ``host``:``port``."""
    try:
        with socket.create_connection((host.strip("[]"), port), timeout=timeout):
            return True
    except OSError:
        return False


def open_dashboard(engine: Engine, web: RemoteWebConfig, *, clock: Clock = utcnow) -> str:
    """Open the dashboard in the default browser and return the plain address.

    Raises `ServerNotRunningError` rather than open a dead page.
    """
    host = browser_host(web.listen)
    base = f"http://{host}:{web.port}/"
    if not server_running(host, web.port):
        raise ServerNotRunningError(base)
    if web.auth.enabled:
        url = f"{base}#/login?link={login_link.mint(engine, clock=clock)}"
    else:
        url = base
    if not webbrowser.open(url):
        raise OSError(f"No browser could be opened. Open {base} yourself and log in with the PIN.")
    return base
