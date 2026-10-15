"""Probe the registry mirror's egress lockdown from the host.

Three requests a locked-down mirror refuses and rpardini's stock config
serves (see `provision/registry-mirror/nginx.conf`):

- ``connect``: ``CONNECT example.com:443`` — stock tunnels it.
- ``host-header``: through a tunnel to an intercepted registry, ``GET /``
  with ``Host: example.com`` — stock fetches example.com.
- ``method``: same tunnel, ``PATCH /v2/`` — stock forwards it upstream.

A locked mirror answers all three itself, so the probes cause no egress.
``example.com`` rather than a ``.invalid`` name: an unpatched mirror would
fail to resolve ``.invalid`` and look locked. Plain ``socket``/``ssl``; no
subprocess (see CLAUDE.md).
"""

from __future__ import annotations

import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

_MARKER = b"jailbee-refused"
_REGISTRY = "registry-1.docker.io"


class ProbeOutcome(StrEnum):
    REFUSED = "refused"
    LEAKED = "leaked"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class ProbeResult:
    name: str
    outcome: ProbeOutcome
    detail: str


def _tls_wrap(sock: socket.socket, server_name: str) -> socket.socket:
    """TLS without verification: the probe plays a client that skips the MITM cert check."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx.wrap_socket(sock, server_hostname=server_name)


_MAX_BODY = 4096  # the marker is in the first line of our refusals


def _read_response(sock: socket.socket, *, want_body: bool = True) -> tuple[int, bytes]:
    """Status code and up to `_MAX_BODY` bytes of the body.

    A CONNECT reply has no body to wait for (``want_body=False``): after a
    200 the socket is the tunnel. Otherwise the body is read to its
    Content-Length, or until the peer closes (every probe request says
    ``Connection: close``).
    """
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    if not head:
        raise OSError("connection closed without a response")
    lines = head.split(b"\r\n")
    parts = lines[0].split()
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if not want_body:
        return status, b""
    length = _MAX_BODY
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length" and value.strip().isdigit():
            length = min(int(value.strip()), _MAX_BODY)
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return status, body[:length]


def _connect(host: str, port: int, timeout: float, target: str) -> tuple[socket.socket, int, bytes]:
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.sendall(f"CONNECT {target}:443 HTTP/1.1\r\nHost: {target}:443\r\n\r\n".encode())
    status, body = _read_response(sock, want_body=False)
    sock.settimeout(timeout)
    return sock, status, body


def _probe_connect(host: str, port: int, timeout: float) -> ProbeResult:
    sock, status, _ = _connect(host, port, timeout, "example.com")
    sock.close()
    if status == 200:
        return ProbeResult(
            "connect", ProbeOutcome.LEAKED, "CONNECT example.com:443 → tunnel opened"
        )
    return ProbeResult("connect", ProbeOutcome.REFUSED, f"CONNECT example.com:443 → {status}")


def _probe_in_tunnel(
    host: str,
    port: int,
    timeout: float,
    tls_wrap: Callable[[socket.socket, str], socket.socket],
    *,
    name: str,
    request: str,
    expect: int,
) -> ProbeResult:
    sock, status, _ = _connect(host, port, timeout, _REGISTRY)
    try:
        if status != 200:
            # The registry itself is not tunnelled: that is the mirror broken, not leaking.
            raise OSError(f"CONNECT {_REGISTRY}:443 → {status}")
        tls = tls_wrap(sock, _REGISTRY)
        tls.sendall(request.encode())
        got, body = _read_response(tls)
    finally:
        sock.close()
    line = request.split("\r\n", 1)[0]
    if got == expect and _MARKER in body:
        return ProbeResult(name, ProbeOutcome.REFUSED, f"{line} → {got}")
    return ProbeResult(name, ProbeOutcome.LEAKED, f"{line} → {got} (not refused by the mirror)")


def run_selftest(
    host: str,
    port: int,
    *,
    timeout: float = 5.0,
    tls_wrap: Callable[[socket.socket, str], socket.socket] = _tls_wrap,
) -> list[ProbeResult]:
    """Run the three probes against the mirror at ``host:port``."""
    probes: list[tuple[str, Callable[[], ProbeResult]]] = [
        ("connect", lambda: _probe_connect(host, port, timeout)),
        (
            "host-header",
            lambda: _probe_in_tunnel(
                host,
                port,
                timeout,
                tls_wrap,
                name="host-header",
                expect=403,
                request="GET / HTTP/1.1\r\nHost: example.com\r\nConnection: close\r\n\r\n",
            ),
        ),
        (
            "method",
            lambda: _probe_in_tunnel(
                host,
                port,
                timeout,
                tls_wrap,
                name="method",
                expect=405,
                request=(
                    f"PATCH /v2/ HTTP/1.1\r\nHost: {_REGISTRY}\r\n"
                    "Content-Length: 0\r\nConnection: close\r\n\r\n"
                ),
            ),
        ),
    ]
    results = []
    for name, probe in probes:
        try:
            results.append(probe())
        except (OSError, ssl.SSLError, ValueError) as e:
            results.append(ProbeResult(name, ProbeOutcome.UNREACHABLE, str(e) or type(e).__name__))
    return results


def verdict(results: list[ProbeResult]) -> bool | None:
    """True = locked down, False = any probe leaked, None = inconclusive."""
    outcomes = {r.outcome for r in results}
    if ProbeOutcome.LEAKED in outcomes:
        return False
    if ProbeOutcome.UNREACHABLE in outcomes:
        return None
    return True
