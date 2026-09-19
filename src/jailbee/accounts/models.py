"""Shared data model for an agent's account pool.

Moved here, verbatim, from jailbee's original, Claude-only pool, as the
generic engine took shape behind `adapters.base.AccountAdapter`.
Nothing here is Claude-specific: `Identity` and `Slot` describe any agent's
stored login, `Member` any repo sharing a holder, `LiveAccount` any holder's
live credential, and `PoolChange` what one pool operation did, for the CLI to
report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

LIVE_UNIDENTIFIED = "(unknown)"
"""Display name for a live credential whose account cannot be identified.

The parentheses are load-bearing: they cannot appear in a slug, so this name
can never collide with a parked slot or be typed as a bare-email reference.
"""

DISAMBIGUATOR = "~"
"""Separator between a slot's derived name and whatever makes it unique.

Safe because `_SLUG_UNSAFE` replaces it inside both halves of a derived name,
so it can never appear in one by accident. See `Slot` for the full grammar.
"""

_SLOT_SUFFIX = ".json"
"""Extension every file in the store carries; a slot name is the rest."""


class PoolError(Exception):
    """A pool operation cannot proceed; the message is user-facing."""


@dataclass(frozen=True)
class Identity:
    """An agent account as its config home names it.

    `email` is the primary discriminator every agent has. `org_uuid` is an
    optional second discriminator an agent may record (Claude Code's
    `organizationUuid`); it is not inherently a Claude organization, and an
    agent that names no such value leaves it None.
    """

    email: str
    org_uuid: str | None = None


@dataclass(frozen=True)
class Slot:
    """One stored login: a parked file, or the live credential.

    **Slot names follow one grammar**, and this docstring is its definition:

        <email>[#<org8>][~<disambiguator>]

    - `<email>` and `<org8>` are what `slug_for` derives from the account's
      identity. Its allowed set — `[a-z0-9@._-]` — contains neither `#` nor
      `~`, which is exactly what makes both separators safe to split on. Widen
      that set and this grammar breaks.
    - `~<disambiguator>` appears only when the derived name is already taken by
      a **different** grant. One account can legitimately hold two independent
      logins: `/login` as the same account after a `park` is the documented way
      to add one, and two holders on a host can each be logged into it. See
      `_disambiguated_slot` for the park side and `_slots_for` for the live
      side, whose disambiguator is the literal `live`.
    - Two shapes carry no email at all: `LIVE_UNIDENTIFIED`, and
      `unknown-<timestamp>` from `unknown_slot_name`. Both properties below are
      None for them, disambiguator or not.

    `org_hint` is the **truncated** organization from the slot name, not a
    UUID. Never compare it with `Identity.org_uuid`, and do not treat a name as
    an identity: `slug_for(identity)` equals the *derived* part of a slot name,
    which for a disambiguated slot is not the whole of it.
    """

    name: str
    path: Path
    live: bool

    @property
    def _derived(self) -> str:
        """The name without its `~<disambiguator>` suffix."""
        return self.name.split(DISAMBIGUATOR, 1)[0]

    @property
    def email(self) -> str | None:
        """The account's email, or None for an unidentified slot.

        Display-only: a real email address that happens to start with
        `unknown-` would be misreported as unidentified. `read_identity` does
        not require an email-shaped string, so this is possible in principle.
        """
        if is_unidentified(self.name):
            return None
        return self._derived.split("#", 1)[0]

    @property
    def org_hint(self) -> str | None:
        """First 8 characters of the organization UUID, when the name has one."""
        if self.email is None:
            return None
        _, sep, tail = self._derived.partition("#")
        return tail if sep else None

    @property
    def disambiguator(self) -> str | None:
        """The `~<disambiguator>` part, or None when the name has none.

        Unlike `email` and `org_hint` this is defined for the emailless shapes
        too: an unidentified slot can collide with another just as an
        identified one can, and the suffix is then the only thing telling the
        two apart.
        """
        _, sep, tail = self.name.partition(DISAMBIGUATOR)
        return tail if sep else None

    @property
    def display_name(self) -> str:
        """The name minus the organization, for a table that has an ORG column.

        `org_hint` is parsed back out of `name`, so rendering both in one row
        repeats the same eight characters twice. This drops the `#<org8>` half
        and keeps everything else — the `~<disambiguator>` included, because
        that half is load-bearing: it is what distinguishes two grants of one
        account, and `resolve_ref` needs it typed.

        Not the reference to feed back to `jailbee account use`: for an account
        stored under two organizations that is `name`, and the ambiguity error
        names it. This is display only, like `email` and `org_hint`.
        """
        if self.email is None:
            return self.name
        suffix = "" if self.disambiguator is None else f"{DISAMBIGUATOR}{self.disambiguator}"
        return f"{self.email}{suffix}"


_SLUG_UNSAFE = re.compile(r"[^a-z0-9@._-]")


def slug_for(identity: Identity) -> str:
    """The slot name for an account: `<email>` or `<email>#<org8>`.

    The organization is in the name whenever the account has one, not only on
    collision: detecting a collision after the fact would mean knowing an
    existing slot's organization, which without a manifest is not knowable.

    **Both halves are sanitized, and the result is a single path component.**
    Identity comes from a `.claude.json` that containers write, so it is
    untrusted: an unsanitized organization carrying `/` would let a slot path
    escape the store. Leading dots are stripped as hygiene — a slot file is not
    meant to be hidden — though `Path.glob` would still list one.
    """
    email = _SLUG_UNSAFE.sub("-", identity.email.strip().lower())
    slug = email
    if identity.org_uuid:
        org = _SLUG_UNSAFE.sub("-", identity.org_uuid.strip().lower())[:8]
        slug = f"{email}#{org}"
    return slug.lstrip(".") or "unnamed"


_UNKNOWN_PREFIX = "unknown-"


def is_unidentified(name: str) -> bool:
    """Whether a slot name says nothing about which account it holds.

    True for the two emailless shapes: `unknown_slot_name`'s output and
    `LIVE_UNIDENTIFIED`. One definition, because `Slot.email` and the CLI's
    warning have to agree on what "unidentified" means — a name that reads as
    identified but warns, or the reverse, is worse than either.
    """
    derived = name.split(DISAMBIGUATOR, 1)[0]
    return derived == LIVE_UNIDENTIFIED or derived.startswith(_UNKNOWN_PREFIX)


def unknown_slot_name(when: datetime) -> str:
    """Name for parking a credential whose account cannot be identified.

    Self-healing rather than blocking: once that account is activated and used,
    its config home carries an identity, so the *next* park writes the real
    name.
    """
    return f"{_UNKNOWN_PREFIX}{when.strftime('%Y%m%d-%H%M%S')}"


@dataclass(frozen=True)
class Member:
    """One repo sharing a holder, and the config home whose identity it owns."""

    container_prefix: str
    config_home: Path


@dataclass(frozen=True)
class LiveAccount:
    """The holder's live login: an account, and the record that names it.

    The identity and the record come from the *same* read, which is why they
    are one object: the name a park writes and the record it stores must
    describe the same account, and two separate reads could land on two
    different files.
    """

    identity: Identity
    record: dict[str, Any]


@dataclass(frozen=True)
class PoolChange:
    """What one pool operation did, for the CLI to report."""

    parked_as: str | None
    activated: str | None
    updated: list[str]
    """Members whose recorded account now agrees with the holder.

    "Updated" rather than "cleared": an activation *writes* the record the slot
    was carrying (see `ACCOUNT_RECORD_KEY`), and only a `park` — or a slot with
    no record — deletes it. Both leave the member correct, which is the fact the
    CLI reports.
    """
    not_updated: list[str]
    """Members still naming the previous account, unreadable ones included."""
    live_sessions: list[str]
