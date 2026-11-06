"""Tests for persisted golden Docker seed results."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, create_engine, select

from jailbee.db.models import GoldenSeedState
from jailbee.docker_seed_state import get, record


def test_seed_state_record_overwrites_per_alias() -> None:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)

    first_time = datetime(2026, 11, 5, tzinfo=UTC)
    second_time = datetime(2026, 11, 6, tzinfo=UTC)
    with Session(engine) as session:
        record(
            session,
            alias="sampleapp--golden",
            container_prefix="sampleapp",
            seeded_at=first_time,
            source="seed-container",
            outcome="seeded",
            image_count=3,
            size_bytes=4096,
        )
        record(
            session,
            alias="sampleapp--golden",
            container_prefix="sampleapp",
            seeded_at=second_time,
            source="container:dev",
            outcome="failed",
            image_count=0,
            size_bytes=0,
            detail="docker load failed",
        )

        rows = session.exec(select(GoldenSeedState)).all()
        state = get(session, "sampleapp--golden")

    assert len(rows) == 1
    assert state is not None
    assert state.container_prefix == "sampleapp"
    assert state.seeded_at == second_time
    assert state.source == "container:dev"
    assert state.outcome == "failed"
    assert state.image_count == 0
    assert state.size_bytes == 0
    assert state.detail == "docker load failed"
