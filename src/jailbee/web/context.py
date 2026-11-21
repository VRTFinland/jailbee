"""What one web app instance holds: its dependencies and the objects built from them."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee.web.auth import Clock, PinHolder, RateLimiter, SessionStore, utcnow
from jailbee.web.auth_admin import TotpVerifier
from jailbee.web.repo_sessions import AgentHomes, RepoSessions, snapshot_homes
from jailbee.web.state import DEFAULT_GRACE_SECONDS, SnapshotSourceFactory, StateHub

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from jailbee.remote_ssh.repo_scope import RemoteRepoScope


@dataclass(frozen=True)
class WebDeps:
    """Everything `create_app` needs from outside. Tests pass fakes.

    New slices add fields with defaults (a TOTP replay cache, a FIDO2
    server, the auth-disabled switch), so existing callers keep working.
    """

    snapshot_source: SnapshotSourceFactory
    engine: Engine
    pin: PinHolder
    clock: Clock = utcnow
    monotonic: Callable[[], float] = time.monotonic
    static_dir: Path | None = None
    grace_seconds: float = DEFAULT_GRACE_SECONDS
    repo_scope: RemoteRepoScope | None = None
    """Which repositories a browser may see (`remote.web.excluded_repos`).
    None means every repository; the real server always passes one."""
    agent_homes: AgentHomes | None = None
    """Repo prefix → ``(agent, config home)`` pairs whose transcripts the session
    list reads. None derives them from the live snapshot."""


@dataclass
class WebContext:
    """Per-app state, reachable from a route as ``request.app.state.web``."""

    deps: WebDeps
    sessions: SessionStore = field(init=False)
    limiter: RateLimiter = field(init=False)
    totp: TotpVerifier = field(init=False)
    hub: StateHub = field(init=False)
    repo_sessions: RepoSessions = field(init=False)

    @property
    def scope(self) -> RemoteRepoScope:
        """The repo scope every REST and WebSocket output is filtered with."""
        from jailbee.remote_ssh.repo_scope import RemoteRepoScope

        return self.deps.repo_scope or RemoteRepoScope(frozenset())

    def __post_init__(self) -> None:
        self.sessions = SessionStore(self.deps.engine, self.deps.clock)
        self.limiter = RateLimiter(self.deps.monotonic)
        self.totp = TotpVerifier(self.deps.engine, self.deps.clock)
        self.hub = StateHub(self.deps.snapshot_source, grace_seconds=self.deps.grace_seconds)
        self.repo_sessions = RepoSessions(self.deps.agent_homes or snapshot_homes(self.hub.latest))
