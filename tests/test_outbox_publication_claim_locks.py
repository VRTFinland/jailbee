"""Where a publication claim's lock file lives, and that orphans are swept.

A claim's liveness is judged by a lock file in ``claims/`` next to broker.db.
The writer and every reader must agree on that directory, also when broker.db
is reached through a symlink; and lock files nobody holds must not pile up.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from jailbee.orchestration.store import BrokerStore, store_path
from jailbee.outbox import publication_db as pdb
from jailbee.outbox.publication_db import PublicationStore


def test_a_claim_through_a_symlinked_store_reads_as_live(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    real = store_path(real_root)
    BrokerStore(real).transaction().__enter__().close()  # create and migrate it
    link_dir = tmp_path / "link"
    link_dir.mkdir()
    link = link_dir / real.name
    link.symlink_to(real)
    ps = PublicationStore(BrokerStore(link))

    with ps.claim("u1", "issue", "001-x.json"):
        with BrokerStore(real).transaction() as con:
            assert pdb.claim_is_live(con, "u1", "issue", "001-x.json")
        with BrokerStore(link).transaction() as con:
            assert pdb.claim_is_live(con, "u1", "issue", "001-x.json")


def _claims(root: Path) -> list[str]:
    path = store_path(root).parent / "claims"
    return sorted(p.name for p in path.iterdir()) if path.exists() else []


def test_a_claim_sweeps_old_lock_files_nobody_holds(tmp_path: Path) -> None:
    ps = PublicationStore(BrokerStore(store_path(tmp_path)))
    with ps.claim("u1", "issue", "a.json"):
        pass
    claims = store_path(tmp_path).parent / "claims"
    orphan = claims / ("0" * 32 + ".lock")
    orphan.write_bytes(b"")
    old = time.time() - 3600
    os.utime(orphan, (old, old))
    fresh = claims / ("1" * 32 + ".lock")  # may be a claim between create and flock
    fresh.write_bytes(b"")

    with ps.claim("u1", "issue", "b.json"):
        assert orphan.name not in _claims(tmp_path)
        assert fresh.name in _claims(tmp_path)


def test_a_claim_sweep_keeps_lock_files_someone_holds(tmp_path: Path) -> None:
    ps = PublicationStore(BrokerStore(store_path(tmp_path)))
    with ps.claim("u1", "issue", "a.json"):
        held = _claims(tmp_path)
        old = time.time() - 3600
        for name in held:
            path = store_path(tmp_path).parent / "claims" / name
            os.utime(path, (old, old))
        with ps.claim("u1", "issue", "b.json"):
            assert set(held) <= set(_claims(tmp_path))
    assert _claims(tmp_path) == []  # released claims leave nothing behind
