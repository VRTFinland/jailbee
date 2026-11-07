"""Deletion plans preserve raw input and never guess body exclusivity."""

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from jailbee.outbox.delete import DeleteSelection, plan_delete
from jailbee.outbox.inspect import build_views
from jailbee.outbox.models import ContainerView, OutboxError, ProposalId
from jailbee.outbox_io import proposal_digest
from tests.outbox_journal_fake import MemoryJournals, journal_key
from tests.outbox_support import IDENTITY, issue_files, pr_files, store


def inspected(tmp_path, kind, files, *, rejected=(), journals=None, pr_journals=None):
    snapshot = store(kind, files, rejected=rejected)
    views = build_views(
        IDENTITY,
        (snapshot,),
        issue_journals=journals.snapshot() if journals is not None else None,
        pr_journals=pr_journals,
    )
    return ContainerView(IDENTITY, IDENTITY.full_name, True, None, (snapshot,), views)


def test_comment_zero_preserves_raw_fields_and_bodies(tmp_path):
    files = pr_files()
    raw = json.loads(files["001.json"])
    raw["extension"] = {"keep": True}
    review = raw["actions"][0]
    review["body_file"] = "review.md"
    del review["body"]
    review["comments"][1] = {
        "path": "a.py",
        "line": 4,
        "start_line": 2,
        "side": "LEFT",
        "start_side": "RIGHT",
        "body_file": "line.md",
        "extension": [1, 2],
    }
    files.update({"001.json": json.dumps(raw), "review.md": "Review", "line.md": "Line"})
    view = inspected(tmp_path, "pr", files)
    plan = plan_delete(view, ProposalId("pr", "001.json"), DeleteSelection(action=0, comment=0))
    expected = json.loads(files["001.json"])
    expected["actions"][0]["comments"] = [raw["actions"][0]["comments"][1]]
    assert json.loads(plan.new_text) == expected
    assert plan.delete_names == ()
    assert plan.removed_actions == ()
    assert plan.removed_comments == ((0, 0),)
    assert plan.expected_revision == view.proposals[0].revision
    assert files["001.json"] == view.proposals[0].raw_text
    assert plan.summary
    with pytest.raises(FrozenInstanceError):
        plan.new_text = None


def test_action_preserves_issue_expected_and_body_reference(tmp_path):
    files = issue_files()
    raw = json.loads(files["001.json"])
    raw["actions"].append(
        {
            "type": "edit",
            "repo": ".",
            "issue": 43,
            "body_file": "edit.md",
            "expected": {"body": "Old"},
        }
    )
    files.update({"001.json": json.dumps(raw), "edit.md": "New"})
    plan = plan_delete(
        inspected(tmp_path, "issue", files),
        ProposalId("issue", "001.json"),
        DeleteSelection(action=2),
    )
    assert json.loads(plan.new_text) == {
        "version": 1,
        "actions": [
            {
                "type": "create",
                "repo": ".",
                "ref": "new",
                "title": "Example",
                "body_file": "body.md",
            },
            {"type": "comment", "repo": ".", "issue_ref": "new", "body": "Follow-up"},
            {
                "type": "edit",
                "repo": ".",
                "issue": 43,
                "body_file": "edit.md",
                "expected": {"body": "Old"},
            },
        ],
    }
    assert plan.removed_actions == (2,)
    assert plan.delete_names == ()


def test_create_requires_explicit_cascade(tmp_path):
    view = inspected(tmp_path, "issue", issue_files())
    with pytest.raises(OutboxError, match="1"):
        plan_delete(view, ProposalId("issue", "001.json"), DeleteSelection(action=0))
    plan = plan_delete(
        view, ProposalId("issue", "001.json"), DeleteSelection(action=0, with_dependents=True)
    )
    assert plan.removed_actions == (0, 1)
    assert plan.removed_comments == ()
    assert json.loads(plan.new_text)["actions"] == [
        {"type": "comment", "repo": ".", "issue": 42, "body": "Independent"}
    ]
    assert plan.delete_names == ()


