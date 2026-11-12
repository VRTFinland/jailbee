"""Draft batches in the host store: row-locked appends, atomic submit, caps."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from jailbee.orchestration.store import BrokerStore, store_path
from jailbee.outbox.store_db import (
    DraftCapError,
    DraftNotFoundError,
    OutboxStore,
    ProposalCapError,
)


@pytest.fixture
def ob(tmp_path: Path) -> OutboxStore:
    return OutboxStore(BrokerStore(store_path(tmp_path)))


def comment(n: int) -> dict[str, Any]:
    return {"type": "comment", "repo": ".", "issue": n, "body": str(n)}


def passthrough(stem: str, manifest: str, bodies: Any) -> tuple[str, dict[str, str]]:
    return manifest, dict(bodies)


def test_append_creates_then_extends_a_draft_and_bumps_the_revision(ob: OutboxStore) -> None:
    first = ob.draft_append("u1", "issue", "b", comment(1), {"x.md": "text"}, now=1.0)
    second = ob.draft_append("u1", "issue", "b", comment(2), {}, now=2.0)
    assert (first.revision, second.revision) == (1, 2)
    shown = ob.get_draft("u1", "issue", "b")
    assert shown is not None
    assert [a["issue"] for a in json.loads(shown.manifest)["actions"]] == [1, 2]
    assert shown.bodies == {"x.md": "text"}
    assert (shown.created_at, shown.updated_at, shown.action_count) == (1.0, 2.0, 2)


def test_drafts_are_scoped_by_container_and_kind(ob: OutboxStore) -> None:
    ob.draft_append("u1", "issue", "b", comment(1), {}, now=1.0)
    assert ob.get_draft("u2", "issue", "b") is None
    assert ob.get_draft("u1", "pr", "b") is None
    assert [d.name for d in ob.list_drafts("u1")] == ["b"]
    assert ob.list_drafts("u2") == []


def test_a_failed_prepare_or_validate_changes_nothing(ob: OutboxStore) -> None:
    ob.draft_append("u1", "issue", "b", comment(1), {}, now=1.0)
    before = ob.get_draft("u1", "issue", "b")

    def refuse(*args: object) -> Any:
        raise ValueError("no")

    with pytest.raises(ValueError):
        ob.draft_append("u1", "issue", "b", comment(2), {}, now=2.0, prepare=refuse)
    with pytest.raises(ValueError):
        ob.draft_append("u1", "issue", "b", comment(2), {}, now=2.0, validate=refuse)
    assert ob.get_draft("u1", "issue", "b") == before
    with pytest.raises(ValueError):
        ob.draft_append("u1", "issue", "new", comment(2), {}, now=2.0, validate=refuse)
    assert ob.get_draft("u1", "issue", "new") is None


def test_prepare_sees_the_stored_draft(ob: OutboxStore) -> None:
    seen: list[int] = []

    def prepare(manifest: dict[str, Any], bodies: Any, action: dict[str, Any]) -> dict[str, Any]:
        seen.append(len(manifest["actions"]))
        return action

    for n in (1, 2, 3):
        ob.draft_append("u1", "issue", "b", comment(n), {}, now=1.0, prepare=prepare)
    assert seen == [0, 1, 2]


def test_the_number_of_open_drafts_is_capped(ob: OutboxStore) -> None:
    for i in range(3):
        ob.draft_append("u1", "issue", f"d{i}", comment(1), {}, now=1.0, max_drafts=3)
    with pytest.raises(DraftCapError):
        ob.draft_append("u1", "issue", "d3", comment(1), {}, now=1.0, max_drafts=3)
    ob.draft_append("u1", "issue", "d0", comment(2), {}, now=1.0, max_drafts=3)  # existing
    ob.draft_append("u2", "issue", "d3", comment(1), {}, now=1.0, max_drafts=3)  # per container


def test_discard_removes_the_draft_and_only_once(ob: OutboxStore) -> None:
    ob.draft_append("u1", "issue", "b", comment(1), {}, now=1.0)
    ob.discard_draft("u1", "issue", "b")
    assert ob.get_draft("u1", "issue", "b") is None
    with pytest.raises(DraftNotFoundError):
        ob.discard_draft("u1", "issue", "b")


def test_submit_creates_a_numbered_proposal_and_removes_the_draft(ob: OutboxStore) -> None:
    ob.draft_append("u1", "issue", "b", comment(1), {"x.md": "text"}, now=1.0)
    proposal = ob.submit_draft("u1", "issue", "b", "b", passthrough, now=2.0)
    assert proposal.name == "001-b.json"
    assert proposal.bodies == {"x.md": "text"}
    assert ob.get_draft("u1", "issue", "b") is None
    assert [p.name for p in ob.list_proposals("u1", "issue")] == ["001-b.json"]


def test_a_refused_submit_keeps_the_draft(ob: OutboxStore) -> None:
    ob.draft_append("u1", "issue", "b", comment(1), {}, now=1.0)
    for i in range(2):
        ob.put_proposal("u1", "issue", f"p{i}", lambda stem: ("{}", {}), now=1.0)
    with pytest.raises(ProposalCapError):
        ob.submit_draft("u1", "issue", "b", "b", passthrough, now=2.0, cap=2)

    def broken(stem: str, manifest: str, bodies: Any) -> Any:
        raise ValueError("invalid")

    with pytest.raises(ValueError):
        ob.submit_draft("u1", "issue", "b", "b", broken, now=2.0)
    with pytest.raises(DraftNotFoundError):
        ob.submit_draft("u1", "issue", "missing", "b", passthrough, now=2.0)
    assert ob.get_draft("u1", "issue", "b") is not None
    assert len(ob.list_proposals("u1", "issue")) == 2


@pytest.mark.parametrize("existing", [False, True])
def test_concurrent_append_retries_against_changed_snapshot(
    ob: OutboxStore, existing: bool
) -> None:
    if existing:
        ob.draft_append("u1", "issue", "b", comment(0), {}, now=0)
    entered = threading.Event()
    release = threading.Event()
    seen: list[list[int]] = []
    errors: list[BaseException] = []

    def prepare(manifest: dict[str, Any], bodies: Any, action: dict[str, Any]) -> dict[str, Any]:
        seen.append([item["issue"] for item in manifest["actions"]])
        if len(seen) == 1:
            entered.set()
            assert release.wait(5)
        return action

    def append() -> None:
        try:
            ob.draft_append("u1", "issue", "b", comment(1), {}, now=1, prepare=prepare)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=append)
    thread.start()
    try:
        assert entered.wait(5)
        ob.draft_append("u1", "issue", "b", comment(2), {}, now=2)
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert not errors
    initial = [0] if existing else []
    assert seen == [initial, [*initial, 2]]
    draft = ob.get_draft("u1", "issue", "b")
    assert draft is not None
    assert [item["issue"] for item in json.loads(draft.manifest)["actions"]] == [*initial, 2, 1]


def test_another_store_can_write_while_prepare_waits(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    first_store = OutboxStore(BrokerStore(path))
    second_store = OutboxStore(BrokerStore(path))
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def prepare(manifest: dict[str, Any], bodies: Any, action: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        assert release.wait(10)
        return action

    def append() -> None:
        try:
            first_store.draft_append("u1", "issue", "b", comment(1), {}, now=1, prepare=prepare)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=append)
    thread.start()
    try:
        assert entered.wait(5)
        second_store.draft_append("u1", "issue", "other", comment(2), {}, now=2)
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert not errors
    assert second_store.get_draft("u1", "issue", "other") is not None


def test_many_threads_appending_lose_nothing(ob: OutboxStore) -> None:
    ob.draft_append("u1", "issue", "b", comment(0), {}, now=0.0)
    errors: list[BaseException] = []

    def run(n: int) -> None:
        try:
            ob.draft_append("u1", "issue", "b", comment(n), {}, now=float(n))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(n,)) for n in range(1, 13)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not errors
    draft = ob.get_draft("u1", "issue", "b")
    assert draft is not None and draft.revision == 13
    assert sorted(a["issue"] for a in json.loads(draft.manifest)["actions"]) == list(range(13))
