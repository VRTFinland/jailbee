"""Publication claims across processes: a claim is live only while its owner holds it.

A claim row whose owner process is alive but no longer holds the claim (its
release failed: a leaked row) must not block other processes until that owner
restarts. Each test runs the owner as a real child process.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from jailbee.orchestration.store import BrokerStore, store_path
from jailbee.outbox.publication_db import ClaimBusyError, PublicationStore

KEY = "outbox.claim:u1:issue:001-x.json"


@pytest.fixture
def ps(tmp_path: Path) -> PublicationStore:
    return PublicationStore(BrokerStore(store_path(tmp_path)))


def _child(script: str) -> subprocess.Popen[bytes]:
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE)
    assert child.stdout is not None and child.stdout.readline() == b"ready\n"
    return child


def test_a_live_processs_leaked_claim_does_not_block_another_process(
    ps: PublicationStore, tmp_path: Path
) -> None:
    """The broker's release failed: its row stays, the broker lives on."""
    script = (
        "import time\n"
        "from pathlib import Path\n"
        "from jailbee.orchestration.store import BrokerStore, StoreError, store_path\n"
        "from jailbee.outbox.publication_db import PublicationStore\n"
        f"store = BrokerStore(store_path(Path({str(tmp_path)!r})))\n"
        "ps = PublicationStore(store)\n"
        "with ps.claim('u1', 'issue', '001-x.json'):\n"
        "    def broken(*args, **kwargs):\n"
        "        raise StoreError('disk I/O error')\n"
        "    store.transaction = broken  # the release's DELETE fails\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    child = _child(script)
    try:
        with BrokerStore(store_path(tmp_path)).transaction() as con:
            (value,) = con.execute("SELECT value FROM state WHERE key=?", (KEY,)).fetchone()
        leaked = json.loads(value)
        assert leaked["pid"] == child.pid  # the row is still there, its owner alive
        with ps.claim("u1", "issue", "001-x.json") as claim:
            assert claim.token != leaked["token"]
    finally:
        child.kill()
        child.wait()


def test_a_claim_another_process_holds_is_refused(ps: PublicationStore, tmp_path: Path) -> None:
    script = (
        "import time\n"
        "from pathlib import Path\n"
        "from jailbee.orchestration.store import BrokerStore, store_path\n"
        "from jailbee.outbox.publication_db import PublicationStore\n"
        f"ps = PublicationStore(BrokerStore(store_path(Path({str(tmp_path)!r}))))\n"
        "with ps.claim('u1', 'issue', '001-x.json'):\n"
        "    print('ready', flush=True)\n"
        "    time.sleep(30)\n"
    )
    child = _child(script)
    try:
        with pytest.raises(ClaimBusyError), ps.claim("u1", "issue", "001-x.json"):
            pass
    finally:
        child.kill()
        child.wait()
    # Its owner is gone: the claim is free again.
    with ps.claim("u1", "issue", "001-x.json"):
        pass
