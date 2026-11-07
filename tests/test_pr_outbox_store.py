"""PR review/comment/reply/description proposals publish from the host outbox store.

The proposals are staged in ``broker.db`` (keyed by the instance uuid); the host
reads, journals and removes them there, never through ``incus exec``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from jailbee import pr, pr_outbox
from jailbee.db import state_dir
from jailbee.outbox import publication_db, store_db
from jailbee.outbox_io import proposal_digest
from jailbee.pr_flow import PrScope

UUID = "uuid-1"
FULL = "proj-c1"
CREATED = "2026-11-07T10:00:00Z"
REPO = "acme/widgets"


def manifest(*actions: dict[str, Any], pr_number: int | None = 12) -> dict[str, Any]:
    return {
        "version": 1,
        "repo": REPO,
        "pr": pr_number,
        "head_sha": "a" * 40,
        "actions": list(actions),
    }


COMMENT = {"type": "comment", "body": "General"}
REPLY = {"type": "reply", "comment_id": 7, "body_file": "x.md"}
DESCRIPTION = {"type": "description", "title": "T", "body": "New body"}
REVIEW = {
    "type": "review",
    "event": "COMMENT",
    "body": "Summary",
    "comments": [{"path": "a.py", "line": 3, "side": "RIGHT", "body": "nit"}],
}


def stage(data: dict[str, Any], bodies: dict[str, str] | None = None, slug: str = "p") -> str:
    def build(stem: str) -> tuple[str, dict[str, str]]:
        text = json.dumps(data)
        renamed = {}
        for index, (old, body) in enumerate((bodies or {}).items(), 1):
            text = text.replace(old, f"{stem}-{index}.md")
            renamed[f"{stem}-{index}.md"] = body
        return text, renamed

    root = state_dir() / "orchestration"
    return store_db.host_store(root).put_proposal(UUID, "pr", slug, build, now=1.0).name


def rows() -> list[store_db.Proposal]:
    return store_db.read_proposals(state_dir() / "orchestration", UUID, "pr")


def sent() -> list[publication_db.SentRecord]:
    return publication_db.read_sent(publication_db.default_root(), uuid=UUID)


@pytest.fixture
def incus(mocker):
    fake = mocker.Mock()
    fake.list_containers.return_value = [
        {"name": FULL, "created_at": CREATED, "config": {"volatile.uuid": UUID}}
    ]
    return fake


@pytest.fixture
def cfg(make_cfg, tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def github(mocker, cfg):
    """GitHub mutations and the gates, faked: every manifest resolves to PR #12."""
    info = pr.PrInfo(12, "topic", "a" * 40, "OPEN", "main")

    def target(_cfg, _incus, _container, parsed, **_kw):
        return pr_outbox.Target(parsed, info, False, PrScope.for_repo(cfg))

    mocker.patch.object(pr_outbox, "resolve_target", side_effect=target)
    mocker.patch.object(pr, "gh_login", return_value="me")
    mocker.patch.object(pr, "pr_body", return_value="Old body", create=True)
    calls = mocker.Mock()
    mocker.patch.object(pr, "add_issue_comment", side_effect=calls.comment)
    mocker.patch.object(pr, "reply_to_review_comment", side_effect=calls.reply)
    mocker.patch.object(pr, "submit_review", side_effect=calls.review)
    mocker.patch.object(pr, "edit_pr", side_effect=calls.edit)
    calls.comment.return_value = "https://github.com/acme/widgets/pull/12#c1"
    calls.reply.return_value = "https://github.com/acme/widgets/pull/12#r1"
    calls.review.return_value = "https://github.com/acme/widgets/pull/12#rev"
    calls.edit.return_value = None
    return calls


def review_apply(cfg, incus, name: str, **kw: Any) -> int:
    return pr_outbox.offer_pending_comments(
        cfg,
        incus,
        FULL,
        "c1",
        pr_number=None,
        confirm=lambda _n: True,
        manifest_names=(name,),
        publisher="user",
        **kw,
    )


def test_read_outbox_reads_the_store_without_exec(incus):
    name = stage(manifest(REPLY), {"x.md": "Reply text"})
    outbox = pr_outbox.read_outbox(incus, FULL, uid=1000, strict=True)
    assert outbox.uuid == UUID
    assert outbox.manifest_names == [name]
    parsed = pr_outbox.parse_manifest(name, outbox.files[name], outbox.files)
    assert parsed.actions[0].body == "Reply text"
    incus.exec.assert_not_called()


@pytest.mark.parametrize(
    ("action", "call", "kind"),
    [
        (COMMENT, "comment", "comment"),
        (REPLY, "reply", "reply"),
        (REVIEW, "review", "review"),
        (DESCRIPTION, "edit", "description"),
    ],
)
def test_each_pr_kind_publishes_from_the_store(cfg, incus, github, action, call, kind):
    name = stage(manifest(action), {"x.md": "Reply text"} if action is REPLY else None)
    assert review_apply(cfg, incus, name) == 0
    getattr(github, call).assert_called_once()
    assert rows() == []  # removed in the transaction that settled the last action
    (record,) = sent()
    assert (record.outbox, record.kind, record.manifest, record.state) == (
        "review",
        kind,
        name,
        "applied",
    )
    assert publication_db.read_journals(publication_db.default_root(), UUID, "pr") == {}
    incus.exec.assert_not_called()


