"""FIDO2 / WebAuthn ceremonies: registering a key (setup mode) and logging in with one.

Ported from codereader's `fido2_auth.py` and adapted: no module state (the
challenges live in a `Fido2Auth` the app is given), keys in JailBee's own
database (`db.models.WebFido2Key`), and no public-URL setting. The relying
party is derived per request from the browser's `Origin` header, falling back
to `Host` (`relying_party`), so the server works behind a reverse proxy at a
domain root without being told its public name. Trusting the header is safe:
the authenticator signs the origin and the RP ID hash itself, and a key
registered for one domain never signs for another.
"""

from __future__ import annotations

import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from fido2.server import Fido2Server
from fido2.webauthn import (
    AttestedCredentialData,
    AuthenticationResponse,
    PublicKeyCredentialRpEntity,
    PublicKeyCredentialUserEntity,
    UserVerificationRequirement,
)
from sqlmodel import Session, col, select

from jailbee.db.models import WebFido2Key
from jailbee.web import auth_admin
from jailbee.web.auth import Clock

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

CHALLENGE_TTL = 120.0
"""Seconds a begun ceremony may take before its challenge is refused."""

MAX_PENDING = 64
"""Pending challenges kept at most; the oldest is dropped past it. Beginning a
login needs no credentials, so the store must not grow without bound."""

RP_NAME = "JailBee"

USER = PublicKeyCredentialUserEntity(id=b"jailbee-web", name="jailbee", display_name="JailBee")
"""The one account: the dashboard has a single user, the host's owner."""

Ceremony = Literal["register", "login"]


class Fido2Error(Exception):
    """A refused ceremony step; the message is safe to show the browser."""


@dataclass(frozen=True)
class RelyingParty:
    """The WebAuthn relying party one request is talking to."""

    id: str
    """The RP ID: the host name of the page origin, e.g. ``jb.example.com``."""
    origin: str
    """The page origin the browser puts in ``clientDataJSON``."""


def relying_party(origin: str | None, host: str | None, scheme: str) -> RelyingParty:
    """The relying party from a request's ``Origin`` header, else ``Host`` + ``scheme``.

    Browsers send ``Origin`` on the `fetch` POSTs the login and setup views
    make; a reverse proxy passes it through unchanged while it may rewrite
    ``Host``, so ``Origin`` wins. Raises `Fido2Error` when neither names a host.
    """
    if not origin or origin == "null":
        if not host:
            raise Fido2Error("The request names no origin or host.")
        origin = f"{scheme}://{host}"
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Fido2Error(f"Unusable origin: {origin!r}")
    return RelyingParty(parts.hostname, f"{parts.scheme}://{parts.netloc}")


@dataclass(frozen=True)
class Challenge:
    """A begun ceremony: what the browser passes to ``navigator.credentials``."""

    id: str
    options: dict[str, Any]
    """The ``publicKey`` options as JSON (binary fields base64url-encoded)."""


@dataclass(frozen=True)
class _Pending:
    ceremony: Ceremony
    rp: RelyingParty
    state: dict[str, Any]
    started: float


def _server(rp: RelyingParty) -> Fido2Server:
    return Fido2Server(
        PublicKeyCredentialRpEntity(id=rp.id, name=RP_NAME),
        verify_origin=lambda o: o == rp.origin,
    )


