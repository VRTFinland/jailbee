"""Cross-origin defences, as one ASGI middleware around the dashboard app.

Two checks, both on headers a browser sets and a page cannot forge:

- **`Origin`** (WebSockets and state-changing HTTP): a request whose `Origin` names another
  host:port than the request's `Host` is refused, so a page on another site
  cannot open `/api/ws` or change state through the user's browser, even when
  login is disabled. A request without `Origin` comes from a non-browser client
  and is let through. A reverse proxy
  passes the public `Host` and `Origin` (docs/web-dashboard.md), so the two
  still match there.
- **`Host` with login off** (DNS rebinding): every request whose `Host` is
  not a loopback name or address, or the configured listen address, is
  refused, unless `remote.web.auth.allow_unauthenticated_remote` opted in to
  remote access. With login on a rebound page gains nothing (it holds no
  token), so the check is off.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


@dataclass(frozen=True)
class HostPolicy:
    """Which `Host` values the server answers; None accepts any."""

    allowed_hosts: frozenset[str] | None

    @classmethod
    def of(cls, *, auth_enabled: bool, listen: str | None, allow_remote: bool) -> HostPolicy:
        if auth_enabled or allow_remote:
            return cls(None)
        extra = {listen.lower()} if listen else set()
        return cls(frozenset(LOOPBACK_NAMES | extra))

    def allows(self, host_header: str | None) -> bool:
        if self.allowed_hosts is None:
            return True
        split = _split_host(host_header)
        if split is None:
            return False
        name = split[0]
        if name in self.allowed_hosts:
            return True
        try:
            return ipaddress.ip_address(name).is_loopback
        except ValueError:
            return False


def _split_host(value: str | None) -> tuple[str, int | None] | None:
    """``host[:port]`` → (lower-case host without brackets, port or None)."""
    if not value:
        return None
    try:
        parts = urlsplit(f"//{value}")
        return (parts.hostname or "").lower(), parts.port
    except ValueError:
        return None


def origin_matches(origin: str, host_header: str | None) -> bool:
    """Whether ``origin`` (a URL) names the same host and port as ``host_header``."""
    try:
        parts = urlsplit(origin)
        origin_port = parts.port
    except ValueError:
        return False
    default = _DEFAULT_PORTS.get(parts.scheme.lower())
    if not parts.hostname or default is None:
        return False
    host = _split_host(host_header)
    if host is None:
        return False
    return (parts.hostname.lower(), origin_port or default) == (host[0], host[1] or default)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return bytes(value).decode("latin-1")
    return None


class OriginGuard:
    """Refuse cross-origin writes/handshakes and, with login off, foreign `Host`s."""

    def __init__(self, app: ASGIApp, policy: HostPolicy) -> None:
        self._app = app
        self._policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return
        host = _header(scope, b"host")
        allowed = self._policy.allows(host)
        if allowed and (
            kind == "websocket" or scope.get("method") not in ("GET", "HEAD", "OPTIONS")
        ):
            origin = _header(scope, b"origin")
            allowed = origin is None or origin_matches(origin, host)
        if allowed:
            await self._app(scope, receive, send)
        elif kind == "websocket":
            # A close before accept is an HTTP 403 to the browser.
            await send({"type": "websocket.close", "code": 1008, "reason": "Origin not allowed"})
        else:
            from starlette.responses import PlainTextResponse

            await PlainTextResponse("Host not allowed", status_code=403)(scope, receive, send)
