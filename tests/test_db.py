"""Tests for the egress-override table.

Schema bootstrapping and the migration chain live in
`tests/test_db_schema.py`; this file covers only the model added with the
egress-override feature.
"""

from __future__ import annotations


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


def test_host_network_default_roundtrips(db_session):
    from jailbee.db.models import HostNetworkDefault

    db_session.add(HostNetworkDefault(id=1, generation="work"))
    db_session.commit()

    row = db_session.get(HostNetworkDefault, 1)
    assert row is not None
    assert row.generation == "work"