class Fido2Auth:
    """Runs WebAuthn ceremonies against the keys in the database.

    One per app (`WebContext.fido2`, `SetupContext.fido2`). Each begun
    ceremony is remembered under a random id with the relying party it was
    begun for; completing it from another origin, after `CHALLENGE_TTL`, or a
    second time is refused. Thread-safe: requests run in a thread pool.
    """

    def __init__(self, engine: Engine, *, clock: Clock, monotonic: Callable[[], float]) -> None:
        self._engine = engine
        self._clock = clock
        self._monotonic = monotonic
        self._pending: OrderedDict[str, _Pending] = OrderedDict()
        self._lock = threading.Lock()

    # --- challenge store ---------------------------------------------------

    def _remember(self, ceremony: Ceremony, rp: RelyingParty, state: dict[str, Any]) -> str:
        challenge_id = secrets.token_urlsafe(16)
        now = self._monotonic()
        with self._lock:
            for key in [k for k, p in self._pending.items() if now - p.started > CHALLENGE_TTL]:
                del self._pending[key]
            self._pending[challenge_id] = _Pending(ceremony, rp, state, now)
            while len(self._pending) > MAX_PENDING:
                self._pending.popitem(last=False)
        return challenge_id

    def _take(self, ceremony: Ceremony, rp: RelyingParty, challenge_id: str) -> dict[str, Any]:
        with self._lock:
            pending = self._pending.pop(challenge_id, None)
        if (
            pending is None
            or pending.ceremony != ceremony
            or self._monotonic() - pending.started > CHALLENGE_TTL
        ):
            raise Fido2Error("Unknown or expired challenge; start again.")
        if pending.rp != rp:
            raise Fido2Error("The challenge was begun from another origin.")
        return pending.state

    # --- stored keys -------------------------------------------------------

    def _credentials(self, rp: RelyingParty) -> list[AttestedCredentialData]:
        with Session(self._engine) as s:
            rows = s.exec(select(WebFido2Key).where(WebFido2Key.rp_id == rp.id)).all()
            return [AttestedCredentialData(r.credential_data) for r in rows]

    # --- registration (setup mode only) -------------------------------------

    def register_begin(self, rp: RelyingParty) -> Challenge:
        options, state = _server(rp).register_begin(
            USER,
            self._credentials(rp),
            user_verification=UserVerificationRequirement.DISCOURAGED,
        )
        return Challenge(self._remember("register", rp, state), dict(options)["publicKey"])

    def register_complete(
        self, rp: RelyingParty, challenge_id: str, credential: dict[str, Any], name: str
    ) -> auth_admin.Fido2KeyInfo:
        state = self._take("register", rp, challenge_id)
        try:
            auth_data = _server(rp).register_complete(state, credential)
        except Exception as exc:  # fido2 raises ValueError and friends on any bad field
            raise Fido2Error(f"The key's registration was refused: {exc}") from exc
        cred = auth_data.credential_data
        if cred is None:
            raise Fido2Error("The key sent no credential.")
        with Session(self._engine) as s:
            duplicate = s.exec(
                select(WebFido2Key).where(col(WebFido2Key.credential_id) == cred.credential_id)
            ).first()
            if duplicate is not None:
                raise Fido2Error("This key is already registered.")
            row = WebFido2Key(
                name=name,
                credential_id=cred.credential_id,
                credential_data=bytes(cred),
                rp_id=rp.id,
                sign_count=auth_data.counter,
                created_at=self._clock(),
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            assert row.id is not None
            return auth_admin.Fido2KeyInfo(row.id, row.name, row.rp_id, row.created_at, None)

    # --- login ---------------------------------------------------------------

    def login_begin(self, rp: RelyingParty) -> Challenge:
        """Raises `Fido2Error` when no key is registered for ``rp``."""
        credentials = self._credentials(rp)
        if not credentials:
            raise Fido2Error(f"No security key is registered for {rp.id}.")
        options, state = _server(rp).authenticate_begin(
            credentials, user_verification=UserVerificationRequirement.DISCOURAGED
        )
        return Challenge(self._remember("login", rp, state), dict(options)["publicKey"])

    def login_complete(
        self, rp: RelyingParty, challenge_id: str, credential: dict[str, Any]
    ) -> bool:
        """True when ``credential`` is a valid signature over the begun challenge.

        A signature counter that does not grow past the stored one (when
        either is non-zero) is refused: the key may have been cloned.
        """
        try:
            state = self._take("login", rp, challenge_id)
            response = AuthenticationResponse.from_dict(credential)
            matched = _server(rp).authenticate_complete(state, self._credentials(rp), response)
        except Exception:  # a malformed or forged response is a failed login, not a 500
            return False
        counter = response.response.authenticator_data.counter
        with Session(self._engine) as s:
            row = s.exec(
                select(WebFido2Key).where(col(WebFido2Key.credential_id) == matched.credential_id)
            ).first()
            if row is None:
                return False
            if (counter or row.sign_count) and counter <= row.sign_count:
                return False
            row.sign_count = counter
            row.last_used_at = self._clock()
            s.add(row)
            s.commit()
        return True