@pytest.mark.parametrize(
    "selection",
    [
        DeleteSelection(action=True),
        DeleteSelection(action=-1),
        DeleteSelection(action=1),
        DeleteSelection(action=0, comment=True),
        DeleteSelection(action=0, comment=-1),
        DeleteSelection(action=0, comment=2),
        DeleteSelection(comment=0),
        DeleteSelection(with_dependents=True),
        DeleteSelection(action=0, with_dependents=True),
        DeleteSelection(action=0, archive_journal=True),
        DeleteSelection(archive_journal=True),
    ],
)
def test_rejects_invalid_pr_selection(tmp_path, selection):
    with pytest.raises(OutboxError):
        plan_delete(inspected(tmp_path, "pr", pr_files()), ProposalId("pr", "001.json"), selection)


@pytest.mark.parametrize(
    "selection",
    [
        DeleteSelection(action=1, comment=0),
        DeleteSelection(action=1, with_dependents=True),
        DeleteSelection(action=0, archive_journal=True),
    ],
)
def test_rejects_inapplicable_issue_flags(tmp_path, selection):
    with pytest.raises(OutboxError):
        plan_delete(
            inspected(tmp_path, "issue", issue_files()), ProposalId("issue", "001.json"), selection
        )


def test_last_inline_comment_keeps_review(tmp_path):
    files = pr_files()
    raw = json.loads(files["001.json"])
    raw["actions"][0]["comments"] = raw["actions"][0]["comments"][:1]
    files["001.json"] = json.dumps(raw)
    plan = plan_delete(
        inspected(tmp_path, "pr", files),
        ProposalId("pr", "001.json"),
        DeleteSelection(action=0, comment=0),
    )
    raw["actions"][0]["comments"] = []
    assert json.loads(plan.new_text) == raw
    assert plan.delete_names == ()
    assert plan.removed_comments == ((0, 0),)


@pytest.mark.parametrize("last_action", [False, True])
def test_whole_deletes_only_referenced_exclusive_bodies(tmp_path, last_action):
    files = pr_files()
    raw = json.loads(files["001.json"])
    raw["actions"][0]["body_file"] = "body.md"
    del raw["actions"][0]["body"]
    files.update(
        {
            "001.json": json.dumps(raw),
            "body.md": "Body",
            "orphan.md": "Keep",
        }
    )
    plan = plan_delete(
        inspected(tmp_path, "pr", files),
        ProposalId("pr", "001.json"),
        DeleteSelection(action=0) if last_action else DeleteSelection(),
    )
    assert plan.new_text is None
    assert plan.delete_names == ("001.json", "body.md")
    assert plan.removed_actions == (0,)


@pytest.mark.parametrize("kind", ["issue", "pr"])
@pytest.mark.parametrize("body", ["002.json", "other.json.progress.json"])
@pytest.mark.parametrize("last_action", [False, True])
def test_body_cleanup_never_removes_a_manifest(tmp_path, kind, body, last_action):
    files = issue_files() if kind == "issue" else pr_files()
    raw = json.loads(files["001.json"])
    raw["actions"] = raw["actions"][:1]
    action = raw["actions"][0]
    action.pop("body", None)
    action["body_file"] = body
    sibling = issue_files()["001.json"] if kind == "issue" else pr_files()["001.json"]
    files.update({"001.json": json.dumps(raw), body: sibling, "002.json": sibling})
    plan = plan_delete(
        inspected(tmp_path, kind, files),
        ProposalId(kind, "001.json"),
        DeleteSelection(action=0) if last_action else DeleteSelection(),
    )
    assert plan.new_text is None
    assert plan.delete_names == ("001.json",)


@pytest.mark.parametrize("neighbor", ["shared", "invalid", "rejected", "unsafe-rejected"])
def test_whole_retains_bodies_when_shared_or_exclusivity_unknown(tmp_path, neighbor):
    files = issue_files()
    rejected = ()
    if neighbor == "shared":
        files["002.json"] = json.dumps(
            {
                "version": 1,
                "actions": [{"type": "comment", "repo": ".", "issue": 42, "body_file": "body.md"}],
            }
        )
    elif neighbor == "invalid":
        files["002.json"] = "{bad"
    else:
        rejected = ("002.json" if neighbor == "rejected" else "unsafe/name.json",)
    plan = plan_delete(
        inspected(tmp_path, "issue", files, rejected=rejected),
        ProposalId("issue", "001.json"),
        DeleteSelection(),
    )
    assert plan.new_text is None
    assert plan.delete_names == ("001.json",)


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_invalid_whole_manifest_only_deletes_named_manifest(tmp_path, kind):
    files = {"001.json": '{"actions":[{"body_file":"body.md"}]}', "body.md": "Keep"}
    view = inspected(tmp_path, kind, files)
    plan = plan_delete(view, ProposalId(kind, "001.json"), DeleteSelection())
    assert plan.delete_names == ("001.json",)
    assert plan.new_text is None
    assert plan.removed_actions == ()
    with pytest.raises(OutboxError):
        plan_delete(view, ProposalId(kind, "001.json"), DeleteSelection(action=0))


