"""Outbox socket ops: the broker re-validates, caps and stores issue proposals."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

from jailbee.issue_manifest import MAX_BODY_BYTES, MAX_MANIFEST_BYTES
from jailbee.orchestration import wire
from jailbee.outbox import store_db
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


def comment(body: str = "Looks good") -> dict[str, object]:
    return {"type": "comment", "repo": ".", "issue": 7, "body": body}


def comment_file(name: str) -> dict[str, object]:
    return {"type": "comment", "repo": ".", "issue": 7, "body_file": name}


def manifest(*actions: dict[str, object]) -> dict[str, object]:
    return {"version": 1, "actions": list(actions or (comment(),))}


def put(env: Env, *, uuid: str = PLAIN, **args: object) -> wire.Response:
    payload: dict[str, object] = {
        "kind": "issue",
        "slug": "looks-good",
        "manifest": manifest(),
        "bodies": {},
    }
    payload.update(args)
    return env.call(wire.OUTBOX_PUT, uuid=uuid, **payload)


def stored(env: Env, uuid: str = PLAIN) -> list[store_db.Proposal]:
    return store_db.read_proposals(env.rig.root, uuid, "issue")


def test_a_plain_container_stages_lists_and_shows_an_issue_proposal(env: Env) -> None:
    body = "x" * 5000
    staged = put(
        env,
        manifest=manifest(comment_file("0123abcd.md")),
        bodies={"0123abcd.md": body},
    )
    assert staged.ok, staged
    assert staged.result == {"kind": "issue", "name": "001-looks-good.json"}
    (row,) = stored(env)
    assert row.bodies == {"001-looks-good-1.md": body}
    assert json.loads(row.manifest)["actions"][0]["body_file"] == "001-looks-good-1.md"

    listed = env.call(wire.OUTBOX_LIST, uuid=PLAIN, kind="issue")
    assert listed.result == {
        "proposals": [
            {
                "kind": "issue",
                "name": "001-looks-good.json",
                "summary": "comment",
                "actions": 1,
                "created_at": row.created_at,
                "revision": 1,
            }
        ]
    }
    shown = env.call(wire.OUTBOX_SHOW, uuid=PLAIN, kind="issue", name="001-looks-good.json")
    assert shown.result == {
        "kind": "issue",
        "name": "001-looks-good.json",
        "manifest": row.manifest,
        "bodies": row.bodies,
        "revision": 1,
    }


def test_an_orchestrator_may_stage_too(env: Env) -> None:
    assert put(env, uuid="proj-orch").ok
    assert [p.name for p in stored(env, "proj-orch")] == ["001-looks-good.json"]


def test_the_broker_refuses_a_child_whatever_its_cli_says(env: Env) -> None:
    response = put(env, uuid="proj-kid")
    assert response.error_code == wire.REFUSED
    assert "child container" in response.error_detail
    assert stored(env, "proj-kid") == []
    listed = env.call(wire.OUTBOX_LIST, uuid="proj-kid", kind="issue")
    assert listed.result == {"proposals": []}  # reading is not staging


def test_a_child_without_a_round_context_is_still_refused(env: Env) -> None:
    from jailbee.orchestration import query

    env.store.set_recipient("proj-lost", "proj-lost", now=1.0)
    env.listener.open_container("proj-lost", env.rig.root / "proj-lost")
    env.memory.env_roles["proj-lost"] = "child"  # its repo config stopped loading
    query.publish_queries(env.memory)
    assert "proj-lost" not in {ctx.sender.full for ctx in env.memory.contexts.values()}
    assert put(env, uuid="proj-lost").error_code == wire.REFUSED
    assert stored(env, "proj-lost") == []


def test_a_container_without_a_published_view_is_told_to_retry(env: Env) -> None:
    assert put(env, uuid="unknown").error_code == wire.STORE
    assert stored(env, "unknown") == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"manifest": {"version": 1, "actions": [{"type": "bogus"}]}}, "type"),
        ({"manifest": "not an object"}, "manifest"),
        ({"kind": "pr"}, "repo"),  # an issue manifest is no PR source manifest
        ({"kind": "other"}, "issue"),
        ({"slug": "Bad Slug"}, "slug"),
        ({"slug": "a" * 81}, "slug"),
        ({"bodies": {"../x.md": "t"}}, "body"),
        ({"bodies": {"unused.md": "t"}}, "unused.md"),
        ({"bodies": {"a.md": 3}}, "bodies"),
        ({"manifest": manifest(*[comment()] * 101)}, "100"),
    ],
)
def test_invalid_proposals_are_refused_with_nothing_stored(
    env: Env, change: dict[str, object], message: str
) -> None:
    response = put(env, **change)
    assert response.error_code == wire.BAD_REQUEST, response
    assert message in response.error_detail
    assert stored(env) == []


def test_size_caps_hold_on_the_broker(env: Env) -> None:
    oversize_body = put(
        env,
        manifest=manifest(comment_file("b.md")),
        bodies={"b.md": "x" * (MAX_BODY_BYTES + 1)},
    )
    assert oversize_body.error_code == wire.BAD_REQUEST
    oversize_manifest = put(env, manifest=manifest(comment("y" * MAX_MANIFEST_BYTES)))
    assert oversize_manifest.error_code == wire.BAD_REQUEST
    assert stored(env) == []


def test_the_twenty_first_pending_issue_proposal_is_refused(env: Env) -> None:
    for _ in range(store_db.MAX_PENDING):
        assert put(env).ok
    response = put(env)
    assert response.error_code == wire.BAD_REQUEST
    assert "cap of 20" in response.error_detail
    assert len(stored(env)) == 20
    assert put(env, uuid="proj-orch").ok  # the cap is per container


def test_show_of_an_unknown_proposal_is_not_found(env: Env) -> None:
    response = env.call(wire.OUTBOX_SHOW, uuid=PLAIN, kind="issue", name="001-nope.json")
    assert response.error_code == wire.NOT_FOUND


def test_proposals_are_scoped_to_the_calling_container(env: Env) -> None:
    assert put(env).ok
    assert env.call(wire.OUTBOX_LIST, uuid="proj-orch", kind="issue").result == {"proposals": []}
    shown = env.call(wire.OUTBOX_SHOW, uuid="proj-orch", kind="issue", name="001-looks-good.json")
    assert shown.error_code == wire.NOT_FOUND
