"""Inspection must not mistake missing or malformed evidence for unpublished work."""

import json
from dataclasses import FrozenInstanceError

import pytest

from jailbee.outbox.inspect import build_views, detail_json, overview_json, safe_text
from jailbee.outbox.models import ContainerView, OutboxError, ProposalId
from jailbee.outbox_io import proposal_digest
from tests.outbox_journal_fake import MemoryJournals, journal_key
from tests.outbox_support import IDENTITY, issue_files, pr_files, store


def pr_journal(name, files, applied, count=None, *, uncertain=()):
    """A store PR publication journal of `name` with `applied` indices settled."""
    from jailbee.outbox.publication_db import Journal
    from jailbee.outbox_io import JournalAction

    raw = files[name]
    refs = {n for n in files if n != name and n in raw}
    actions = tuple(
        JournalAction(i, "applied", "acme/repo", url=f"https://x/{i}") for i in applied
    ) + tuple(JournalAction(i, "uncertain", "acme/repo") for i in uncertain)
    total = count if count is not None else len(json.loads(raw)["actions"])
    digest = proposal_digest(name, raw, {n: files[n] for n in refs})
    return Journal("uuid", "pr", name, digest, IDENTITY.full_name, total, actions)


def test_same_filename_is_two_proposals(tmp_path):
    views = build_views(
        IDENTITY,
        (store("pr", pr_files()), store("issue", issue_files())),
    )
    assert {str(v.id) for v in views} == {"pr/001.json", "issue/001.json"}
    review = next(v for v in views if v.id.kind == "pr")
    assert [(c.index, c.text) for c in review.actions[0].comments] == [(0, "First"), (1, "Second")]
    assert review.state == "pending"
    with pytest.raises(FrozenInstanceError):
        review.state = "applied"


@pytest.mark.parametrize(
    "value",
    [
        "001.json",
        "other/001.json",
        "pr/../x.json",
        "pr/a/b.json",
        "issue/",
        "pr/a.progress.json",
        "pr/a\\b.json",
    ],
)
def test_proposal_id_rejects_unsafe_names(value):
    with pytest.raises(OutboxError):
        ProposalId.parse(value)


def test_proposal_id_roundtrip():
    assert str(ProposalId.parse("issue/001.json")) == "issue/001.json"


def test_invalid_json_keeps_raw_and_a_journal_changes_the_revision(tmp_path):
    from jailbee.outbox.publication_db import Journal
    from jailbee.outbox_io import JournalAction

    first = build_views(IDENTITY, (store("pr", {"001.json": "{bad"}),))[0]
    journal = Journal(
        "uuid",
        "pr",
        "001.json",
        "d" * 64,
        IDENTITY.full_name,
        1,
        (JournalAction(0, "uncertain", "acme/repo"),),
    )
    second = build_views(
        IDENTITY, (store("pr", {"001.json": "{bad"}),), pr_journals={"001.json": journal}
    )[0]
    assert first.state == "invalid"
    assert first.raw_text == "{bad"
    assert first.error
    assert first.revision != second.revision
    assert second.edit_block


@pytest.mark.parametrize("old_count", [1, 3, 9])
def test_empty_old_journal_is_editable(tmp_path, old_count):
    journals = MemoryJournals()
    journals.create(journal_key(IDENTITY, "001.json"), "b" * 64, old_count)
    view = build_views(
        IDENTITY, (store("issue", issue_files()),), issue_journals=journals.snapshot()
    )[0]
    assert view.state == "pending"
    assert view.edit_block is None


def test_prepared_issue_is_uncertain_and_receipt_indices_preserved(tmp_path):
    journals = MemoryJournals()
    files = issue_files()
    key = journal_key(IDENTITY, "001.json")
    journals.create(
        key, proposal_digest("001.json", files["001.json"], {"body.md": files["body.md"]}), 3
    )
    journals.mark_prepared(key, 2, repo="acme/repo")
    view = build_views(IDENTITY, (store("issue", files),), issue_journals=journals.snapshot())[0]
    assert view.state == "uncertain"
    assert [a.state for a in view.actions] == ["pending", "pending", "uncertain"]
    assert view.edit_block