def test_a_refused_action_stays_pending_and_is_retried(cfg, incus, github):
    name = stage(manifest(COMMENT, REPLY), {"x.md": "Reply text"})
    github.reply.side_effect = pr.PrError("422 Unprocessable")
    assert review_apply(cfg, incus, name) == 1
    assert len(rows()) == 1
    journal = publication_db.read_journals(publication_db.default_root(), UUID, "pr")[("pr", name)]
    assert [(a.index, a.state) for a in journal.actions] == [(0, "applied")]
    github.reply.side_effect = None
    assert review_apply(cfg, incus, name) == 0
    assert github.comment.call_count == 1  # never reposted
    assert github.reply.call_count == 2
    assert rows() == []


def test_a_crash_mid_action_leaves_it_uncertain_and_blocks_a_rerun(cfg, incus, github):
    name = stage(manifest(COMMENT))
    github.comment.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        review_apply(cfg, incus, name)
    github.comment.side_effect = None
    assert review_apply(cfg, incus, name) == 1
    assert github.comment.call_count == 1
    assert [r.name for r in rows()] == [name]


def test_a_changed_proposal_is_refused_against_the_shown_revision(cfg, incus, github):
    name = stage(manifest(COMMENT))
    outbox = pr_outbox.read_outbox(incus, FULL, uid=None)
    store_db.host_store(state_dir() / "orchestration").edit_proposal(
        UUID, "pr", name, None, json.dumps(manifest({"type": "comment", "body": "Other"}))
    )
    assert review_apply(cfg, incus, name, outbox=outbox) == 1
    github.comment.assert_not_called()


def test_drop_manifest_deletes_the_row_and_its_bodies(incus):
    name = stage(manifest(REPLY), {"x.md": "Reply text"})
    outbox = pr_outbox.read_outbox(incus, FULL, uid=None)
    removed = pr_outbox.drop_manifest(incus, FULL, outbox, name, uid=None)
    assert removed == [name, "001-p-1.md"]
    assert rows() == []
    incus.exec.assert_not_called()


def test_record_consumed_journals_a_used_description(incus):
    name = stage(manifest(DESCRIPTION, pr_number=None))
    pr_outbox.record_consumed(
        incus, FULL, name, 0, "https://github.com/acme/widgets/pull/30", uid=None
    )
    assert rows() == []
    (record,) = sent()
    assert (record.kind, record.url) == ("description", "https://github.com/acme/widgets/pull/30")
    incus.exec.assert_not_called()


def test_outbox_pr_references_come_from_the_store(incus):
    stage(manifest(COMMENT), slug="a")
    stage(manifest(COMMENT, pr_number=13), slug="b")
    refs = pr_outbox.outbox_pr_references(incus, FULL, uid=None, repo=REPO, exclude={13})
    assert refs == {12: ("001-a.json",)}
    incus.exec.assert_not_called()


def test_pending_pr_text_offers_a_store_description(cfg, incus, mocker):
    mocker.patch.object(pr_outbox, "scope_slug", return_value=REPO)
    name = stage(manifest(DESCRIPTION, pr_number=None))
    found = pr_outbox.pending_pr_text(
        cfg, incus, FULL, scope=PrScope.for_repo(cfg), source_branch="topic", uid=None
    )
    assert found is not None
    assert (found.manifest, found.index, found.text.body) == (name, 0, "New body")
    incus.exec.assert_not_called()


def test_journal_progress_marks_applied_indices(incus):
    name = stage(manifest(COMMENT, REPLY), {"x.md": "Reply text"})
    row = rows()[0]
    digest = proposal_digest(row.name, row.manifest, row.bodies)
    pubs = publication_db.open_publications()
    with pubs.claim(UUID, "pr", name) as claim:
        pubs.begin(claim, digest=digest, action_count=2, container_full=FULL, now=1.0)
        pubs.mark_prepared(claim, digest, 1, repo=REPO, now=1.0)
        pubs.settle(claim, digest, 1, outcome="applied", url="https://x/r", now=1.0)
    outbox = pr_outbox.read_outbox(incus, FULL, uid=None)
    progress = pr_outbox._publication_progress(outbox, name, 2)
    assert progress.applied == frozenset({1})
    assert progress.urls == {"1": "https://x/r"}


def _crash(cfg, incus, github, name):
    github.comment.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        review_apply(cfg, incus, name)
    github.comment.side_effect = None


def test_an_uncertain_action_resolved_as_landed_completes_the_proposal(cfg, incus, github):
    name = stage(manifest(COMMENT))
    _crash(cfg, incus, github, name)
    outbox = pr_outbox.read_outbox(incus, FULL, uid=None)
    assert pr_outbox.uncertain_actions(outbox, name) == [0]
    url = "https://github.com/acme/widgets/pull/12#issuecomment-9"
    assert pr_outbox.resolve_uncertain(incus, FULL, name, 0, landed_url=url) is True
    assert rows() == []
    (record,) = sent()
    assert (record.state, record.url) == ("applied", url)
    assert github.comment.call_count == 1


def test_an_uncertain_action_resolved_for_retry_is_published_again(cfg, incus, github):
    name = stage(manifest(COMMENT))
    _crash(cfg, incus, github, name)
    assert pr_outbox.resolve_uncertain(incus, FULL, name, 0, landed_url=None) is False
    assert review_apply(cfg, incus, name) == 0
    assert github.comment.call_count == 2
    assert rows() == []


def test_only_an_uncertain_action_can_be_resolved(incus):
    name = stage(manifest(COMMENT))
    with pytest.raises(pr_outbox.OutboxError, match="no uncertain action 0"):
        pr_outbox.resolve_uncertain(incus, FULL, name, 0, landed_url=None)