def test_rejected_selected_manifest_is_not_parsed_for_cleanup(tmp_path):
    view = inspected(tmp_path, "issue", issue_files(), rejected=("001.json",))
    assert plan_delete(view, ProposalId("issue", "001.json"), DeleteSelection()).delete_names == (
        "001.json",
    )


@pytest.mark.parametrize("raw", ["{bad", json.dumps({"version": 1, "actions": []})])
def test_invalid_manifest_cannot_be_repaired_by_partial_delete(tmp_path, raw):
    with pytest.raises(OutboxError):
        plan_delete(
            inspected(tmp_path, "issue", {"001.json": raw}),
            ProposalId("issue", "001.json"),
            DeleteSelection(action=0),
        )


@pytest.mark.parametrize("state", ["applied", "uncertain"])
def test_pr_journal_blocks_deletion_even_for_invalid_manifest(tmp_path, state):
    from jailbee.outbox.publication_db import Journal
    from jailbee.outbox_io import JournalAction

    files = {"001.json": "{bad"}
    journal = Journal(
        "uuid",
        "pr",
        "001.json",
        "d" * 64,
        IDENTITY.full_name,
        1,
        (JournalAction(0, state, "acme/repo"),),
    )
    view = inspected(tmp_path, "pr", files, pr_journals={"001.json": journal})
    with pytest.raises(OutboxError):
        plan_delete(view, ProposalId("pr", "001.json"), DeleteSelection())


@pytest.mark.parametrize("uncertain", [False, True])
def test_issue_progress_requires_settled_explicit_archive(tmp_path, uncertain):
    journals = MemoryJournals()
    files = issue_files()
    key = journal_key(IDENTITY, "001.json")
    journals.create(
        key, proposal_digest("001.json", files["001.json"], {"body.md": files["body.md"]}), 3
    )
    journals.mark_prepared(key, 0, repo="acme/repo")
    if not uncertain:
        journals.mark_applied(key, 0, repo="acme/repo", issue=43, url="https://example.test/43")
    view = inspected(tmp_path, "issue", files, journals=journals)
    with pytest.raises(OutboxError):
        plan_delete(view, ProposalId("issue", "001.json"), DeleteSelection())
    with pytest.raises(OutboxError):
        plan_delete(view, ProposalId("issue", "001.json"), DeleteSelection(action=2))
    if uncertain:
        with pytest.raises(OutboxError):
            plan_delete(
                view, ProposalId("issue", "001.json"), DeleteSelection(archive_journal=True)
            )
    else:
        plan = plan_delete(
            view, ProposalId("issue", "001.json"), DeleteSelection(archive_journal=True)
        )
        assert plan.delete_names == ("001.json", "body.md")
        assert journals.load(key) is not None


def test_null_body_file_is_not_a_cleanup_name(tmp_path):
    files = pr_files()
    raw = json.loads(files["001.json"])
    raw["actions"][0]["body_file"] = None
    files["001.json"] = json.dumps(raw)
    plan = plan_delete(
        inspected(tmp_path, "pr", files), ProposalId("pr", "001.json"), DeleteSelection()
    )
    assert plan.delete_names == ("001.json",)


def test_serialized_partial_edit_is_validated_again(tmp_path):
    files = pr_files()
    raw = json.loads(files["001.json"])
    raw["extension"] = [0] * 40000
    files["001.json"] = json.dumps(raw, separators=(",", ":"))
    view = inspected(tmp_path, "pr", files)
    assert view.proposals[0].state == "pending"
    with pytest.raises(OutboxError, match="larger"):
        plan_delete(view, ProposalId("pr", "001.json"), DeleteSelection(action=0, comment=0))


def test_missing_or_unavailable_selection_refused(tmp_path):
    view = inspected(tmp_path, "issue", issue_files())
    with pytest.raises(OutboxError):
        plan_delete(view, ProposalId("pr", "001.json"), DeleteSelection())
    with pytest.raises(OutboxError):
        plan_delete(
            replace(view, available=False), ProposalId("issue", "001.json"), DeleteSelection()
        )
