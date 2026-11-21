"""Web dashboard auth management: the PIN switch, TOTP keys, device sessions, the audit log.

The CLI (`jailbee remote web auth ...`) is the only writer of settings and
keys; the server reads them on every login, so a change applies without a
restart. Imports no FastAPI, so the CLI works without loading the server;
`pyotp` (from the `web` extra) is imported only where a TOTP secret is used.

Audit: every login, failed login, logout and revocation is a `WebAuthEvent`
row (the CLI revokes from another process than the server, so the database is
the one place both can write) and a line on the `jailbee.web.audit` logger,
which the foreground server prints and its systemd unit sends to the journal.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from sqlalchemy import and_, not_
from sqlmodel import Session, col, delete, select

from jailbee.db.models import WebAuthEvent, WebAuthSetting, WebFido2Key, WebSession, WebTotpKey
from jailbee.web.auth import Clock, SessionInfo, utcnow

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

log = logging.getLogger("jailbee.web.audit")

AUDIT_KEEP = 1000
"""How many audit rows are kept; older ones are pruned on each write.

Failed logins by an `UNLIMITED_METHODS` method are counted apart (see
`UNLIMITED_FAILURES_KEEP`): nothing limits how many an attacker sends, so
sharing this budget would let a flood of them prune the PIN and TOTP guesses
the log is there to show."""

UNLIMITED_FAILURES_KEEP = 100
"""How many failed logins by an `UNLIMITED_METHODS` method are kept, on their own budget."""

UNLIMITED_METHODS = frozenset({"link", "fido2"})
"""Login methods no attempt limit applies to: a link token is single-use,
high-entropy and lives 60 s; a FIDO2 login is a signature over a fresh
challenge. Guessing either is hopeless, and limiting them would let a PIN or
TOTP guesser lock the owner out. PIN and TOTP each have their own bucket."""

TOTP_REPLAY_WINDOW = timedelta(seconds=90)
"""How long a used code stays refused: the accepted window (±1 step of 30 s) and then some."""

TOTP_ISSUER = "JailBee"

AuthEventKind = Literal["login", "login_failed", "logout", "revoke", "key_added"]
LoginMethod = Literal["pin", "totp", "fido2"]

_PIN_ENABLED = "pin_enabled"


class AuthAdminError(Exception):
    """A refused management action; the message is shown to the user as is."""


@dataclass(frozen=True)
class TotpKeyInfo:
    """A stored TOTP key, as listings show it. Never carries the secret."""

    id: int
    name: str
    created_at: datetime


@dataclass(frozen=True)
class NewTotpKey:
    """A key just added: the one time its secret and provisioning URI are shown."""

    key: TotpKeyInfo
    secret: str
    uri: str


@dataclass(frozen=True)
class Fido2KeyInfo:
    """A registered FIDO2 key, as listings show it."""

    id: int
    name: str
    rp_id: str
    created_at: datetime
    last_used_at: datetime | None


@dataclass(frozen=True)
class AuditEvent:
    at: datetime
    event: str
    method: str
    device_name: str
    source: str


# --- PIN switch -------------------------------------------------------------


def pin_enabled(engine: Engine) -> bool:
    """Whether PIN login is on (the default)."""
    with Session(engine) as s:
        row = s.get(WebAuthSetting, _PIN_ENABLED)
        return row is None or row.value == "true"


def set_pin_enabled(engine: Engine, enabled: bool) -> None:
    """Turn PIN login on or off. Refuses to turn off the last login method."""
    if not enabled and not list_totp_keys(engine) and not list_fido2_keys(engine):
        raise AuthAdminError(
            "Refusing to turn off PIN login: no TOTP key or FIDO2 key exists, so nobody "
            "could log in. Add one first with `jailbee remote web auth totp add` or "
            "`jailbee remote web auth fido2 setup`."
        )
    with Session(engine) as s:
        row = s.get(WebAuthSetting, _PIN_ENABLED) or WebAuthSetting(key=_PIN_ENABLED, value="")
        row.value = "true" if enabled else "false"
        s.add(row)
        s.commit()


def enabled_methods(engine: Engine) -> list[LoginMethod]:
    """The login methods the login view offers, in display order."""
    methods: list[LoginMethod] = []
    if pin_enabled(engine):
        methods.append("pin")
    if list_totp_keys(engine):
        methods.append("totp")
    if list_fido2_keys(engine):
        methods.append("fido2")
    return methods


# --- TOTP keys --------------------------------------------------------------


def _key_info(row: WebTotpKey) -> TotpKeyInfo:
    assert row.id is not None
    return TotpKeyInfo(row.id, row.name, row.created_at)


def add_totp_key(engine: Engine, name: str, *, clock: Clock = utcnow) -> NewTotpKey:
    """Store a fresh random TOTP secret labelled ``name``."""
    import pyotp

    secret = pyotp.random_base32()
    label = name or "web dashboard"
    uri = pyotp.TOTP(secret).provisioning_uri(name=label, issuer_name=TOTP_ISSUER)
    with Session(engine) as s:
        row = WebTotpKey(name=name, secret=secret, created_at=clock())
        s.add(row)
        s.commit()
        s.refresh(row)
        return NewTotpKey(_key_info(row), secret, uri)


def qr_text(data: str) -> str:
    """``data`` as a QR code drawn with block characters, for a terminal."""
    import io

    import qrcode

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(data)
    qr.make(fit=True)
    out = io.StringIO()
    qr.print_ascii(out=out, invert=True)
    return out.getvalue()


def list_totp_keys(engine: Engine) -> list[TotpKeyInfo]:
    with Session(engine) as s:
        rows = s.exec(select(WebTotpKey).order_by(col(WebTotpKey.id))).all()
        return [_key_info(r) for r in rows]


def remove_totp_key(engine: Engine, key_id: int) -> TotpKeyInfo:
    """Delete key ``key_id``. Refuses the last key while PIN login is off."""
    with Session(engine) as s:
        row = s.get(WebTotpKey, key_id)
        if row is None:
            raise AuthAdminError(f"No TOTP key with id {key_id}.")
        info = _key_info(row)
        remaining = len(s.exec(select(WebTotpKey.id)).all()) - 1
        if remaining == 0 and not pin_enabled(engine) and not list_fido2_keys(engine):
            raise AuthAdminError(
                "Refusing to remove the last TOTP key while PIN login is off and no "
                "FIDO2 key exists: nobody could log in. Turn the PIN on first with "
                "`jailbee remote web auth pin on`."
            )
        s.delete(row)
        s.commit()
        return info


class TotpVerifier:
    """Checks a code against every stored key; each accepted code works once.

    Accepts the current 30-second step and one on either side (clock drift).
    A code already used with a key is refused for `TOTP_REPLAY_WINDOW`, so a
    shoulder-surfed code cannot open a second session. Thread-safe: login
    requests run in a thread pool.
    """

    def __init__(self, engine: Engine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock
        self._used: dict[tuple[int, str], datetime] = {}
        self._lock = threading.Lock()

    def verify(self, code: str) -> bool:
        import pyotp

        now = self._clock()
        with Session(self._engine) as s:
            keys = [(r.id, r.secret) for r in s.exec(select(WebTotpKey)).all()]
        with self._lock:
            self._used = {k: t for k, t in self._used.items() if now - t < TOTP_REPLAY_WINDOW}
            for key_id, secret in keys:
                assert key_id is not None
                if (key_id, code) in self._used:
                    continue
                if pyotp.TOTP(secret).verify(code, for_time=now, valid_window=1):
                    self._used[(key_id, code)] = now
                    return True
        return False


# --- FIDO2 keys -------------------------------------------------------------
# Registering a key needs the WebAuthn ceremony (`jailbee.web.fido2_auth`, run
# by the setup server); listing and removing them is plain database work.


def _fido2_info(row: WebFido2Key) -> Fido2KeyInfo:
    assert row.id is not None
    return Fido2KeyInfo(row.id, row.name, row.rp_id, row.created_at, row.last_used_at)


def list_fido2_keys(engine: Engine) -> list[Fido2KeyInfo]:
    with Session(engine) as s:
        rows = s.exec(select(WebFido2Key).order_by(col(WebFido2Key.id))).all()
        return [_fido2_info(r) for r in rows]


def remove_fido2_key(engine: Engine, key_id: int) -> Fido2KeyInfo:
    """Delete key ``key_id``. Refuses the last login method (no PIN, no TOTP key)."""
    with Session(engine) as s:
        row = s.get(WebFido2Key, key_id)
        if row is None:
            raise AuthAdminError(f"No FIDO2 key with id {key_id}.")
        info = _fido2_info(row)
        remaining = len(s.exec(select(WebFido2Key.id)).all()) - 1
        if remaining == 0 and not pin_enabled(engine) and not list_totp_keys(engine):
            raise AuthAdminError(
                "Refusing to remove the last FIDO2 key while PIN login is off and no "
                "TOTP key exists: nobody could log in. Turn the PIN on first with "
                "`jailbee remote web auth pin on`."
            )
        s.delete(row)
        s.commit()
        return info


# --- device sessions --------------------------------------------------------


def list_sessions(engine: Engine) -> list[SessionInfo]:
    """Every stored session, most recently used first."""
    with Session(engine) as s:
        rows = s.exec(select(WebSession).order_by(col(WebSession.last_used_at).desc())).all()
        return [_session_info(r) for r in rows]


def _session_info(row: WebSession) -> SessionInfo:
    assert row.id is not None
    return SessionInfo(row.id, row.device_name, row.created_at, row.last_used_at)


def revoke_session(engine: Engine, session_id: int, *, clock: Clock = utcnow) -> SessionInfo:
    """Delete session ``session_id`` and audit it; its next request is refused."""
    with Session(engine) as s:
        row = s.get(WebSession, session_id)
        if row is None:
            raise AuthAdminError(f"No web session with id {session_id}.")
        info = _session_info(row)
        s.delete(row)
        s.commit()
    record_event(engine, "revoke", clock=clock, method="cli", device_name=info.device_name)
    return info


# --- audit ------------------------------------------------------------------


def record_event(
    engine: Engine,
    event: AuthEventKind,
    *,
    clock: Clock = utcnow,
    method: str = "",
    device_name: str = "",
    source: str = "",
) -> None:
    """Write one audit row and log it.

    Prunes the oldest rows of the written row's class past its budget: failed
    logins by an `UNLIMITED_METHODS` method past `UNLIMITED_FAILURES_KEEP`,
    every other row past `AUDIT_KEEP`. A write never evicts the other class.
    """
    at = clock()
    level = logging.WARNING if event == "login_failed" else logging.INFO
    log.log(
        level,
        "web auth %s method=%s device=%r source=%s at=%s",
        event,
        method or "-",
        device_name,
        source or "-",
        at.isoformat(),
    )
    with Session(engine) as s:
        s.add(
            WebAuthEvent(at=at, event=event, method=method, device_name=device_name, source=source)
        )
        s.commit()
        unlimited_failure = and_(
            col(WebAuthEvent.event) == "login_failed",
            col(WebAuthEvent.method).in_(UNLIMITED_METHODS),
        )
        if event == "login_failed" and method in UNLIMITED_METHODS:
            same_class, budget = unlimited_failure, UNLIMITED_FAILURES_KEEP
        else:
            same_class, budget = not_(unlimited_failure), AUDIT_KEEP
        keep = (
            select(WebAuthEvent.id)
            .where(same_class)
            .order_by(col(WebAuthEvent.id).desc())
            .limit(budget)
        )
        s.exec(delete(WebAuthEvent).where(same_class, col(WebAuthEvent.id).not_in(keep)))
        s.commit()


@contextlib.contextmanager
def audit_logging() -> Iterator[None]:
    """Print `jailbee.web.audit` lines on stderr while the server runs.

    As the SSH server does with its own audit logger: a foreground run shows
    them in the terminal, the systemd unit sends them to the journal.
    """
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    previous_level, previous_propagate = log.level, log.propagate
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        yield
    finally:
        log.removeHandler(handler)
        handler.close()
        log.setLevel(previous_level)
        log.propagate = previous_propagate


def recent_events(engine: Engine, limit: int = 50) -> list[AuditEvent]:
    """The newest ``limit`` audit entries, oldest first."""
    with Session(engine) as s:
        rows = s.exec(select(WebAuthEvent).order_by(col(WebAuthEvent.id).desc()).limit(limit)).all()
        return [
            AuditEvent(r.at, r.event, r.method, r.device_name, r.source) for r in reversed(rows)
        ]
