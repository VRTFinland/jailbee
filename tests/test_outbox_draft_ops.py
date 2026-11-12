"""Draft socket ops: host-side @REF expected values, caps, role gates, submit."""
# ruff: noqa: F811  (fixtures imported from the sibling test module)

from __future__ import annotations

import json
from typing import Any

import pytest

from jailbee.issue_manifest import MAX_BODY_BYTES, parse_manifest
from jailbee.orchestration import wire
from jailbee.outbox import store_db
from tests.test_orchestration_query import Env
from tests.test_outbox_broker_ops import PLAIN, env, put  # noqa: F401  (env is a fixture)


def create(ref: str = "bug", **extra: object) -> dict[str, object]:
    return {
        "type": "create",
        "repo": ".",
        "title": "Bug",
        "body": "body",
        "labels": ["bug"],
        "ref": ref,
        **extra,
    }


def on_ref(kind: str, **fields: object) -> dict[str, object]:
    return {"type": kind, "repo": ".", "issue_ref": "bug", **fields}


def append(
    env: Env, action: dict[str, object], *, name: str = "b", uuid: str = PLAIN, **extra: object
) -> wire.Response:
    return env.call(
        wire.OUTBOX_DRAFT_APPEND,
        uuid=uuid,
        kind="issue",
        name=name,
        action=action,
        bodies=extra.pop("bodies", {}),
        **extra,
    )


def draft(env: Env, name: str = "b", uuid: str = PLAIN) -> store_db.Draft | None:
    return store_db.OutboxStore(env.store).get_draft(uuid, "issue", name)


def stored_actions(env: Env, name: str = "b") -> list[dict[str, Any]]:
    found = draft(env, name)
    assert found is not None
    return json.loads(found.manifest)["actions"]


def test_project_draft_derives_ref_membership_without_reader(env: Env) -> None:
    assert append(env, create()).ok
    assert append(env, on_ref("project_add", project="acme/3", expected={"present": True})).ok
    assert stored_actions(env)[1]["expected"] == {"present": False}


def test_project_reader_failure_leaves_draft_and_put_unchanged(env: Env, monkeypatch) -> None:
    from jailbee import issue_outbox, issue_projects
    from jailbee.issue_github import IssueGithubReadError

    monkeypatch.setattr(
        issue_outbox,
        "resolve_repo_targets",
        lambda cfg: {".": issue_outbox.RepoTarget(".", cfg.repo_root, "acme/app")},
    )

    def fail(*args):
        raise IssueGithubReadError("host project read failed")

    monkeypatch.setattr(issue_projects, "resolve_project", fail)
    action = {"type": "project_add", "repo": ".", "issue": 7, "project": "acme/3"}
    assert append(env, {**action, "unknown": True}).error_code == wire.BAD_REQUEST
    assert append(env, create()).ok
    before = draft(env)
    response = append(env, action)
    assert response.error_code == wire.REFUSED
    assert draft(env) == before
    assert not put(env, manifest={"version": 1, "actions": [action]}).ok
    assert store_db.read_proposals(env.rig.root, PLAIN, "issue") == []


def test_append_builds_a_draft_and_submit_publishes_one_proposal(env: Env) -> None:
    assert append(env, create(), bodies={}).ok
    assert append(env, {"type": "comment", "repo": ".", "issue_ref": "bug", "body": "hi"}).ok
    shown = env.call(wire.OUTBOX_DRAFT_SHOW, uuid=PLAIN, kind="issue", name="b")
    assert shown.ok and shown.result["revision"] == 2
    assert len(json.loads(shown.result["manifest"])["actions"]) == 2
    listed = env.call(wire.OUTBOX_DRAFT_LIST, uuid=PLAIN, kind="issue")
    row = {"kind": "issue", "name": "b", "actions": 2, "revision": 2}
    assert listed.result == {"drafts": [row]}
    submitted = env.call(wire.OUTBOX_DRAFT_SUBMIT, uuid=PLAIN, kind="issue", name="b")
    assert submitted.result == {"kind": "issue", "name": "001-b.json"}
    (row,) = store_db.read_proposals(env.rig.root, PLAIN, "issue")
    assert len(parse_manifest(row.name, row.manifest, row.bodies).actions) == 2
    assert draft(env) is None


