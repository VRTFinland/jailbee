"""Read and write the latest Docker seed result for each golden alias."""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Session

from jailbee.db.models import GoldenSeedState


def record(
    session: Session,
    *,
    alias: str,
    container_prefix: str,
    seeded_at: datetime,
    source: str,
    outcome: str,
    image_count: int,
    size_bytes: int,
    detail: str = "",
) -> GoldenSeedState:
    """Insert or replace the latest seed result for ``alias``."""
    state = session.get(GoldenSeedState, alias)
    if state is None:
        state = GoldenSeedState(
            alias=alias,
            container_prefix=container_prefix,
            seeded_at=seeded_at,
            source=source,
            outcome=outcome,
            image_count=image_count,
            size_bytes=size_bytes,
            detail=detail,
        )
        session.add(state)
    else:
        state.container_prefix = container_prefix
        state.seeded_at = seeded_at
        state.source = source
        state.outcome = outcome
        state.image_count = image_count
        state.size_bytes = size_bytes
        state.detail = detail
    session.commit()
    session.refresh(state)
    return state


def get(session: Session, alias: str) -> GoldenSeedState | None:
    """Return the stored seed result for ``alias``, if any."""
    return session.get(GoldenSeedState, alias)


def delete(session: Session, alias: str) -> None:
    """Remove the stored seed result for ``alias`` if present."""
    state = session.get(GoldenSeedState, alias)
    if state is not None:
        session.delete(state)
        session.commit()
