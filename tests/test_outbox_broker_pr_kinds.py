"""Outbox socket ops for PR comments, replies, descriptions, reviews and their drafts."""
# ruff: noqa: F811  (fixtures imported from the sibling test module)

from __future__ import annotations

import json
from typing import Any

import pytest

from jailbee.orchestration import wire
from jailbee.outbox import store_db
from jailbee.pr_manifest import parse_manifest
from tests.test_orchestration_query import Env
from tests.test_outbox_broker_pr_source import PLAIN, env  # noqa: F401  (env is a fixture)

HEAD = "b" * 40


def envelope(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {"version": 1, "repo": "acme/widgets", "pr": 12, "head_sha": HEAD}
    value.update(changes)
    return value


def put(env: Env, action: dict[str, object], bodies: dict[str, str] | None = None) -> wire.Response:
    manifest = {**envelope(), "actions": [action]}
    return env.call(
        wire.OUTBOX_PUT,
        uuid=PLAIN,
        kind="pr",
        slug=str(action["type"]),
        manifest=manifest,
        bodies=bodies or {},
    )


def append(
    env: Env, action: dict[str, object], *, name: str = "b", **extra: object
) -> wire.Response:
    return env.call(
        wire.OUTBOX_DRAFT_APPEND,
        uuid=PLAIN,
        kind="pr",
        name=name,
        action=action,
        bodies=extra.pop("bodies", {}),
        envelope=extra.pop("envelope", envelope()),
        **extra,
    )


def stored(env: Env) -> list[store_db.Proposal]:
    return store_db.read_proposals(env.rig.root, PLAIN, "pr")


def draft_actions(env: Env, name: str) -> list[dict[str, Any]]:
    found = store_db.OutboxStore(env.store).get_draft(PLAIN, "pr", name)
    assert found is not None
    return list(json.loads(found.manifest)["actions"])


def comment(body: str = "hi") -> dict[str, object]:
    return {"path": "src/a.py", "line": 3, "side": "RIGHT", "body": body}


@pytest.mark.parametrize(
    "action",
    [
        {"type": "comment", "body": "General"},
        {"type": "comment", "body": "Answer", "reply_to": 5},
        {"type": "reply", "comment_id": 9, "body": "Thread reply"},
        {"type": "description", "title": "T", "body": "New body"},
        {"type": "review", "event": "COMMENT", "body": "LGTM", "comments": [comment()]},
    ],
)
def test_each_pr_proposal_kind_is_staged_listed_and_shown_from_the_store(
    env: Env, action: dict[str, object]
) -> None:
    staged = put(env, action)
    assert staged.ok, staged
    name = staged.result["name"]
    assert name == f"001-{action['type']}.json"
    (row,) = stored(env)
    manifest = parse_manifest(row.name, row.manifest, row.bodies)
    assert manifest.pr == 12 and len(manifest.actions) == 1
    listed = env.call(wire.OUTBOX_LIST, uuid=PLAIN, kind="pr").result["proposals"]
    assert [(p["name"], p["summary"]) for p in listed] == [(name, action["type"])]
    shown = env.call(wire.OUTBOX_SHOW, uuid=PLAIN, kind="pr", name=name).result
    assert shown["manifest"] == row.manifest


def test_a_pr_body_file_is_renamed_to_the_proposal(env: Env) -> None:
    assert put(env, {"type": "comment", "body_file": "x.md"}, {"x.md": "long"}).ok
    (row,) = stored(env)
    assert row.bodies == {"001-comment-1.md": "long"}


def test_an_invalid_pr_action_is_refused_with_nothing_stored(env: Env) -> None:
    response = put(env, {"type": "review", "event": "APPROVE", "body": "", "comments": []})
    assert response.error_code == wire.BAD_REQUEST
    assert "APPROVE" in response.error_detail
    assert stored(env) == []


def test_a_child_container_cannot_stage_a_pr_comment(env: Env) -> None:
    response = env.call(
        wire.OUTBOX_PUT,
        uuid="proj-kid",
        kind="pr",
        slug="comment",
        manifest={**envelope(), "actions": [{"type": "comment", "body": "x"}]},
        bodies={},
    )
    assert response.error_code == wire.REFUSED


# -- batch drafts ------------------------------------------------------------


def test_a_pr_batch_draft_takes_its_envelope_and_submits_as_one_proposal(env: Env) -> None:
    assert append(env, {"type": "comment", "body": "one"}).ok
    assert append(env, {"type": "reply", "comment_id": 4, "body": "two"}).ok
    listed = env.call(wire.OUTBOX_DRAFT_LIST, uuid=PLAIN, kind="pr").result
    assert listed == {"drafts": [{"kind": "pr", "name": "b", "actions": 2, "revision": 2}]}
    submitted = env.call(wire.OUTBOX_DRAFT_SUBMIT, uuid=PLAIN, kind="pr", name="b")
    assert submitted.result == {"kind": "pr", "name": "001-b.json"}
    (row,) = stored(env)
    manifest = parse_manifest(row.name, row.manifest, row.bodies)
    assert (manifest.repo, manifest.pr, len(manifest.actions)) == ("acme/widgets", 12, 2)
    assert store_db.OutboxStore(env.store).get_draft(PLAIN, "pr", "b") is None


def test_a_batch_action_for_another_pr_is_refused(env: Env) -> None:
    assert append(env, {"type": "comment", "body": "one"}).ok
    response = append(env, {"type": "comment", "body": "two"}, envelope=envelope(pr=13))
    assert response.error_code == wire.BAD_REQUEST
    assert "different repo or PR" in response.error_detail
    assert len(draft_actions(env, "b")) == 1


# -- review drafts -----------------------------------------------------------


def review(*comments: dict[str, object]) -> dict[str, object]:
    return {"type": "review", "event": "COMMENT", "body": "", "comments": list(comments)}


def test_review_comments_accumulate_into_one_review_action(env: Env) -> None:
    assert append(env, review(comment("a")), name="review-12").ok
    assert append(env, review(comment("b")), name="review-12").ok
    (action,) = draft_actions(env, "review-12")
    assert [c["body"] for c in action["comments"]] == ["a", "b"]


def test_a_review_comment_on_a_moved_head_is_refused(env: Env) -> None:
    assert append(env, review(comment("a")), name="review-12").ok
    response = append(
        env, review(comment("b")), name="review-12", envelope=envelope(head_sha="c" * 40)
    )
    assert response.error_code == wire.BAD_REQUEST
    assert "head moved" in response.error_detail


def test_review_and_batch_actions_do_not_mix(env: Env) -> None:
    assert append(env, review(comment("a")), name="review-12").ok
    mixed = append(env, {"type": "comment", "body": "x"}, name="review-12")
    assert mixed.error_code == wire.BAD_REQUEST
    assert append(env, {"type": "comment", "body": "x"}, name="b").ok
    assert append(env, review(comment("a")), name="b").error_code == wire.BAD_REQUEST


def test_review_submit_sets_the_summary_and_checks_the_head(env: Env) -> None:
    assert append(env, review(comment("a")), name="review-12").ok
    moved = env.call(
        wire.OUTBOX_DRAFT_SUBMIT,
        uuid=PLAIN,
        kind="pr",
        name="review-12",
        review={"repo": "acme/widgets", "pr": 12, "head_sha": "c" * 40, "body": "Summary"},
    )
    assert moved.error_code == wire.BAD_REQUEST and "head moved" in moved.error_detail
    submitted = env.call(
        wire.OUTBOX_DRAFT_SUBMIT,
        uuid=PLAIN,
        kind="pr",
        name="review-12",
        review={"repo": "acme/widgets", "pr": 12, "head_sha": HEAD, "body_file": "s.md"},
        bodies={"s.md": "Long summary"},
    )
    assert submitted.result == {"kind": "pr", "name": "001-review.json"}
    (row,) = stored(env)
    manifest = parse_manifest(row.name, row.manifest, row.bodies)
    assert manifest.actions[0].body == "Long summary"
    assert row.bodies == {"001-review-1.md": "Long summary"}


def test_a_review_submit_for_another_repo_is_refused(env: Env) -> None:
    assert append(env, review(comment("a")), name="review-12").ok
    other = {"repo": "acme/other", "pr": 12, "head_sha": HEAD, "body": "s"}
    response = env.call(
        wire.OUTBOX_DRAFT_SUBMIT, uuid=PLAIN, kind="pr", name="review-12", review=other
    )
    assert response.error_code == wire.BAD_REQUEST
    assert "different repo or PR" in response.error_detail
    assert stored(env) == []


def test_a_review_draft_needs_the_review_submit(env: Env) -> None:
    assert append(env, review(comment("a")), name="review-12").ok
    response = env.call(wire.OUTBOX_DRAFT_SUBMIT, uuid=PLAIN, kind="pr", name="review-12")
    assert response.error_code == wire.BAD_REQUEST
    assert "pr review submit" in response.error_detail
    batch = append(env, {"type": "comment", "body": "x"}, name="b")
    assert batch.ok
    wrong = env.call(
        wire.OUTBOX_DRAFT_SUBMIT,
        uuid=PLAIN,
        kind="pr",
        name="b",
        review={"head_sha": HEAD, "body": "s"},
    )
    assert wrong.error_code == wire.BAD_REQUEST


def test_a_pr_draft_is_shown_and_discarded(env: Env) -> None:
    assert append(env, review(comment("a")), name="review-12").ok
    shown = env.call(wire.OUTBOX_DRAFT_SHOW, uuid=PLAIN, kind="pr", name="review-12").result
    assert json.loads(shown["manifest"])["head_sha"] == HEAD
    gone = env.call(wire.OUTBOX_DRAFT_DISCARD, uuid=PLAIN, kind="pr", name="review-12")
    assert gone.ok
    assert store_db.OutboxStore(env.store).list_drafts(PLAIN, "pr") == []


def test_a_body_referenced_only_by_extension_metadata_is_refused(env: Env) -> None:
    action = {"type": "comment", "body": "x", "x-tool": {"body_file": "e.md"}}
    response = put(env, action, {"e.md": "extension"})
    assert response.error_code == wire.BAD_REQUEST
    assert "e.md" in response.error_detail
    assert stored(env) == []