def test_submit_renames_bodies_like_single_staging(env: Env) -> None:
    big = "x" * 5000
    action = {
        "type": "create",
        "repo": ".",
        "title": "Bug",
        "body_file": "aa.md",
        "labels": [],
        "ref": "bug",
    }
    assert append(env, action, bodies={"aa.md": big}).ok
    assert env.call(wire.OUTBOX_DRAFT_SUBMIT, uuid=PLAIN, kind="issue", name="b").ok
    (row,) = store_db.read_proposals(env.rig.root, PLAIN, "issue")
    assert row.bodies == {"001-b-1.md": big}
    assert json.loads(row.manifest)["actions"][0]["body_file"] == "001-b-1.md"


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (on_ref("edit", title="T2"), {"title": "Bug"}),
        (on_ref("edit", body="B2"), {"body": "body"}),
        (on_ref("edit", title="T2", body="B2"), {"title": "Bug", "body": "body"}),
        (on_ref("labels", add=["x"], remove=["bug"]), {"labels": ["bug"]}),
        (on_ref("state", state="closed", reason="completed"), {"state": "open"}),
        (on_ref("assignees", add=["bob"], remove=[]), {"assignees": []}),
    ],
)
def test_ref_expected_values_are_derived_on_the_host(
    env: Env, action: dict[str, object], expected: dict[str, object]
) -> None:
    assert append(env, create()).ok
    assert append(env, action).ok
    assert stored_actions(env)[1]["expected"] == expected


def test_a_container_supplied_expected_value_for_a_ref_is_overwritten(env: Env) -> None:
    assert append(env, create()).ok
    wrong = {"state": "closed"}
    assert append(env, on_ref("state", state="closed", reason="completed", expected=wrong)).ok
    assert stored_actions(env)[1]["expected"] == {"state": "open"}


def test_ref_expected_body_comes_from_the_stored_body_file(env: Env) -> None:
    long_create = {
        "type": "create",
        "repo": ".",
        "title": "T",
        "body_file": "aa.md",
        "labels": [],
        "ref": "bug",
    }
    assert append(env, long_create, bodies={"aa.md": "long text"}).ok
    assert append(env, on_ref("edit", body="new")).ok
    assert stored_actions(env)[1]["expected"] == {"body": "long text"}


@pytest.mark.parametrize(
    ("action", "message"),
    [
        (on_ref("comment", body="x", issue_ref="nope"), "nope"),
        (on_ref("comment", body="x", repo="github:other/repo"), "same repo"),
        (on_ref("labels", add=["BUG"], remove=[]), "already present"),
        (on_ref("labels", add=[], remove=["other"]), "not present"),
        (on_ref("assignees", add=["bob"], remove=["bob"]), "both add and remove"),
        (on_ref("assignees", add=["bob"], remove=["carol"]), "is not assigned"),
        (on_ref("assignees", add=[1], remove=[]), "add"),
        (on_ref("assignees", add=["bob"], expected={"assignees": [1]}), "expected"),
        (on_ref("assignees", add=["bad login"], remove=[]), "invalid GitHub login"),
        (on_ref("state", state="open"), "state"),
    ],
)
def test_a_bad_ref_action_is_refused_and_leaves_the_draft_alone(
    env: Env, action: dict[str, object], message: str
) -> None:
    assert append(env, create()).ok
    before = draft(env)
    response = append(env, action)
    assert response.error_code == wire.BAD_REQUEST, response
    assert message in response.error_detail
    assert draft(env) == before


def test_a_refused_first_append_creates_no_draft(env: Env) -> None:
    assert append(env, {"type": "bogus"}).error_code == wire.BAD_REQUEST
    assert append(env, create(), name="Bad Name").error_code == wire.BAD_REQUEST
    assert append(env, create(), name="a" * 41).error_code == wire.BAD_REQUEST
    assert store_db.OutboxStore(env.store).list_drafts(PLAIN) == []


def test_caps_hold_on_append(env: Env) -> None:
    assert append(env, create(), bodies={"big.md": "x" * (MAX_BODY_BYTES + 1)}).error_code == (
        wire.BAD_REQUEST
    )
    for _ in range(100):
        assert append(env, {"type": "comment", "repo": ".", "issue": 7, "body": "c"}).ok
    over = append(env, {"type": "comment", "repo": ".", "issue": 7, "body": "c"})
    assert over.error_code == wire.BAD_REQUEST and "100" in over.error_detail
    assert len(stored_actions(env)) == 100


def test_the_number_of_open_drafts_is_capped(env: Env) -> None:
    for i in range(store_db.MAX_DRAFTS):
        assert append(env, create(), name=f"d{i}").ok
    over = append(env, create(), name="one-more")
    assert over.error_code == wire.BAD_REQUEST and "cap of 20" in over.error_detail
    assert append(env, create("other"), name="d0").ok  # an existing draft still grows
    assert append(env, create(), name="d0", uuid="proj-orch").ok  # per container


