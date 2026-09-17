"""Advisory notices the user can mark read, and the store that remembers it.

Two kinds of advisory repeat on every command until the thing behind them is
fixed: the upgrade advice in `jailbee.upgrade` (an owed `jailbee base build` /
`jailbee apply`) and the deprecation notices emitted while the config loads
(`paths._warn_legacy_config_dir`, `config.loader._warn_legacy_chrome_block`).
`jailbee dismiss` marks either kind read.

This module owns the `dismissed_notice` table for **both** families — one
store, so the two spellings of "dismissed" cannot drift — and additionally
owns the deprecation family itself: its `Notice` type, the `emit` that
suppresses a dismissed one, and the registry of what applies to this process.
`upgrade.py` brings its own `Action` vocabulary and only ever reads.

Only *ambient* advisories belong here: output that appears whatever the user
asked for. A warning that answers the command the user just typed —
`jailbee config validate`'s notices, `jailbee base build`'s `golden.python`
line, a deprecated alias — is deliberately not dismissible, because "mark
read" means nothing for something you asked to be told.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jailbee.tui import hint

if TYPE_CHECKING:
    from datetime import datetime

    from sqlmodel import Session


@dataclass(frozen=True)
class Dismissal:
    """One recorded dismissal, detached from the row it was read from.

    A plain dataclass rather than the SQLModel instance so callers can hold it
    past their ``with Session(...)`` block — `upgrade.load_or_backfill`
    documents the ``DetachedInstanceError`` that shape avoids.
    """

    key: str
    scope: str
    fingerprint: str
    version: str
    dismissed_at: datetime


@dataclass(frozen=True)
class Notice:
    """One deprecation advisory, identified by what it is about.

    `scope` is the config file the notice names, never the repo the user
    happens to stand in: a host-wide command (`jailbee claude ls`, the
    dashboards) loads every registered repo's config, so two files spelling
    ``chrome:`` are two notices and two independent decisions.

    `lines` is the message *without* the dismissal footer — `emit` adds that,
    so `jailbee doctor` can print the message again without inviting the user
    to dismiss what they have already dismissed.
    """

    key: str
    scope: str
    lines: tuple[str, ...]

    @property
    def ident(self) -> tuple[str, str]:
        return (self.key, self.scope)


# ---------------------------------------------------------------------------
# the store — shared by both families
# ---------------------------------------------------------------------------


def load_all(session: Session) -> dict[tuple[str, str], Dismissal]:
    """Every recorded dismissal, keyed ``(key, scope)``."""
    from sqlmodel import select

    from jailbee.db.models import DismissedNotice

    rows = session.exec(select(DismissedNotice)).all()
    return {
        (row.key, row.scope): Dismissal(
            key=row.key,
            scope=row.scope,
            fingerprint=row.fingerprint,
            version=row.version,
            dismissed_at=row.dismissed_at,
        )
        for row in rows
    }


def save(
    session: Session,
    key: str,
    scope: str,
    *,
    fingerprint: str,
    version: str,
    now: datetime,
) -> None:
    """Record (or update) a dismissal.

    Updating rather than refusing a duplicate is the point: dismissing `apply`
    again after a new release added a reason is how the user acknowledges the
    newer fingerprint.
    """
    from jailbee.db.models import DismissedNotice

    row = session.get(DismissedNotice, (scope, key))
    if row is None:
        row = DismissedNotice(
            scope=scope,
            key=key,
            fingerprint=fingerprint,
            version=version,
            dismissed_at=now,
        )
    else:
        row.fingerprint = fingerprint
        row.version = version
        row.dismissed_at = now
    session.add(row)
    session.commit()


def drop(session: Session, key: str, scope: str) -> bool:
    """Remove a dismissal. True when one was there."""
    from jailbee.db.models import DismissedNotice

    row = session.get(DismissedNotice, (scope, key))
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


# ---------------------------------------------------------------------------
# the deprecation family
# ---------------------------------------------------------------------------

_ACTIVE: dict[tuple[str, str], Notice] = {}
"""Notices this process found applicable, suppressed ones included.

Populated by `emit` as the config loads, read afterwards by `jailbee dismiss`
(to list what can be marked read) and by `jailbee doctor` (to report what has
been). A suppressed notice is registered too: it still applies, and leaving it
out would hide it from the one command that must never hide it.
"""


@functools.cache
def _read_dismissals() -> dict[tuple[str, str], Dismissal]:
    """The cached read behind `dismissals`.

    Cached because `emit` runs inside config loading — `jailbee new` loads the
    config three times and both dashboards reload it on every refresh tick —
    and wrapped broadly because config loading must not acquire a hard
    dependency on the state DB. A locked or unreadable database degrades to
    "nothing is dismissed", which shows an advisory one more time; the
    alternative is a traceback out of an unrelated command.
    """
    from sqlmodel import Session

    from jailbee.db import get_engine

    try:
        with Session(get_engine()) as session:
            return load_all(session)
    except Exception:  # a courtesy read; must never fail the command
        return {}


def dismissals() -> dict[tuple[str, str], Dismissal]:
    """The recorded dismissals, read once per process.

    A thin wrapper over the cached `_read_dismissals` rather than the cached
    function itself: a test that substitutes this function must not also take
    `reset_caches`'s handle on the cache with it.
    """
    return _read_dismissals()


def reset_caches() -> None:
    """Drop both process caches.

    For tests, and for `jailbee dismiss` after it writes — its own status view
    must show what it just recorded.
    """
    _read_dismissals.cache_clear()
    _ACTIVE.clear()


def active() -> tuple[Notice, ...]:
    """The notices that apply to this process, in the order they were found."""
    return tuple(_ACTIVE.values())


def emit(notice: Notice) -> None:
    """Print `notice` unless it has been dismissed, registering it either way.

    `hint`, like the notices this replaces: stderr, so a warning never lands in
    the middle of ``jailbee ls --format json``, and no Rich markup, so a
    ``[...]`` in a path is not read as a style tag and silently deleted.
    """
    _ACTIVE[notice.ident] = notice
    if notice.ident in dismissals():
        return
    hint([*notice.lines, f"    Or `jb dismiss {notice.key}` to stop repeating this."])
