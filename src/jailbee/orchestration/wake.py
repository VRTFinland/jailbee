"""Pure wake rules over broker-store rows (port of ``inbox.waiting.select_wakeup``)."""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from jailbee.inbox.waiting import (
    MESSAGE_KINDS,
    STATUS_STATES,
    matches_wake_filter,
    valid_wake_token,
)

__all__ = [
    "MESSAGE_KINDS",
    "STATUS_STATES",
    "DigestWake",
    "MessageWake",
    "QuietChildWake",
    "Wake",
    "WakeRow",
    "WakeUsage",
    "newest_from_child",
    "select_wakeup",
    "valid_wake_token",
]


class WakeRow(Protocol):
    """The read-only row shape the rules need; the store's item satisfies it."""

    @property
    def id(self) -> str: ...
    @property
    def type(self) -> str: ...
    @property
    def kind_or_state(self) -> str | None: ...
    @property
    def from_name(self) -> str | None: ...
    @property
    def created_at(self) -> float: ...
    @property
    def acked_at(self) -> float | None: ...


class WakeUsage(Exception):  # noqa: N818 - mirrors inbox.errors.InboxUsage
    """quiet/digest was requested without a fresh children list."""


@dataclass(frozen=True)
class MessageWake:
    id: str


@dataclass(frozen=True)
class QuietChildWake:
    child: str


@dataclass(frozen=True)
class DigestWake:
    pass


Wake = MessageWake | QuietChildWake | DigestWake


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def newest_from_child(history: Sequence[WakeRow]) -> dict[str, float]:
    """Newest `created_at` of each sender's message rows."""
    newest: dict[str, float] = {}
    for row in history:
        name = row.from_name
        if row.type == "message" and isinstance(name, str):
            newest[name] = max(newest.get(name, 0.0), row.created_at)
    return newest


def _activity_at(child: Mapping[str, Any], now: float) -> float:
    status = child.get("status")
    value: Any = status.get("activity_at") if isinstance(status, Mapping) else None
    if not _number(value) or value > now + 5:
        return 0.0
    return float(value)


def select_wakeup(
    unacked: Sequence[WakeRow],
    history: Sequence[WakeRow],
    children: Sequence[Mapping[str, object]] | None,
    *,
    children_observed_at: float | None,
    wake: frozenset[str] | None,
    quiet: int,
    digest: int,
    digest_at: float | None,
    now: float,
) -> tuple[Wake | None, float | None]:
    """Pick what should wake a waiter, and a digest stamp to store (or None).

    The stamp is `now` when a digest is requested and none was usable (nothing
    fires yet) or when a digest fires.
    """
    started: dict[str, float] = {}
    activity: dict[str, float] = {}
    running: set[str] = set()
    try:
        assert children is not None
        # Same freshness window as `inbox.waiting.select_wakeup`'s `updated_at`.
        assert children_observed_at is not None and _number(children_observed_at)
        assert -5 <= now - children_observed_at <= 900
        for child in children:
            name = child["name"]
            assert isinstance(name, str) and isinstance(child["running"], bool)
            done = child.get("done", False)
            assert isinstance(done, bool)
            start = child["started_at"]
            assert isinstance(start, (int, float)) and not isinstance(start, bool)
            assert math.isfinite(start) and start <= now + 5
            started[name] = float(start)
            activity[name] = _activity_at(child, now)
            if child["running"] and not done:
                running.add(name)
    except (AssertionError, KeyError, TypeError):
        if quiet or digest:
            names = " and ".join(
                flag for flag, on in (("--quiet-after", quiet), ("--digest", digest)) if on
            )
            raise WakeUsage(
                f"{names} {'need' if quiet and digest else 'needs'} a fresh broker children list"
            ) from None
        started = {}
        running.clear()

    first: dict[str, str] = {}
    for row in sorted(history, key=lambda r: (r.created_at, r.id)):
        name = row.from_name
        if row.type == "message" and isinstance(name, str) and name in started:
            if row.created_at >= started[name]:
                first.setdefault(name, row.id)

    for row in unacked:
        matches = matches_wake_filter(row.type, row.kind_or_state, wake)
        name = row.from_name
        is_first = isinstance(name, str) and first.get(name) == row.id
        if wake is None or matches or is_first:
            return MessageWake(row.id), None

    if quiet:
        newest = newest_from_child(history)
        latest = {n: max(started[n], newest.get(n, 0.0), activity.get(n, 0.0)) for n in running}
        for name in sorted(latest, key=lambda n: (latest[n], n)):
            if now - latest[name] >= quiet:
                clean = "".join("?" if ord(c) < 32 or 127 <= ord(c) <= 159 else c for c in name)
                return QuietChildWake(clean), None
    if digest:
        last = digest_at if digest_at is not None and math.isfinite(digest_at) else None
        if last is not None and last > now + 5:
            last = None
        if last is None:
            return None, now
        if running and now - last >= digest:
            return DigestWake(), now
    return None, None
