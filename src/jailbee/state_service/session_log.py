"""Which container ran which agent session: recorded per gather, read by the web dashboard.

The gatherer hands every snapshot's groups to a `SessionRecorder`; a live
session (`AgentSummary.session_ids`) becomes a `ContainerSession` row keyed
by the container's Incus instance uuid, so a container recreated under the
same name does not inherit the old one's sessions. Writes are throttled:
a session already written in the last `write_interval` seconds costs no
database round trip. Only sessions the latest gather reported are remembered.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from jailbee.db.models import ContainerSession

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from jailbee.dashboard.model import RepoGroup

WRITE_INTERVAL = 60.0
"""Seconds between writes of one session's ``last_seen``."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SessionRecorder:
    """Upserts the live sessions of each gather. Not thread-safe (one gather at a time)."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] = _utcnow,
        write_interval: float = WRITE_INTERVAL,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._interval = write_interval
        self._written: dict[tuple[str, str, str, str], datetime] = {}

    def __call__(self, groups: Iterable[RepoGroup]) -> None:
        self.record(groups)

    def record(self, groups: Iterable[RepoGroup]) -> None:
        now = self._clock()
        due: list[tuple[str, str, str, str]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for group in groups:
            for c in group.containers:
                for summary in c.agent_status:
                    for sid in summary.session_ids:
                        key = (c.name, c.uuid or "", summary.agent, sid)
                        seen.add(key)
                        last = self._written.get(key)
                        if last is None or (now - last).total_seconds() >= self._interval:
                            due.append(key)
        # Forget sessions this gather no longer reports: the throttle's memory
        # stays bounded by the live sessions over a long-running server.
        self._written = {k: v for k, v in self._written.items() if k in seen}
        if not due:
            return
        with Session(self._engine) as s:
            for name, uuid, agent, sid in due:
                row = s.exec(
                    select(ContainerSession).where(
                        ContainerSession.container_uuid == uuid,
                        ContainerSession.container_name == name,
                        ContainerSession.agent == agent,
                        ContainerSession.session_id == sid,
                    )
                ).first()
                if row is None:
                    s.add(
                        ContainerSession(
                            container_name=name,
                            container_uuid=uuid,
                            agent=agent,
                            session_id=sid,
                            first_seen=now,
                            last_seen=now,
                        )
                    )
                else:
                    row.last_seen = now
                    s.add(row)
            s.commit()
        for key in due:
            self._written[key] = now


def recorded_sessions(engine: Engine, name: str, uuid: str | None) -> list[ContainerSession]:
    """The sessions recorded for this container instance, most recently seen first."""
    with Session(engine) as s:
        rows = s.exec(
            select(ContainerSession)
            .where(
                ContainerSession.container_name == name,
                ContainerSession.container_uuid == (uuid or ""),
            )
            .order_by(col(ContainerSession.last_seen).desc())
        ).all()
        for row in rows:
            s.expunge(row)
        return list(rows)


def attribution(
    engine: Engine, names: Iterable[tuple[str, str | None]]
) -> dict[tuple[str, str], str]:
    """``(agent, session_id)`` → container name, over these ``(name, uuid)`` instances.

    The most recently seen container wins when two instances claim one session.
    """
    out: dict[tuple[str, str], str] = {}
    for name, uuid in names:
        for row in reversed(recorded_sessions(engine, name, uuid)):
            out[(row.agent, row.session_id)] = name
    return out
