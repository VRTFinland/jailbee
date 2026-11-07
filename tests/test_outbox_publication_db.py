"""Publication state in broker.db: claims, journals, crash recovery, sent records."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jailbee.orchestration.store import BrokerStore, store_path
from jailbee.outbox import publication_db as pdb
from jailbee.outbox.publication_db import ClaimBusyError, PublicationStore, SentEntry
from jailbee.outbox.store_db import OutboxStore
from jailbee.outbox_io import JournalError, proposal_digest

MANIFEST = '{"version":1,"actions":[]}'


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def ps(root: Path) -> PublicationStore:
    return PublicationStore(BrokerStore(store_path(root)))


def _stage(root: Path, uuid: str = "u1", body: str = "text") -> tuple[str, str]:
    put = OutboxStore(BrokerStore(store_path(root))).put_proposal(
        uuid, "issue", "fix", lambda stem: (MANIFEST, {f"{stem}-1.md": body}), now=1.0
    )
    return put.name, proposal_digest(put.name, put.manifest, put.bodies)


def _entry(name: str, digest: str, index: int, *, uuid: str = "u1") -> SentEntry:
    return SentEntry(
        uuid=uuid,
        container_prefix="acme",
        container="acme-feat",
        publisher="user",
        outbox="issue",
        manifest=name,
        digest=digest,
        index=index,
        kind="comment",
        repo="o/r",
        target=7,
        url=None,
        title=None,
        content="hello",
    )


def _pending(root: Path, uuid: str = "u1") -> list[str]:
    return [p.name for p in OutboxStore(BrokerStore(store_path(root))).list_proposals(uuid)]


def test_applying_the_last_action_records_sent_and_removes_the_proposal(
    ps: PublicationStore, root: Path
) -> None:
    name, digest = _stage(root)
    with ps.claim("u1", "issue", name) as claim:
        ps.begin(claim, digest=digest, action_count=2, container_full="acme-feat", now=1.0)
        for index in (0, 1):
            ps.mark_prepared(claim, digest, index, repo="o/r", now=2.0)
            settled = ps.settle(
                claim,
                digest,
                index,
                outcome="applied",
                url=f"https://github.com/o/r/issues/{index + 1}",
                sent=_entry(name, digest, index),
                now=3.0,
            )
            assert settled.finalized is (index == 1)
            assert _pending(root) == ([] if index == 1 else [name])
    records = pdb.read_sent(root, uuid="u1")
    assert [(r.index, r.state, r.url) for r in records] == [
        (1, "applied", "https://github.com/o/r/issues/2"),
        (0, "applied", "https://github.com/o/r/issues/1"),
    ]
    assert pdb.read_journals(root, "u1") == {}  # archived with the proposal


def test_a_crash_after_prepared_loads_as_uncertain_and_keeps_the_proposal(
    ps: PublicationStore, root: Path
) -> None:
    name, digest = _stage(root)
    with ps.claim("u1", "issue", name) as claim:
        ps.begin(claim, digest=digest, action_count=1, container_full="acme-feat", now=1.0)
        ps.mark_prepared(claim, digest, 0, repo="o/r", now=2.0)
    # No settle: the process died between the mutation and recording it.
    journal = ps.load("u1", "issue", name)
    assert journal is not None
    assert [a.state for a in journal.actions] == ["uncertain"]
    assert journal.actions[0].detail == pdb.RECOVERED_DETAIL
    assert pdb.read_journals(root, "u1")[("issue", name)].uncertain
    assert _pending(root) == [name]
    assert pdb.read_sent(root) == []


def test_archive_is_refused_while_an_outcome_is_uncertain(ps: PublicationStore, root: Path) -> None:
    name, digest = _stage(root)
    with ps.claim("u1", "issue", name) as claim:
        ps.begin(claim, digest=digest, action_count=2, container_full="acme-feat", now=1.0)
        ps.mark_prepared(claim, digest, 0, repo="o/r", now=2.0)
        with pytest.raises(JournalError, match="uncertain"):
            ps.archive("u1", "issue", name, now=3.0)
        ps.settle(claim, digest, 0, outcome="uncertain", detail="boom", now=3.0)
        with pytest.raises(JournalError, match="uncertain"):
            ps.archive("u1", "issue", name, now=3.0)
        ps.resolve(claim, digest, 0, applied=None, sent_outbox="issue", now=4.0)
    assert ps.archive("u1", "issue", name, now=5.0) is True
    assert ps.load("u1", "issue", name) is None


def test_uncertain_outcome_keeps_a_safe_detail_and_an_uncertain_sent_record(
    ps: PublicationStore, root: Path
) -> None:
    name, digest = _stage(root)
    with ps.claim("u1", "issue", name) as claim:
        ps.begin(claim, digest=digest, action_count=1, container_full="acme-feat", now=1.0)
        ps.mark_prepared(claim, digest, 0, repo="o/r", now=2.0)
        ps.settle(
            claim,
            digest,
            0,
            outcome="uncertain",
            detail="token=secret leaked",
            sent=_entry(name, digest, 0),
            now=3.0,
        )
    journal = ps.load("u1", "issue", name)
    assert (
        journal is not None and journal.actions[0].detail == "GitHub mutation outcome is uncertain"
    )
    assert [r.state for r in pdb.read_sent(root)] == ["uncertain"]


def test_resolving_applied_marks_sent_applied_and_finalizes(
    ps: PublicationStore, root: Path
) -> None:
    name, digest = _stage(root)
    with ps.claim("u1", "issue", name) as claim:
        ps.begin(claim, digest=digest, action_count=1, container_full="acme-feat", now=1.0)
        ps.mark_prepared(claim, digest, 0, repo="o/r", now=2.0)
        ps.settle(claim, digest, 0, outcome="uncertain", sent=_entry(name, digest, 0), now=3.0)
        settled = ps.resolve(
            claim,
            digest,
            0,
            applied=("https://github.com/o/r/issues/9", 9),
            sent_outbox="issue",
            now=4.0,
        )
    assert settled.finalized
    assert _pending(root) == []
    (record,) = pdb.read_sent(root)
    assert (record.state, record.url, record.target) == (
        "applied",
        "https://github.com/o/r/issues/9",
        9,
    )


def test_a_proposal_changed_under_publication_is_not_removed(
    ps: PublicationStore, root: Path
) -> None:
    name, digest = _stage(root)
    with ps.claim("u1", "issue", name) as claim:
        ps.begin(claim, digest=digest, action_count=1, container_full="acme-feat", now=1.0)
        ps.mark_prepared(claim, digest, 0, repo="o/r", now=2.0)
        with BrokerStore(store_path(root)).transaction() as con:
            con.execute("UPDATE outbox_body SET text='edited'")
        settled = ps.settle(
            claim, digest, 0, outcome="applied", url="https://github.com/o/r/issues/1", now=3.0
        )
    assert (settled.finalized, settled.changed) == (False, True)
    assert _pending(root) == [name]


def test_a_changed_digest_replaces_only_an_untouched_journal(
    ps: PublicationStore, root: Path
) -> None:
    name, digest = _stage(root)
    with ps.claim("u1", "issue", name) as claim:
        ps.begin(claim, digest=digest, action_count=1, container_full="acme-feat", now=1.0)
        replaced = ps.begin(claim, digest="b" * 64, action_count=1, container_full="f", now=1.0)
        assert replaced.digest == "b" * 64
        ps.mark_prepared(claim, "b" * 64, 0, repo="o/r", now=2.0)
        with pytest.raises(JournalError, match="changed after recorded progress"):
            ps.begin(claim, digest=digest, action_count=1, container_full="f", now=3.0)


def test_a_second_live_claim_is_refused_and_is_free_after_release(
    ps: PublicationStore, root: Path
) -> None:
    name, _ = _stage(root)
    other = PublicationStore(BrokerStore(store_path(root)))
    with ps.claim("u1", "issue", name):
        # Reentrant in the same thread (publish holds it across apply).
        with ps.claim("u1", "issue", name):
            pass
        with BrokerStore(store_path(root)).transaction() as con:
            (value,) = con.execute(
                "SELECT value FROM state WHERE key LIKE 'outbox.claim:%'"
            ).fetchone()
        owner = json.loads(value)
        assert owner["pid"] == os.getpid()
        # Another process holding it: simulate by swapping the token under us.
        script = (
            "import sys, time; sys.stdout.write('ready\\n'); sys.stdout.flush(); time.sleep(30)"
        )
        child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE)
        try:
            assert child.stdout is not None and child.stdout.readline() == b"ready\n"
            with BrokerStore(store_path(root)).transaction() as con:
                con.execute(
                    "UPDATE state SET value=? WHERE key LIKE 'outbox.claim:%'",
                    (json.dumps({"token": "x", "pid": child.pid, "start": None}),),
                )
            import threading

            errors: list[Exception] = []

            def contend() -> None:
                try:
                    with other.claim("u1", "issue", name):
                        pass
                except ClaimBusyError as exc:
                    errors.append(exc)

            thread = threading.Thread(target=contend)
            thread.start()
            thread.join()
            assert errors, "a live owner's claim must refuse a second publisher"
        finally:
            child.kill()
            child.wait()


def test_a_dead_owners_claim_is_taken_over_and_its_writes_refused(
    ps: PublicationStore, root: Path
) -> None:
    name, digest = _stage(root)
    with ps.claim("u1", "issue", name) as stale:
        ps.begin(stale, digest=digest, action_count=1, container_full="f", now=1.0)
        # The owner "died": its pid no longer exists.
        with BrokerStore(store_path(root)).transaction() as con:
            con.execute(
                "UPDATE state SET value=? WHERE key LIKE 'outbox.claim:%'",
                (json.dumps({"token": "dead", "pid": 2**22 + 12345, "start": None}),),
            )
        import threading

        taken: list[str] = []

        def take_over() -> None:
            with PublicationStore(BrokerStore(store_path(root))).claim("u1", "issue", name) as c:
                taken.append(c.token)

        thread = threading.Thread(target=take_over)
        thread.start()
        thread.join()
        assert taken
        with pytest.raises(ClaimBusyError, match="claim was lost"):
            ps.mark_prepared(stale, digest, 0, repo="o/r", now=2.0)


def test_review_and_source_records_keep_their_outbox(ps: PublicationStore, root: Path) -> None:
    for outbox, kind in (("review", "comment"), ("source", "pr")):
        entry = SentEntry(
            "u1",
            "acme",
            "acme-feat",
            "user",
            outbox,
            "001.json",
            "d",
            0,
            kind,
            "o/r",
            3,
            "https://x",
            None,
            "c",
        )
        ps.record_sent(entry, "applied", now=1.0)
    assert sorted((r.outbox, r.kind) for r in pdb.read_sent(root)) == [
        ("review", "comment"),
        ("source", "pr"),
    ]


def test_sent_reads_are_per_instance_and_counted(ps: PublicationStore, root: Path) -> None:
    ps.record_sent(_entry("a.json", "d", 0, uuid="u1"), "applied", now=1.0)
    ps.record_sent(_entry("a.json", "d", 1, uuid="u1"), "applied", now=2.0)
    ps.record_sent(_entry("b.json", "d", 0, uuid="u2"), "uncertain", now=3.0)
    assert [r.manifest for r in ps.sent_for("u2")] == ["b.json"]
    assert [r.index for r in ps.sent_for("u1", limit=1)] == [1]
    assert pdb.sent_counts(root) == ({"u1": 2, "u2": 1}, {"acme": 3})


def test_reads_without_a_store_are_empty(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere"
    assert pdb.read_sent(missing) == []
    assert pdb.read_journals(missing, "u1") == {}
    assert pdb.sent_counts(missing) == ({}, {})
    assert not store_path(missing).exists()


def test_another_thread_of_this_process_cannot_take_a_live_claim(
    ps: PublicationStore, root: Path
) -> None:
    import threading

    name, _ = _stage(root)
    errors: list[Exception] = []

    def contend() -> None:
        try:
            with PublicationStore(BrokerStore(store_path(root))).claim("u1", "issue", name):
                pass
        except ClaimBusyError as exc:
            errors.append(exc)

    with ps.claim("u1", "issue", name):
        thread = threading.Thread(target=contend)
        thread.start()
        thread.join()
    assert errors, "a live claim of the same process must refuse another thread"


def test_a_leaked_claim_of_this_process_is_taken_over(ps: PublicationStore, root: Path) -> None:
    """A release that failed leaves this process's row behind; it must not wedge a
    long-running broker until restart."""
    name, _ = _stage(root)
    with BrokerStore(store_path(root)).transaction() as con:
        con.execute(
            "INSERT INTO state(key, value) VALUES(?, ?)",
            (
                f"outbox.claim:u1:issue:{name}",
                json.dumps({"token": "leaked", "pid": os.getpid(), "start": None}),
            ),
        )
    with ps.claim("u1", "issue", name) as claim:
        assert claim.token != "leaked"
