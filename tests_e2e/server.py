"""The e2e test server: the real `create_app(WebDeps)` under uvicorn in a thread.

Real: authentication, sessions, rate limits, origin guard, security headers,
the WebSocket protocol, static serving of the built browser app and the SQLite
engine (in a throwaway state directory). Faked through `WebDeps` fields only:
the snapshot source and the transcript file watch (both from
`tests.web_fakes`, which tests steer directly), plus whatever a test passes
as overrides (agent homes, clock, FIDO2).
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import uvicorn

from jailbee.web.app import create_app
from jailbee.web.auth import PinHolder
from jailbee.web.context import WebDeps
from tests.web_fakes import FakeSourceFactory, FakeWatchFactory, pin_sequence

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from jailbee.state_service.protocol import Snapshot

START_TIMEOUT = 10.0


def _listening_socket(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(128)
    return sock


@dataclass
class E2EServer:
    """One dashboard server on a fixed loopback port.

    `restart` builds a new app over the same deps on the same port, so the
    browser's origin (and its stored token) survives and the database keeps
    its sessions: what a real `jb remote web serve` restart looks like.
    """

    engine: Engine
    static_dir: Path
    overrides: dict[str, Any] = field(default_factory=dict)
    snapshots: FakeSourceFactory = field(default_factory=FakeSourceFactory)
    watches: FakeWatchFactory = field(default_factory=FakeWatchFactory)
    pin: PinHolder = field(default_factory=lambda: PinHolder(generate=pin_sequence()))
    port: int = 0
    _server: uvicorn.Server | None = None
    _thread: threading.Thread | None = None
    _sock: socket.socket | None = None
    _seq: int = 0

    @property
    def deps(self) -> WebDeps:
        base = WebDeps(
            snapshot_source=self.snapshots,
            engine=self.engine,
            pin=self.pin,
            static_dir=self.static_dir,
            file_watch=self.watches,
        )
        return replace(base, **self.overrides)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def push(self, snapshot: Snapshot) -> None:
        """Make ``snapshot`` the dashboard state every browser sees.

        Its ``seq`` is replaced by the next number, as the state service
        numbers its gathers: the server sends a browser only a new ``seq``.
        """
        self._seq += 1
        self.snapshots.push(replace(snapshot, seq=self._seq))

    def start(self) -> None:
        assert not self.running, "server already running"
        sock = _listening_socket(self.port)
        self.port = sock.getsockname()[1]
        config = uvicorn.Config(
            create_app(self.deps),
            log_level="warning",
            timeout_graceful_shutdown=1,
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, name="e2e-server", daemon=True
        )
        thread.start()
        deadline = time.monotonic() + START_TIMEOUT
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("the e2e server did not start")
            time.sleep(0.01)
        self._server, self._thread, self._sock = server, thread, sock

    def stop(self) -> None:
        if self._server is None or self._thread is None:
            return
        self._server.should_exit = True
        self._thread.join(timeout=START_TIMEOUT)
        if self._thread.is_alive():
            self._server.force_exit = True
            self._thread.join(timeout=START_TIMEOUT)
        if self._sock is not None:
            self._sock.close()
        self._server = self._thread = self._sock = None

    def restart(self) -> None:
        self.stop()
        self.start()