@pytest.mark.parametrize("change", ["digest", "count"])
def test_nonempty_mismatched_journal_blocks_receipts(tmp_path, change):
    journals = MemoryJournals()
    files = issue_files()
    key = journal_key(IDENTITY, "001.json")
    digest = proposal_digest("001.json", files["001.json"], {"body.md": files["body.md"]})
    journals.create(key, "b" * 64 if change == "digest" else digest, 4 if change == "count" else 3)
    journals.mark_prepared(key, 0, repo="acme/repo")
    view = build_views(IDENTITY, (store("issue", files),), issue_journals=journals.snapshot())[0]
    assert view.state == "uncertain"
    assert view.error and view.edit_block
    assert all(a.state == "pending" for a in view.actions)


def test_bad_proposal_input_does_not_abort_healthy_sibling(tmp_path):
    files = pr_files() | {"healthy.json": pr_files()["001.json"]}
    raw = json.loads(files["001.json"])
    raw["actions"][0]["body"] = "\ud800"
    files["001.json"] = json.dumps(raw)
    snapshot = store("pr", files)
    views = build_views(IDENTITY, (snapshot,))
    invalid, healthy = views
    assert invalid.error
    assert invalid.raw_text == files["001.json"]
    assert healthy.state == "pending" and healthy.error is None
    assert invalid.state == "invalid"


def test_missing_body_and_nested_body_revision(tmp_path):
    invalid = build_views(IDENTITY, (store("issue", {"001.json": issue_files()["001.json"]}),))[0]
    assert invalid.state == "invalid"
    payload = json.loads(pr_files()["001.json"])
    payload["actions"][0]["comments"][0].pop("body")
    payload["actions"][0]["comments"][0]["body_file"] = "nested.md"
    files = {"001.json": json.dumps(payload), "nested.md": "First"}
    first = build_views(IDENTITY, (store("pr", files),))[0]
    second = build_views(IDENTITY, (store("pr", files | {"nested.md": "Changed"}),))[0]
    assert first.actions[0].comments[0].text == "First"
    assert first.revision != second.revision


def test_safe_text_and_explicit_json_keep_literal_text(tmp_path):
    raw = "[red]x[/red]\x1b]52;c;secret\x07"
    assert "[red]x[/red]" in safe_text(raw)
    assert "\x1b" not in safe_text(raw) and "\x07" not in safe_text(raw)
    view = build_views(IDENTITY, (store("pr", {"001.json": raw}),))[0]
    container = ContainerView(IDENTITY, IDENTITY.full_name, True, None, (), (view,))
    detail = detail_json(container, view)
    assert detail["schema"] == 1
    assert detail["proposal"]["raw_text"] == raw
    assert detail["proposal"]["revision"] == view.revision
    overview = overview_json(
        (container, ContainerView(None, "offline", False, "unavailable", (), ()))
    )
    assert overview["schema"] == 1
    assert overview["containers"][1]["available"] is False
    assert overview["containers"][0]["counts"]["invalid"] == 1
    assert "\\u001b" in json.dumps(detail)


def test_issue_partial_receipt_and_revision(tmp_path):
    journals = MemoryJournals()
    files = issue_files()
    key = journal_key(IDENTITY, "001.json")
    digest = proposal_digest("001.json", files["001.json"], {"body.md": files["body.md"]})
    before = build_views(IDENTITY, (store("issue", files),), issue_journals=journals.snapshot())[0]
    journals.create(key, digest, 3)
    journals.mark_prepared(key, 0, repo="acme/repo")
    journals.mark_applied(
        key, 0, repo="acme/repo", url="https://github.com/acme/repo/issues/17", issue=17
    )
    after = build_views(IDENTITY, (store("issue", files),), issue_journals=journals.snapshot())[0]
    assert after.state == "partial"
    assert after.actions[0].receipt == "https://github.com/acme/repo/issues/17"
    assert after.actions[0].state == "applied"
    assert after.revision != before.revision
    assert after.revision != digest


@pytest.mark.parametrize("kind", ["pr", "issue"])
def test_rejected_unsafe_names_do_not_hide_valid_neighbors(tmp_path, kind):
    files = pr_files() if kind == "pr" else issue_files()
    rejected = ("nested/001.json", "../002.json", "bad\x1b.json", "bad\x00.json")
    snapshot = store(kind, files, rejected=rejected)
    views = build_views(IDENTITY, (snapshot,))
    assert [str(v.id) for v in views] == [f"{kind}/001.json"]
    assert views[0].state == "pending"
    container = ContainerView(IDENTITY, IDENTITY.full_name, True, None, (snapshot,), views)
    assert overview_json((container,))["containers"][0]["stores"][0]["rejected"] == list(rejected)


