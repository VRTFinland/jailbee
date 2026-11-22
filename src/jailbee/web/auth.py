"""Web dashboard login: the one-time PIN, session tokens and the guess limit.

Ported from codereader's `auth.py` (ADR 0005) and adapted: no module state
(the PIN lives in a `PinHolder` the app is given), an injected clock, and
sessions in JailBee's own database (`db.models.WebSession`), stored as token
hashes with a device name.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from sqlmodel import Session, col, delete, select

from jailbee.db.models import WebSession

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

SESSION_TTL = timedelta(days=30)
"""How long a session lives after its last use (sliding)."""

Clock = Callable[[], datetime]
"""Returns the current time, timezone-aware UTC."""

RotationReason = Literal["login", "wrong guess"]


def utcnow() -> datetime:
    return datetime.now(UTC)


def random_pin() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


class PinHolder:
    """The current one-time PIN.

    A PIN is spent by any attempt: a correct one logs in, a wrong one is
    refused. Either way a new PIN replaces it and ``on_rotate`` announces it
    (the server prints it), so a guesser never gets a second try at the same
    PIN. Thread-safe: login requests run in a thread pool.
    """

    def __init__(
        self,
        *,
        generate: Callable[[], str] = random_pin,
        on_rotate: Callable[[str, RotationReason], None] | None = None,
    ) -> None:
        self._generate = generate
        self._on_rotate = on_rotate
        self._lock = threading.Lock()
        self._current = generate()

    @property
    def current(self) -> str:
        with self._lock:
            return self._current

    def attempt(self, guess: str) -> bool:
        """Spend the PIN on ``guess``; True when it matched."""
        with self._lock:
            ok = secrets.compare_digest(guess.encode(), self._current.encode())
            new = self._generate()
            while new == self._current:
                new = self._generate()
            self._current = new
        if self._on_rotate is not None:
            self._on_rotate(new, "login" if ok else "wrong guess")
        return ok


@dataclass(frozen=True)
class SessionInfo:
    """A logged-in device, as the API shows it. Never carries the token."""

    id: int
    device_name: str
    created_at: datetime
    last_used_at: datetime


def token_hash(token: str) -> str:
    """The stored form of a bearer secret (session token, login link): its SHA-256 hex digest."""
    return hashlib.sha256(token.encode()).hexdigest()


def _info(row: WebSession) -> SessionInfo:
    assert row.id is not None
    return SessionInfo(row.id, row.device_name, row.created_at, row.last_used_at)


class SessionStore:
    """Session tokens in the state database, 30 days from last use."""

    def __init__(self, engine: Engine, clock: Clock, ttl: timedelta = SESSION_TTL) -> None:
        self._engine = engine
        self._clock = clock
        self._ttl = ttl

    def create(self, device_name: str) -> str:
        """A new session for ``device_name``; returns its bearer token."""
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with Session(self._engine) as s:
            s.add(
                WebSession(
                    token_hash=token_hash(token),
                    device_name=device_name,
                    created_at=now,
                    last_used_at=now,
                )
            )
            s.commit()
        return token

    def validate(self, token: str) -> SessionInfo | None:
        """The session ``token`` names, its expiry slid forward; None if none.

        An expired session is deleted, so it stays dead even if the clock
        moves back.
        """
        if not token:
            return None
        now = self._clock()
        with Session(self._engine) as s:
            row = s.exec(
                select(WebSession).where(WebSession.token_hash == token_hash(token))
            ).first()
            if row is None:
                return None
            if now - row.last_used_at > self._ttl:
                s.delete(row)
                s.commit()
                return None
            row.last_used_at = max(now, row.last_used_at)
            s.add(row)
            s.commit()
            s.refresh(row)
            return _info(row)

    def is_active(self, token: str) -> bool:
        """Whether ``token`` still names a live session, without sliding its expiry.

        An open WebSocket asks this before each send, so a session revoked
        from the CLI (another process) stops receiving data.
        """
        cutoff = self._clock() - self._ttl
        with Session(self._engine) as s:
            row = s.exec(
                select(WebSession).where(WebSession.token_hash == token_hash(token))
            ).first()
            return row is not None and row.last_used_at >= cutoff

    def revoke(self, token: str) -> None:
        with Session(self._engine) as s:
            s.exec(delete(WebSession).where(col(WebSession.token_hash) == token_hash(token)))
            s.commit()

    def purge_expired(self) -> None:
        cutoff = self._clock() - self._ttl
        with Session(self._engine) as s:
            s.exec(delete(WebSession).where(col(WebSession.last_used_at) < cutoff))
            s.commit()


BACKOFF_THRESHOLD = 5
"""Consecutive failures of one method before a wait is imposed."""

BACKOFF_BASE = 2.0
"""The wait after the ``BACKOFF_THRESHOLD``-th failure, in seconds; each further one doubles it."""

BACKOFF_CAP = 1800.0
"""The longest wait between attempts, in seconds (30 minutes)."""


@dataclass
class _Bucket:
    attempts: deque[float]
    failures: int = 0
    blocked_until: float = 0.0


class RateLimiter:
    """Login attempt limits per bucket (a login method): a window and a backoff.

    At most ``max_attempts`` attempts per sliding ``window`` seconds. On top of
    that, from the ``backoff_threshold``-th consecutive failure on, the next
    attempt waits ``backoff_base`` seconds, doubling with each further failure
    up to ``backoff_cap``; a success resets it. Attempts refused by either
    limit are not counted and must not be checked, so they buy no guesses.
    An admitted attempt holds the bucket for the wait its failure would earn
    until ``failed`` or ``succeeded`` settles it, so parallel requests cannot
    share one opening.

    Each bucket counts on its own, so guessing one method never locks out
    another. Rotation already makes each PIN guess a one-in-a-million shot;
    this bounds how many such shots a month allows. State lives in memory: a
    server restart (an upgrade, under systemd) starts every bucket afresh.
    """

    def __init__(
        self,
        monotonic: Callable[[], float],
        *,
        max_attempts: int = 5,
        window: float = 60.0,
        backoff_threshold: int = BACKOFF_THRESHOLD,
        backoff_base: float = BACKOFF_BASE,
        backoff_cap: float = BACKOFF_CAP,
    ) -> None:
        self._monotonic = monotonic
        self._max = max_attempts
        self._window = window
        self._threshold = backoff_threshold
        self._base = backoff_base
        self._cap = backoff_cap
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def _bucket(self, bucket: str) -> _Bucket:
        return self._buckets.setdefault(bucket, _Bucket(deque()))

    def check(self, bucket: str = "") -> float:
        """Admit an attempt (0.0, and counts it) or the seconds to wait before one may come."""
        now = self._monotonic()
        with self._lock:
            b = self._bucket(bucket)
            attempts = b.attempts
            while attempts and attempts[0] <= now - self._window:
                attempts.popleft()
            wait = b.blocked_until - now
            if len(attempts) >= self._max:
                wait = max(wait, attempts[0] + self._window - now)
            if wait > 0:
                return wait
            attempts.append(now)
            # Charge the wait this attempt would earn by failing up front, so
            # a parallel attempt cannot slip in while this one is checked.
            b.blocked_until = now + self._wait_after(b.failures + 1)
            return 0.0

    def _wait_after(self, failures: int) -> float:
        if failures < self._threshold:
            return 0.0
        doublings = min(failures - self._threshold, 64)  # past the cap anyway; no overflow
        return float(min(self._base * 2**doublings, self._cap))

    def failed(self, bucket: str = "") -> float | None:
        """Record a failed attempt; the new wait when it lengthened the backoff, else None."""
        now = self._monotonic()
        with self._lock:
            b = self._bucket(bucket)
            b.failures += 1
            wait = self._wait_after(b.failures)
            if wait > 0:
                b.blocked_until = now + wait
            return wait if wait > self._wait_after(b.failures - 1) else None

    def succeeded(self, bucket: str = "") -> None:
        """A login by ``bucket`` succeeded: its backoff starts afresh."""
        with self._lock:
            b = self._bucket(bucket)
            b.failures = 0
            b.blocked_until = 0.0