def test_submit_enforces_the_pending_cap_and_keeps_the_draft(env: Env) -> None:
    for _ in range(store_db.MAX_PENDING):
        assert put(env).ok
    assert append(env, create()).ok
    response = env.call(wire.OUTBOX_DRAFT_SUBMIT, uuid=PLAIN, kind="issue", name="b")
    assert response.error_code == wire.BAD_REQUEST and "cap of 20" in response.error_detail
    assert draft(env) is not None


def test_submit_revalidates_the_stored_draft(env: Env) -> None:
    """A draft the host could not accept now (here: planted directly in the store, as an
    older host version might have left it) is refused on submit and kept."""
    store_db.OutboxStore(env.store).draft_append(
        PLAIN, "issue", "b", {"type": "bogus"}, {}, now=1.0
    )
    response = env.call(wire.OUTBOX_DRAFT_SUBMIT, uuid=PLAIN, kind="issue", name="b")
    assert response.error_code == wire.BAD_REQUEST
    assert draft(env) is not None
    assert store_db.read_proposals(env.rig.root, PLAIN, "issue") == []


def test_discard_removes_the_draft(env: Env) -> None:
    assert append(env, create()).ok
    assert env.call(wire.OUTBOX_DRAFT_DISCARD, uuid=PLAIN, kind="issue", name="b").ok
    assert draft(env) is None
    again = env.call(wire.OUTBOX_DRAFT_DISCARD, uuid=PLAIN, kind="issue", name="b")
    assert again.error_code == wire.NOT_FOUND
    missing = env.call(wire.OUTBOX_DRAFT_SHOW, uuid=PLAIN, kind="issue", name="b")
    assert missing.error_code == wire.NOT_FOUND
    gone = env.call(wire.OUTBOX_DRAFT_SUBMIT, uuid=PLAIN, kind="issue", name="b")
    assert gone.error_code == wire.NOT_FOUND


def test_drafts_are_scoped_to_the_calling_container(env: Env) -> None:
    assert append(env, create()).ok
    other = env.call(wire.OUTBOX_DRAFT_SHOW, uuid="proj-orch", kind="issue", name="b")
    assert other.error_code == wire.NOT_FOUND
    listed = env.call(wire.OUTBOX_DRAFT_LIST, uuid="proj-orch", kind="issue")
    assert listed.result == {"drafts": []}


def test_the_broker_refuses_a_child_every_draft_write_but_not_reads(env: Env) -> None:
    for op, args in [
        (
            wire.OUTBOX_DRAFT_APPEND,
            {"kind": "issue", "name": "b", "action": create(), "bodies": {}},
        ),
        (wire.OUTBOX_DRAFT_SUBMIT, {"kind": "issue", "name": "b"}),
        (wire.OUTBOX_DRAFT_DISCARD, {"kind": "issue", "name": "b"}),
    ]:
        response = env.call(op, uuid="proj-kid", **args)
        assert response.error_code == wire.REFUSED, op
        assert "child container" in response.error_detail
    assert store_db.OutboxStore(env.store).list_drafts("proj-kid") == []
    assert env.call(wire.OUTBOX_DRAFT_LIST, uuid="proj-kid", kind="issue").ok
    shown = env.call(wire.OUTBOX_DRAFT_SHOW, uuid="proj-kid", kind="issue", name="b")
    assert shown.error_code == wire.NOT_FOUND  # reading is allowed; there is just none


def test_an_issue_action_is_refused_in_a_pr_draft(env: Env) -> None:
    response = env.call(
        wire.OUTBOX_DRAFT_APPEND, uuid=PLAIN, kind="pr", name="b", action=create(), bodies={}
    )
    assert response.error_code == wire.BAD_REQUEST


def test_malformed_arguments_are_bad_requests(env: Env) -> None:
    assert env.call(wire.OUTBOX_DRAFT_APPEND, uuid=PLAIN, kind="issue", name="b").error_code == (
        wire.BAD_REQUEST
    )
    assert append(env, "not an object").error_code == wire.BAD_REQUEST  # type: ignore[arg-type]
    assert env.call(wire.OUTBOX_DRAFT_SUBMIT, uuid=PLAIN, kind="issue", name=3).error_code == (
        wire.BAD_REQUEST
    )
