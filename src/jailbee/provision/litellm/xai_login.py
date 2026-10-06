"""Interactive callback transport only; LiteLLM owns the OAuth exchange and storage."""

from __future__ import annotations

import importlib
import os
import selectors
import socket
import sys
import termios
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

_LIMIT = 8192
_MAX_CONNECTIONS = 8


class LoginError(Exception):
    """A deliberately secret-free local login failure."""


class AuthorizationDenied(LoginError):
    """A validated callback reports a failed authorization."""


class CallbackServer(Protocol):
    expected_state: str
    server_address: tuple[str, int]
    socket: socket.socket

    def fileno(self) -> int: ...
    def server_close(self) -> None: ...


@contextmanager
def hidden_input(fd: int | None) -> Iterator[None]:
    terminal = None
    try:
        if fd is not None:
            try:
                terminal = termios.tcgetattr(fd)
            except termios.error:
                # Pipes have no terminal echo to disable; input still works.
                terminal = None
        if terminal is not None and fd is not None:
            hidden = terminal.copy()
            hidden[3] &= ~(termios.ECHO | termios.ECHONL)
            termios.tcsetattr(fd, termios.TCSANOW, hidden)
        yield
    finally:
        if terminal is not None and fd is not None:
            termios.tcsetattr(fd, termios.TCSANOW, terminal)


def _callback(
    value: str, server: CallbackServer, *, allow_error: bool = False
) -> dict[str, str | None]:
    try:
        url = urlsplit(value)
        params = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
        valid = (
            url.scheme == "http"
            and url.hostname == server.server_address[0]
            and url.port == server.server_address[1]
            and url.username is None
            and url.password is None
            and url.path == "/callback"
            and not url.fragment
            and all(len(v) == 1 for v in params.values())
            and params.get("state") == [server.expected_state]
        )
    except ValueError:
        valid = False
    if not valid:
        raise LoginError("Invalid xAI callback or state mismatch.")
    if allow_error and set(params) <= {"state", "error", "error_description"} and params.get("error", [""])[0]:
        raise AuthorizationDenied("xAI authorization failed.")
    if set(params) != {"code", "state"} or not params["code"][0]:
        raise LoginError("Invalid xAI callback.")
    return {"code": params["code"][0], "state": params["state"][0]}


def wait_for_callback(
    server: CallbackServer, input_fd: int = 0, *, timeout: float = 180
) -> dict[str, str | None]:
    """Race a hidden paste against the callback, with one monotonic deadline."""
    deadline = time.monotonic() + timeout
    connections: dict[socket.socket, bytes] = {}
    pasted = b""
    try:
        with hidden_input(input_fd), selectors.DefaultSelector() as selector:
            selector.register(server.socket, selectors.EVENT_READ)
            selector.register(input_fd, selectors.EVENT_READ)
            sys.stderr.write("Waiting for xAI callback; or paste the code here (hidden), then Enter. Ctrl-C cancels.\n")
            sys.stderr.flush()

            def close(connection: socket.socket) -> None:
                selector.unregister(connection)
                connections.pop(connection)
                connection.close()

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LoginError("Timed out waiting for xAI authorization.")
                events = selector.select(remaining)
                ready = [key.fd for key, _ in events]
                if server.fileno() in ready:
                    connection, _ = server.socket.accept()
                    connection.setblocking(False)
                    if len(connections) >= _MAX_CONNECTIONS:
                        connection.close()
                    else:
                        connections[connection] = b""
                        selector.register(connection, selectors.EVENT_READ)
                        # A nonblocking read checks callbacks before simultaneous stdin EOF.
                        ready.append(connection.fileno())
                for connection in list(connections):
                    if connection.fileno() not in ready:
                        continue
                    try:
                        chunk = connection.recv(_LIMIT + 1)
                    except BlockingIOError:
                        continue
                    except OSError:
                        close(connection)
                        continue
                    data = connections[connection] + chunk
                    if not chunk or len(data) > _LIMIT:
                        close(connection)
                        continue
                    connections[connection] = data
                    if b"\r\n\r\n" not in data:
                        continue
                    try:
                        method, target, version = data.split(b"\r\n", 1)[0].decode("ascii").split(" ")
                        if method != "GET" or not target.startswith("/") or version not in ("HTTP/1.0", "HTTP/1.1"):
                            raise LoginError("Invalid xAI callback.")
                        host, port = server.server_address
                        result = _callback(f"http://{host}:{port}{target}", server, allow_error=True)
                    except AuthorizationDenied:
                        raise
                    except (ValueError, LoginError):
                        close(connection)
                        continue
                    try:
                        connection.send(b"HTTP/1.1 200 OK\r\nContent-Length: 23\r\nConnection: close\r\n\r\nAuthorization received.")
                    except OSError:
                        pass
                    return result
                if input_fd in ready:
                    chunk = os.read(input_fd, _LIMIT + 1)
                    if not chunk:
                        raise LoginError("xAI login cancelled (input closed).")
                    pasted += chunk
                    if len(pasted) > _LIMIT:
                        raise LoginError("xAI input is too long.")
                    if b"\n" in pasted:
                        try:
                            value = pasted.split(b"\n", 1)[0].decode("ascii").strip()
                        except UnicodeError:
                            raise LoginError("Invalid xAI code.") from None
                        if "://" in value:
                            return _callback(value, server)
                        if not value or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in value):
                            raise LoginError("Invalid xAI code.")
                        # A bare code is explicitly provided for this pending PKCE session.
                        return {"code": value, "state": server.expected_state}
    finally:
        for connection in connections:
            connection.close()
        server.server_close()


def authenticator() -> Any:
    vendor = importlib.import_module("litellm.llms.xai.oauth")

    def wait(self: Any, server: CallbackServer) -> dict[str, str | None]:
        return wait_for_callback(server)

    # Override only the transport: inherited login keeps PKCE, state and atomic storage.
    cls = type("InteractiveXAIAuthenticator", (vendor.XAIOAuthAuthenticator,), {"_wait_for_callback": wait})
    return cls()


def main(factory: Callable[[], Any] = authenticator) -> int:
    try:
        try:
            fd = sys.stdin.fileno()
        except (OSError, ValueError):
            fd = None
        # Hide input before upstream prints the authorization URL, not just at the waiter.
        with hidden_input(fd):
            factory().login(no_browser=True)
    except KeyboardInterrupt:
        print("xAI login cancelled.", file=sys.stderr)
        return 130
    except LoginError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        # Upstream exceptions can contain token endpoint bodies or credentials.
        print("xAI login failed; retry the login with a fresh authorization code.", file=sys.stderr)
        return 1
    print("Logged in.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