@pytest.mark.parametrize("kind", ["pr", "issue"])
@pytest.mark.parametrize("depth", [900, 1100])
def test_deep_json_is_invalid_without_hiding_neighbor_or_progress(tmp_path, kind, depth):
    journals = MemoryJournals()
    raw = "[" * depth + "]" * depth
    files = (pr_files() if kind == "pr" else issue_files()) | {"bad.json": raw}
    first = build_views(
        IDENTITY, (store(kind, files),), issue_journals=journals.snapshot(), pr_journals={}
    )
    bad = next(v for v in first if v.id.name == "bad.json")
    valid = next(v for v in first if v.id.name == "001.json")
    assert valid.state == "pending"
    assert bad.state == "invalid"
    assert bad.raw_text == raw
    assert bad.error and bad.revision
    pr_journals = {}
    if kind == "pr":
        pr_journals["bad.json"] = pr_journal("bad.json", files, (), 1, uncertain=(0,))
    else:
        key = journal_key(IDENTITY, "bad.json")
        journals.create(key, "b" * 64, 1)
        journals.mark_prepared(key, 0, repo="acme/repo")
    second = build_views(
        IDENTITY,
        (store(kind, files),),
        issue_journals=journals.snapshot(),
        pr_journals=pr_journals,
    )
    changed = next(v for v in second if v.id.name == "bad.json")
    assert changed.state == "invalid"
    assert changed.raw_text == raw
    assert changed.edit_block
    assert changed.revision != bad.revision


@pytest.mark.parametrize("manifest_pr", [None, 42])
def test_recorded_pr_is_display_context_not_revision_authority(tmp_path, manifest_pr):
    payload = json.loads(pr_files()["001.json"])
    payload["pr"] = manifest_pr
    payload["actions"] = [{"type": "description", "body": "Draft description"}]
    snapshots = (store("pr", {"001.json": json.dumps(payload)}),)
    canonical = build_views(IDENTITY, snapshots)[0]
    contextual = build_views(IDENTITY, snapshots, recorded_pr=73)[0]

    assert canonical.actions[0].target == ("" if manifest_pr is None else "42")
    assert contextual.actions[0].target == ("73" if manifest_pr is None else "42")
    assert canonical.state == ("awaiting-pr" if manifest_pr is None else "pending")
    assert contextual.state == "pending"
    assert contextual.revision == canonical.revision


def test_inspection_never_resolves_remote_targets(tmp_path, mocker):
    mocker.patch("jailbee.pr.resolve_pr", side_effect=AssertionError("network"))
    for helper in ("current_login", "list_labels", "get_issue"):
        mocker.patch(f"jailbee.issue_github.{helper}", side_effect=AssertionError("network"))
    payload = json.loads(pr_files()["001.json"])
    payload.pop("pr")
    payload["actions"] = [{"type": "description", "body": "Draft description"}]
    files = {"001.json": json.dumps(payload)}
    awaiting = build_views(IDENTITY, (store("pr", files),))[0]
    recorded = build_views(
        IDENTITY,
        (store("pr", files), store("issue", issue_files())),
        recorded_pr=42,
    )
    assert awaiting.state == "awaiting-pr"
    assert recorded[0].state == "pending"
    assert recorded[0].actions[0].target == "42"


def test_titles_stay_apart_from_markdown_bodies(tmp_path):
    pr_manifest = json.loads(pr_files()["001.json"])
    pr_manifest["actions"] = [{"type": "description", "title": "T", "body": "**B**"}]
    issue = json.loads(issue_files()["001.json"])
    issue["actions"].append(
        {"type": "labels", "repo": ".", "issue": 42, "add": ["bug"], "expected": {"labels": []}}
    )
    files = issue_files() | {"001.json": json.dumps(issue)}
    views = build_views(
        IDENTITY,
        (store("pr", {"001.json": json.dumps(pr_manifest)}), store("issue", files)),
    )
    pr_view = next(v for v in views if v.id.kind == "pr")
    issue_view = next(v for v in views if v.id.kind == "issue")
    description = pr_view.actions[0]
    assert (description.title, description.body, description.markdown) == ("T", "**B**", True)
    assert description.text == "T\n\n**B**"
    create, labels = issue_view.actions[0], issue_view.actions[-1]
    assert (create.title, create.body, create.markdown) == ("Example", "Original body", True)
    assert create.text == "Example\n\nOriginal body"
    assert labels.title is None and labels.markdown is False


