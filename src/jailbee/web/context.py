"""What one web app instance holds: its dependencies and the objects built from them."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee.web.auth import Clock, PinHolder, RateLimiter, SessionStore, utcnow
from jailbee.web.auth_admin import TotpVerifier
from jailbee.web.fido2_auth import Fido2Auth
from jailbee.web.repo_sessions import (
    AgentHomes,
    RepoSessions,
    snapshot_homes,
    snapshot_repo_dirs,
)
from jailbee.web.session_info_store import DbSessionInfoStore
from jailbee.web.state import (
    DEFAULT_GRACE_SECONDS,
    DEFAULT_SNAPSHOT_WAIT_SECONDS,
    SnapshotSourceFactory,
    StateHub,
)
from jailbee.web.transcript_follow import TranscriptFollows
from jailbee.web.transcript_render import RenderCache, render_entries

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from jailbee.remote_ssh.repo_scope import RemoteRepoScope
    from jailbee.state_service.protocol import Snapshot
    from jailbee.web.file_watch import WatchFactory


log = logging.getLogger(__name__)


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
    fido2: Fido2Auth | None = None
    """The FIDO2 ceremony runner; None builds one over ``engine``."""
    auth_enabled: bool = True
    """False serves every route without a session (`remote.web.auth.enabled: false`).
    Never derived from the request's source address."""
    listen: str | None = None
    """The configured listen address. With login off, a request's `Host` must
    be loopback or this address (`web.origin_guard`)."""
    allow_unauthenticated_remote: bool = False
    """`remote.web.auth.allow_unauthenticated_remote`: with login off, answer any `Host`."""
    snapshot_wait_seconds: float = DEFAULT_SNAPSHOT_WAIT_SECONDS
    """How long a REST route waits for the first snapshot when no WebSocket
    holds the state-service connection (a deep link or a reload)."""
    file_watch: WatchFactory | None = None
    """Starts a watch on a followed transcript; None uses inotify (polling
    where it is unavailable)."""
    warm_session_cache: bool = False
    """Read every visible repository's session info in the background at
    start-up (`WebContext.start_warm_up`). The real server turns it on; tests
    that do not exercise it leave it off."""


@dataclass
class WebContext:
    """Per-app state, reachable from a route as ``request.app.state.web``."""

    deps: WebDeps
    sessions: SessionStore = field(init=False)
    limiter: RateLimiter = field(init=False)
    totp: TotpVerifier = field(init=False)
    fido2: Fido2Auth = field(init=False)
    hub: StateHub = field(init=False)
    repo_sessions: RepoSessions = field(init=False)
    render_cache: RenderCache = field(init=False)
    follows: TranscriptFollows = field(init=False)
    sessions_warmed: threading.Event = field(init=False)
    """Set once the start-up warm-up has finished (or was not asked for)."""
    _warm_stop: threading.Event = field(init=False)

    @property
    def scope(self) -> RemoteRepoScope:
        """The repo scope every REST and WebSocket output is filtered with."""
        from jailbee.remote_ssh.repo_scope import RemoteRepoScope

        return self.deps.repo_scope or RemoteRepoScope(frozenset())

    async def snapshot(self) -> Snapshot | None:
        """The latest snapshot, fetched on demand when no browser holds the hub."""
        return await self.hub.snapshot(self.deps.snapshot_wait_seconds)

    async def homes_ready(self) -> None:
        """Make sure the session homes resolve: they come from the snapshot
        unless `WebDeps.agent_homes` overrides them."""
        if self.deps.agent_homes is None:
            await self.snapshot()

    async def start_warm_up(self) -> None:
        """Start reading every visible repository's session info in a thread.

        Waits only for the first snapshot (which names the repositories); the
        reading itself runs on a thread of its own, never the request pool, so
        no request waits for it. A no-op unless `WebDeps.warm_session_cache`.
        """
        if not self.deps.warm_session_cache:
            self.sessions_warmed.set()
            return
        try:
            snapshot = await self.snapshot()
        except Exception:
            # A failed warm-up only costs a slower first list.
            log.debug("session warm-up skipped", exc_info=True)
            snapshot = None
        if snapshot is None:
            self.sessions_warmed.set()
            return
        prefixes = [g.prefix for g in snapshot.groups if self.scope.allows(g.prefix)]
        # Resolve against this snapshot: the hub may close its source before
        # the warm-up is done, and the live resolver would then know nothing.
        homes = self.deps.agent_homes or snapshot_homes(lambda: snapshot)
        dirs = None if self.deps.agent_homes else snapshot_repo_dirs(lambda: snapshot)

        def run() -> None:
            try:
                self.repo_sessions.warm(prefixes, homes, dirs, self._warm_stop)
            finally:
                self.sessions_warmed.set()

        threading.Thread(target=run, name="jailbee-web-warm", daemon=True).start()

    def stop_warm_up(self) -> None:
        """Ask the warm-up to stop after the repository it is reading."""
        self._warm_stop.set()

    def __post_init__(self) -> None:
        self.sessions_warmed = threading.Event()
        self._warm_stop = threading.Event()
        self.sessions = SessionStore(self.deps.engine, self.deps.clock)
        self.limiter = RateLimiter(self.deps.monotonic)
        self.totp = TotpVerifier(self.deps.engine, self.deps.clock)
        self.fido2 = self.deps.fido2 or Fido2Auth(
            self.deps.engine, clock=self.deps.clock, monotonic=self.deps.monotonic
        )
        self.hub = StateHub(self.deps.snapshot_source, grace_seconds=self.deps.grace_seconds)
        # Injected homes (tests) list everything in them; homes from the
        # snapshot list only the sessions started in the repo's own containers.
        self.repo_sessions = RepoSessions(
            self.deps.agent_homes or snapshot_homes(self.hub.latest),
            DbSessionInfoStore(self.deps.engine),
            None if self.deps.agent_homes else snapshot_repo_dirs(self.hub.latest),
        )
        self.render_cache = RenderCache()
        watch: WatchFactory
        if self.deps.file_watch is None:
            from jailbee.web.file_watch import watch_file

            watch = watch_file
        else:
            watch = self.deps.file_watch
        cache = self.render_cache
        self.follows = TranscriptFollows(watch, lambda entries: render_entries(entries, cache))
