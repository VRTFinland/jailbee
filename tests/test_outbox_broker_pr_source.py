"""Outbox socket ops for PR source proposals: re-validated, capped and stored by the broker."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

from jailbee.orchestration import wire
from jailbee.outbox import store_db
from jailbee.pr_manifest import MAX_BODY_BYTES, MAX_MANIFEST_BYTES
from tests.test_orchestration_broker import Rig, _c
from tests.test_orchestration_query import Env

if TYPE_CHECKING:
    from pathlib import Path

PLAIN = "proj-plain"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    instance = Env(Rig(tmp_path, monkeypatch), monkeypatch)
    instance.store.set_recipient(PLAIN, PLAIN, now=1.0)
    instance.listener.open_container(PLAIN, instance.rig.root / PLAIN)
    instance.rig.incus.list_containers.return_value.append(_c(PLAIN, **{"volatile.uuid": PLAIN}))
    instance.rig.tick(instance.memory)
    assert instance.queries.snapshot()[PLAIN].role == "plain"
    yield instance
    instance.listener.stop()


def source(**changes: object) -> dict[str, object]:
    manifest: dict[str, object] = {
        "version": 1,
        "repo": "acme/widgets",
        "pr": None,
        "head_sha": None,
        "source": "refs/heads/topic",
        "source_sha": "a" * 40,
        "base": "main",
        "actions": [{"type": "description", "title": "Title", "body_file": "0123abcd.md"}],
    }
    manifest.update(changes)
    return manifest


def put(env: Env, *, uuid: str = PLAIN, **args: object) -> wire.Response:
    payload: dict[str, object] = {
        "kind": "pr",
        "slug": "source",
        "manifest": source(),
        "bodies": {"0123abcd.md": "Body"},
    }
    payload.update(args)
    return env.call(wire.OUTBOX_PUT, uuid=uuid, **payload)


def stored(env: Env, uuid: str = PLAIN) -> list[store_db.Proposal]:
    return store_db.read_proposals(env.rig.root, uuid, "pr")


def test_a_plain_container_stages_a_source_proposal_into_the_store(env: Env) -> None:
    staged = put(env)
    assert staged.ok, staged
    assert staged.result == {"kind": "pr", "name": "001-source.json"}
    (row,) = stored(env)
    assert row.bodies == {"001-source-1.md": "Body"}
    action = json.loads(row.manifest)["actions"][0]
    assert action["body_file"] == "001-source-1.md"
    listed = env.call(wire.OUTBOX_LIST, uuid=PLAIN, kind="pr").result
    assert [(p["name"], p["summary"]) for p in listed["proposals"]] == [
        ("001-source.json", "description")
    ]
    shown = env.call(wire.OUTBOX_SHOW, uuid=PLAIN, kind="pr", name="001-source.json")
    assert shown.result["manifest"] == row.manifest


def test_an_orchestrator_may_stage_a_source_proposal(env: Env) -> None:
    assert put(env, uuid="proj-orch").ok
    assert [p.name for p in stored(env, "proj-orch")] == ["001-source.json"]


def test_the_broker_refuses_a_child_source_proposal(env: Env) -> None:
    response = put(env, uuid="proj-kid")
    assert response.error_code == wire.REFUSED
    assert "child container" in response.error_detail
    assert stored(env, "proj-kid") == []


def test_a_pushed_pr_update_without_description_is_accepted(env: Env) -> None:
    assert put(env, manifest=source(pr=12, actions=[]), bodies={}).ok


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"manifest": source(source_sha="A" * 40)}, "source_sha"),
        ({"manifest": source(source="topic")}, "refs/"),
        ({"manifest": "not an object"}, "manifest"),
        ({"bodies": {"0123abcd.md": "Body", "unused.md": "t"}}, "unused.md"),
        ({"bodies": {}}, "body_file"),
        ({"bodies": {"0123abcd.md": "x" * (MAX_BODY_BYTES + 1)}}, "64"),
        ({"slug": "Bad Slug"}, "slug"),
    ],
)
def test_invalid_source_proposals_are_refused_with_nothing_stored(
    env: Env, change: dict[str, object], message: str
) -> None:
    response = put(env, **change)
    assert response.error_code == wire.BAD_REQUEST, response
    assert message in response.error_detail
    assert stored(env) == []


def test_an_oversize_source_manifest_is_refused(env: Env) -> None:
    big = source(actions=[{"type": "description", "body": "y" * 60_000, "title": "z" * 300_000}])
    response = put(env, manifest=big, bodies={})
    assert response.error_code == wire.BAD_REQUEST
    assert str(MAX_MANIFEST_BYTES // 1024) in response.error_detail
    assert stored(env) == []


def test_the_twenty_first_pending_pr_proposal_is_refused(env: Env) -> None:
    for _ in range(store_db.MAX_PENDING):
        assert put(env).ok
    response = put(env)
    assert response.error_code == wire.BAD_REQUEST
    assert "cap of 20" in response.error_detail
    assert len(stored(env)) == 20
    # The cap is per kind: issue proposals are still accepted.
    issue = {"version": 1, "actions": [{"type": "comment", "repo": ".", "issue": 7, "body": "x"}]}
    assert env.call(
        wire.OUTBOX_PUT, uuid=PLAIN, kind="issue", slug="c", manifest=issue, bodies={}
    ).ok