def _container(tmp_path, *stores, pr_journals=None):
    views = build_views(IDENTITY, stores, pr_journals=pr_journals)
    return ContainerView(IDENTITY, "c1", True, None, tuple(stores), views)


def _pr_manifest(actions, pr=42):
    return json.dumps(
        {"version": 1, "repo": ".", "pr": pr, "head_sha": "a" * 40, "actions": actions}
    )


def _proposals(tmp_path, *stores, pr_journals=None):
    out = overview_json([_container(tmp_path, *stores, pr_journals=pr_journals)])
    return {p["id"]: p for p in out["containers"][0]["proposals"]}  # type: ignore[index]


def test_overview_json_splits_title_body_markdown(tmp_path):
    proposals = _proposals(tmp_path, store("issue", issue_files()))
    action = proposals["issue/001.json"]["actions"][0]
    assert action["title"] == "Example"
    assert action["body"] == "Original body"
    assert action["markdown"] is True
    assert action["text"] == "Example\n\nOriginal body"


def test_overview_json_comment_anchor_fields(tmp_path):
    files = {
        "001.json": _pr_manifest(
            [
                {
                    "type": "review",
                    "event": "COMMENT",
                    "body": "b",
                    "comments": [{"path": "src/a.py", "start_line": 3, "line": 5, "body": "x"}],
                }
            ]
        )
    }
    comment = _proposals(tmp_path, store("pr", files))["pr/001.json"]["actions"][0]["comments"][0]
    assert comment["path"] == "src/a.py"
    assert comment["line"] == 5
    assert comment["start_line"] == 3
    assert comment["label"] == "src/a.py:5"


def test_overview_json_reply_to(tmp_path):
    files = {
        "001.json": _pr_manifest(
            [
                {"type": "reply", "comment_id": 123, "body": "r"},
                {"type": "comment", "body": "c"},
            ]
        )
    }
    actions = _proposals(tmp_path, store("pr", files))["pr/001.json"]["actions"]
    assert actions[0]["reply_to"] == 123
    assert actions[1]["reply_to"] is None


def test_overview_json_draftable(tmp_path):
    comment_only = {
        "001.json": _pr_manifest(
            [{"type": "comment", "body": "c"}, {"type": "description", "title": "t", "body": "d"}]
        )
    }
    applied = {
        "001.json": _pr_manifest(
            [
                {"type": "review", "event": "COMMENT", "body": "b", "comments": []},
                {"type": "comment", "body": "c"},
            ]
        ),
    }
    pr = pr_files() | {"002.json": comment_only["001.json"], "003.json": applied["001.json"]}
    proposals = _proposals(
        tmp_path,
        store("pr", pr),
        store("issue", issue_files()),
        pr_journals={"003.json": pr_journal("003.json", pr, (0,))},
    )
    assert proposals["pr/001.json"]["draftable"] is True
    assert proposals["pr/002.json"]["draftable"] is False
    assert proposals["pr/003.json"]["draftable"] is False
    assert proposals["issue/001.json"]["draftable"] is False


