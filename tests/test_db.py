"""Tests for database models and schema migrations."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine

from jailbee.db import CURRENT_SCHEMA_VERSION, _ensure_schema


def test_egress_override_roundtrips(db_session, frozen_now):
    from jailbee.db.models import EgressOverride

    db_session.add(
        EgressOverride(
            container_prefix="myrepo",
            entry="nexus.corp:443",
            added_at=frozen_now,
        )
    )
    db_session.commit()

    row = db_session.get(EgressOverride, ("myrepo", "nexus.corp:443"))
    assert row is not None
    assert row.added_at == frozen_now


def test_schema_version_is_seven(db_engine):
    from sqlmodel import Session

    from jailbee.db import CURRENT_SCHEMA_VERSION
    from jailbee.db.models import SchemaMeta

    assert CURRENT_SCHEMA_VERSION == 7
    with Session(db_engine) as s:
        assert s.get(SchemaMeta, 1).version == 7


def test_v6_database_migrates_to_v7_without_losing_rows(tmp_path):
    """A v6 DB with pool data migrates forward, not through the destructive reset."""
    from sqlmodel import Session, SQLModel, create_engine

    from jailbee.db import _ensure_schema
    from jailbee.db.models import RegisteredRepo, SchemaMeta

    db = tmp_path / "state.sqlite"
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(SchemaMeta(id=1, version=6))
        s.add(
            RegisteredRepo(
                container_prefix="myrepo",
                repo_root="/tmp/myrepo",
                registered_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        s.commit()

    _ensure_schema(engine)

    with Session(engine) as s:
        assert s.get(SchemaMeta, 1).version == 7
        # Survived: a destructive reset would have dropped this row.
        assert s.get(RegisteredRepo, "myrepo") is not None
