"""Probes for the registry mirror lockdown, run against a fake mirror.

The fake speaks plain HTTP after CONNECT (tls_wrap is injected as identity),
so each test controls exactly what the "mirror" answers.
"""

import socket
import threading
from collections.abc import Callable

import pytest

from jailbee.registry_selftest import ProbeOutcome, run_selftest, verdict


def _plain(sock: socket.socket, _host: str) -> socket.socket:
    return sock


def _serve(handler: Callable[[bytes], bytes | None]):
    """One-request-per-connection fake. `handler(request_head)` returns the
    reply to send, or None to hang (never answer)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    stop = threading.Event()

    def loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            threading.Thread(target=_conn, args=(conn,), daemon=True).start()

    def _conn(conn):
        with conn:
            conn.settimeout(3)
            try:
                while True:
                    head = b""
                    while b"\r\n\r\n" not in head:
                        chunk = conn.recv(4096)
                        if not chunk:
                            return
                        head += chunk
                    reply = handler(head)
                    if reply is None:
                        stop.wait(3)
                        return
                    conn.sendall(reply)
                    if not head.startswith(b"CONNECT "):
                        return
            except OSError:
                return

    threading.Thread(target=loop, daemon=True).start()
    return srv, stop


@pytest.fixture
def fake_mirror():
    servers = []

    def start(handler):
        srv, stop = _serve(handler)
        servers.append((srv, stop))
        return srv.getsockname()

    yield start
    for srv, stop in servers:
        stop.set()
        srv.close()


OK_TUNNEL = b"HTTP/1.1 200 Connection Established\r\n\r\n"


def _locked(head: bytes) -> bytes:
    if head.startswith(b"CONNECT example.com:443"):
        return b"HTTP/1.1 403 Forbidden\r\nContent-Length: 16\r\n\r\njailbee-refused\n"
    if head.startswith(b"CONNECT "):
        return OK_TUNNEL
    if b"Host: example.com" in head:
        return b"HTTP/1.1 403 Forbidden\r\nContent-Length: 16\r\n\r\njailbee-refused\n"
    if head.startswith(b"PATCH "):
        return b"HTTP/1.1 405 Not Allowed\r\nContent-Length: 16\r\n\r\njailbee-refused\n"
    return b"HTTP/1.1 500 X\r\nContent-Length: 0\r\n\r\n"


def _by_name(results):
    return {r.name: r.outcome for r in results}


def test_locked_mirror_refuses_all_three(fake_mirror):
    host, port = fake_mirror(_locked)

    results = run_selftest(host, port, timeout=2, tls_wrap=_plain)

    assert _by_name(results) == {
        "connect": ProbeOutcome.REFUSED,
        "host-header": ProbeOutcome.REFUSED,
        "method": ProbeOutcome.REFUSED,
    }
    assert verdict(results) is True


def test_open_tunnel_is_a_leak(fake_mirror):
    """rpardini stock: CONNECT example.com gets 200."""
    host, port = fake_mirror(lambda h: OK_TUNNEL if h.startswith(b"CONNECT ") else _locked(h))

    results = run_selftest(host, port, timeout=2, tls_wrap=_plain)

    assert _by_name(results)["connect"] is ProbeOutcome.LEAKED
    assert verdict(results) is False


def test_host_header_forwarded_is_a_leak(fake_mirror):
    """rpardini stock proxies to whatever Host says: example.com answers 200."""

    def handler(head):
        if b"Host: example.com" in head and not head.startswith(b"CONNECT "):
            return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi"
        return _locked(head)

    host, port = fake_mirror(handler)

    assert _by_name(run_selftest(host, port, timeout=2, tls_wrap=_plain))["host-header"] is (
        ProbeOutcome.LEAKED
    )


def test_method_probe_needs_the_marker_not_just_405(fake_mirror):
    """An unpatched mirror forwards PATCH to Docker Hub, which may itself say
    405. Only our marker proves the mirror refused it."""

    def handler(head):
        if head.startswith(b"PATCH "):
            return b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n"
        return _locked(head)

    host, port = fake_mirror(handler)

    assert _by_name(run_selftest(host, port, timeout=2, tls_wrap=_plain))["method"] is (
        ProbeOutcome.LEAKED
    )


def test_refused_connect_without_marker_still_counts(fake_mirror):
    """The dead-address fallback gives a bare 502: no tunnel is what matters."""

    def handler(head):
        if head.startswith(b"CONNECT example.com"):
            return b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n"
        return _locked(head)

    host, port = fake_mirror(handler)

    assert _by_name(run_selftest(host, port, timeout=2, tls_wrap=_plain))["connect"] is (
        ProbeOutcome.REFUSED
    )


def test_nothing_listening_is_unreachable():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    results = run_selftest("127.0.0.1", port, timeout=1, tls_wrap=_plain)

    assert {r.outcome for r in results} == {ProbeOutcome.UNREACHABLE}
    assert verdict(results) is None


def test_probe_times_out_as_unreachable(fake_mirror):
    """Accepts TCP, never answers: doctor must not hang."""
    host, port = fake_mirror(lambda _h: None)

    results = run_selftest(host, port, timeout=0.5, tls_wrap=_plain)

    assert {r.outcome for r in results} == {ProbeOutcome.UNREACHABLE}


def test_leak_wins_over_unreachable():
    from jailbee.registry_selftest import ProbeResult

    results = [
        ProbeResult("connect", ProbeOutcome.LEAKED, ""),
        ProbeResult("method", ProbeOutcome.UNREACHABLE, ""),
    ]
    assert verdict(results) is False