def test_overview_json_delete_eligibility_matches_plan_clear(tmp_path):
    journals = MemoryJournals()
    from jailbee.outbox.delete import plan_clear

    pr = pr_files() | {
        "002.json": _pr_manifest([{"type": "comment", "body": "c"}]),
        "003.json": _pr_manifest(
            [{"type": "comment", "body": "a"}, {"type": "comment", "body": "b"}]
        ),
    }
    pr_journals = {"003.json": pr_journal("003.json", pr, (0,))}
    files = issue_files()
    digest = proposal_digest("001.json", files["001.json"], {"body.md": files["body.md"]})
    key = journal_key(IDENTITY, "001.json")
    journals.create(key, digest, 3)
    for i in range(3):
        journals.mark_prepared(key, i, repo="acme/repo")
        journals.mark_applied(key, i, repo="acme/repo", url=f"https://x/{i}", issue=i + 1)
    uncertain = issue_files()
    ukey = journal_key(IDENTITY, "002.json")
    uncertain_text = uncertain.pop("001.json")
    uncertain["002.json"] = uncertain_text
    journals.create(
        ukey, proposal_digest("002.json", uncertain_text, {"body.md": uncertain["body.md"]}), 3
    )
    journals.mark_prepared(ukey, 2, repo="acme/repo")
    # Separate snapshots: the settled journal belongs to 001.json, the uncertain one to 002.json.
    issue_store = store("issue", files | {"002.json": uncertain_text})
    views = build_views(
        IDENTITY,
        (store("pr", pr), issue_store),
        issue_journals=journals.snapshot(),
        pr_journals=pr_journals,
    )
    container = ContainerView(IDENTITY, "c1", True, None, (store("pr", pr), issue_store), views)
    plan = plan_clear(container)
    keep = {i.proposal: i for i in plan.keep}
    delete = {i.proposal: i for i in plan.delete}
    out = {p["id"]: p for p in overview_json([container])["containers"][0]["proposals"]}  # type: ignore[index]
    assert out["pr/001.json"]["deletable"] is True
    assert out["pr/001.json"]["delete_block"] is None
    assert out["pr/001.json"]["delete_archive"] is False
    blocked = out["pr/003.json"]
    assert blocked["deletable"] is False
    assert blocked["delete_block"] == keep[ProposalId("pr", "003.json")].reason
    settled = out["issue/001.json"]
    assert settled["deletable"] is True
    assert settled["delete_archive"] is True
    assert delete[ProposalId("issue", "001.json")].selection.archive_journal
    stuck = out["issue/002.json"]
    assert stuck["deletable"] is False
    assert "jailbee issue resolve" in stuck["delete_block"]


def test_overview_json_action_delete_scope(tmp_path):
    journals = MemoryJournals()
    actions = _proposals(tmp_path, store("issue", issue_files()))["issue/001.json"]["actions"]
    assert [a["delete_scope"] for a in actions] == [[0, 1], [1], [2]]
    assert all(a["delete_block"] is None for a in actions)
    files = issue_files()
    key = journal_key(IDENTITY, "001.json")
    journals.create(
        key, proposal_digest("001.json", files["001.json"], {"body.md": files["body.md"]}), 3
    )
    journals.mark_prepared(key, 0, repo="acme/repo")
    journals.mark_applied(key, 0, repo="acme/repo", url="https://x/1", issue=1)
    st = store("issue", files)
    views = build_views(IDENTITY, (st,), issue_journals=journals.snapshot())
    container = ContainerView(IDENTITY, "c1", True, None, (st,), views)
    proposal = overview_json([container])["containers"][0]["proposals"][0]  # type: ignore[index]
    assert views[0].edit_block
    for action in proposal["actions"]:
        assert action["delete_scope"] is None
        assert action["delete_block"] == views[0].edit_block


@pytest.mark.parametrize(
    "number,actions",
    [
        (None, [{"type": "description", "body": "text"}]),
        (7, []),
        (7, [{"type": "description", "body": "text"}]),
    ],
)
def test_source_awaits_push_without_inheriting_recorded_pr(tmp_path, number, actions):
    payload = json.loads(pr_files()["001.json"])
    payload.update(pr=number, source="refs/heads/topic", source_sha="a" * 40, actions=actions)
    view = build_views(
        IDENTITY,
        (store("pr", {"001.json": json.dumps(payload)}),),
        recorded_pr=73,
    )[0]
    assert view.state == "awaiting-push"
    assert all(a.target == (str(number) if number else "") for a in view.actions)


def test_assignees_action_view_lists_add_and_remove_as_plain_text(tmp_path):
    issue = json.loads(issue_files()["001.json"])
    issue["actions"] = [
        {
            "type": "assignees",
            "repo": ".",
            "issue": 42,
            "add": ["@me", "bob"],
            "remove": ["carol"],
            "expected": {"assignees": ["carol"]},
        }
    ]
    views = build_views(
        IDENTITY,
        (store("issue", {"001.json": json.dumps(issue)}),),
    )
    (action,) = views[0].actions
    assert (action.kind, action.target, action.title, action.markdown) == (
        "assignees",
        "42",
        None,
        False,
    )
    assert action.body == "add: @me, bob\nremove: carol"
