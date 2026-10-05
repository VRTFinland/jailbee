"""Inspection must not mistake missing or malformed evidence for unpublished work."""

import json
from dataclasses import FrozenInstanceError

import pytest

from jailbee.outbox.inspect import build_views, detail_json, overview_json, safe_text
from jailbee.outbox.models import ContainerView, OutboxError, ProposalId
from jailbee.outbox_io import JournalStore, journal_key, proposal_digest
from tests.outbox_support import IDENTITY, issue_files, pr_files, store


def test_same_filename_is_two_proposals(tmp_path):
    views = build_views(
        IDENTITY,
        (store("pr", pr_files()), store("issue", issue_files())),
        journal_store=JournalStore(tmp_path / "journals"),
    )
    assert {str(v.id) for v in views} == {"pr/001.json", "issue/001.json"}
    review = next(v for v in views if v.id.kind == "pr")
    assert [(c.index, c.text) for c in review.actions[0].comments] == [(0, "First"), (1, "Second")]
    assert review.state == "pending"
    assert not (tmp_path / "journals").exists()
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


def test_invalid_json_keeps_raw_and_progress_revision(tmp_path):
    journals = JournalStore(tmp_path / "journals")
    first = build_views(IDENTITY, (store("pr", {"001.json": "{bad"}),), journal_store=journals)[0]
    second = build_views(
        IDENTITY,
        (store("pr", {"001.json": "{bad"}, rejected=("001.json.progress.json",)),),
        journal_store=journals,
    )[0]
    assert first.state == "invalid"
    assert first.raw_text == "{bad"
    assert first.error
    assert first.revision != second.revision
    assert second.edit_block


@pytest.mark.parametrize("old_count", [1, 3, 9])
def test_empty_old_journal_is_editable(tmp_path, old_count):
    journals = JournalStore(tmp_path / "journals")
    journals.create(journal_key(IDENTITY, "001.json"), "b" * 64, old_count)
    view = build_views(IDENTITY, (store("issue", issue_files()),), journal_store=journals)[0]
    assert view.state == "pending"
    assert view.edit_block is None


def test_prepared_issue_is_uncertain_and_receipt_indices_preserved(tmp_path):
    files = issue_files()
    journals = JournalStore(tmp_path / "journals")
    key = journal_key(IDENTITY, "001.json")
    journals.create(
        key, proposal_digest("001.json", files["001.json"], {"body.md": files["body.md"]}), 3
    )
    journals.mark_prepared(key, 2, repo="acme/repo")
    view = build_views(IDENTITY, (store("issue", files),), journal_store=journals)[0]
    assert view.state == "uncertain"
    assert [a.state for a in view.actions] == ["pending", "pending", "uncertain"]
    assert view.edit_block


@pytest.mark.parametrize("change", ["digest", "count"])
def test_nonempty_mismatched_journal_blocks_receipts(tmp_path, change):
    files = issue_files()
    journals = JournalStore(tmp_path / "journals")
    key = journal_key(IDENTITY, "001.json")
    digest = proposal_digest("001.json", files["001.json"], {"body.md": files["body.md"]})
    journals.create(key, "b" * 64 if change == "digest" else digest, 4 if change == "count" else 3)
    journals.mark_prepared(key, 0, repo="acme/repo")
    view = build_views(IDENTITY, (store("issue", files),), journal_store=journals)[0]
    assert view.state == "uncertain"
    assert view.error and view.edit_block
    assert all(a.state == "pending" for a in view.actions)


@pytest.mark.parametrize(
    "sidecar",
    [
        "{bad",
        '{"applied":[true],"urls":{}}',
        '{"applied":[99],"urls":{}}',
        '{"applied":[0,0],"urls":{}}',
    ],
)
def test_bad_pr_sidecar_is_unknown_not_empty(tmp_path, sidecar):
    files = pr_files() | {"001.json.progress.json": sidecar}
    view = build_views(IDENTITY, (store("pr", files),), journal_store=JournalStore(tmp_path))[0]
    assert view.state == "uncertain"
    assert view.error and view.edit_block


@pytest.mark.parametrize("name", ["one space.json", " leading .json", "one pr=7 space.json"])
@pytest.mark.parametrize(
    "suffix",
    [
        "pr=42 actions=1 urls=https://receipt",
        "broken",
        "pr=42 actions=broken urls=https://x pr=7 actions=1 urls=https://y",
        "pr=42 actions=1 urls=https://x pr=7 actions=1 urls=https://y",
        "pr=42 actions=broken  urls=https://x pr=7 actions=1 urls=https://y",
        "pr=42\tactions=broken\turls=https://x pr=7 actions=1 urls=https://y",
        " pr=42  actions=broken   urls=https://x pr=7 actions=1 urls=https://y",
        "pr = 42 actions = broken urls = https://x pr=7 actions=1 urls=https://y",
        "pr=42 urls=https://x actions=broken pr=7 actions=1 urls=https://y",
        "actions=broken urls=https://x pr=7 actions=1 urls=https://y",
        "pr=42 urls=https://x pr=7 actions=1 urls=https://y",
        "pr=42 actions=broken url=https://x pr=7 actions=1 urls=https://y",
        "pr=42 urls=notes.json pr=7 actions=1 urls=https://y",
        "actions=broken urls=notes.json pr=7 actions=1 urls=https://y",
        "pr=42 actions=broken url=notes.json pr=7 actions=1 urls=https://y",
        "unknown=notes.json pr=7 actions=1 urls=receipt.json",
    ],
)
def test_whitespace_receipt_blocks_exact_proposal(tmp_path, name, suffix):
    from jailbee.outbox.delete import DeleteSelection, plan_delete

    files = {name: pr_files()["001.json"], "applied.log": f"now {name} {suffix}\n"}
    snapshot = store("pr", files)
    views = build_views(IDENTITY, (snapshot,), journal_store=JournalStore(tmp_path))
    view = views[0]
    assert view.error and view.edit_block
    container = ContainerView(IDENTITY, IDENTITY.full_name, True, None, (snapshot,), views)
    with pytest.raises(OutboxError):
        plan_delete(container, ProposalId("pr", name), DeleteSelection())
    with pytest.raises(OutboxError):
        plan_delete(container, ProposalId("pr", name), DeleteSelection(action=0))
    assert view.raw_text == files[name]
    from jailbee.outbox.inspect import pr_progress_evidence

    evidence = pr_progress_evidence(snapshot, name, 1)
    assert ("applied.log", files["applied.log"].rstrip("\n")) in evidence.inputs
    changed = files | {
        "applied.log": files["applied.log"]
        .replace("now ", "later ", 1)
        .replace("https://y", "https://changed")
    }
    after = build_views(IDENTITY, (store("pr", changed),), journal_store=JournalStore(tmp_path))[0]
    assert after.revision != view.revision


@pytest.mark.parametrize(
    "other",
    [
        "one.json longer.json",
        "one.json pr=7 longer.json",
        "one.json pr=7 actions=notes.json",
        "one.json pr=7  longer.json",
        "one.json actions=notes.json",
        "one.json pr = 7 longer.json",
        " one.json pr=7 longer.json",
        "one.json-more pr=7.json",
        "one pr=7 longer.json",
        "one.jsonx longer.json",
    ],
)
def test_complete_name_prefix_receipt_is_conservatively_ambiguous(tmp_path, other):
    text = pr_files()["001.json"]
    files = {"one.json": text, other: text, "applied.log": f"now {other} pr=42 actions=1 urls=x"}
    views = build_views(IDENTITY, (store("pr", files),), journal_store=JournalStore(tmp_path))
    selected = next(v for v in views if v.id.name == "one.json")
    logged = next(v for v in views if v.id.name == other)
    if other.startswith("one.json "):
        assert selected.state == "uncertain"
        assert selected.edit_block and selected.error
    else:
        assert selected.state == "pending"
        assert selected.edit_block is None and selected.error is None
    assert logged.edit_block and logged.error


def test_receipt_shaped_filename_ambiguity_never_authorizes_replay(tmp_path):
    from jailbee.outbox.inspect import pr_progress_evidence

    short = "one.json"
    long = "one.json pr=42 actions=broken urls=notes.json"
    files = {
        short: pr_files()["001.json"],
        long: pr_files()["001.json"],
        "applied.log": f"now {long} pr=7 actions=1 urls=https://y",
    }
    snapshot = store("pr", files)
    for name in (short, long):
        evidence = pr_progress_evidence(snapshot, name, 1)
        assert evidence.edit_block and evidence.error
        assert ("applied.log", files["applied.log"]) in evidence.inputs


def test_suffix_shaped_url_preserves_valid_sidecar_and_revision(tmp_path):
    from jailbee.outbox.inspect import pr_progress_evidence

    files = pr_files() | {
        "001.json.progress.json": '{"applied":[0],"urls":{"0":"https://receipt"}}',
        "applied.log": "now 001.json pr=42 actions=1 urls=https://x pr=7 actions=1 urls=https://y",
    }
    evidence = pr_progress_evidence(store("pr", files), "001.json", 1)
    assert evidence.error is None
    assert evidence.applied == frozenset({0})
    assert evidence.receipts == ((0, "https://receipt"),)
    journals = JournalStore(tmp_path)
    first = build_views(IDENTITY, (store("pr", files),), journal_store=journals)[0]
    files["applied.log"] += "changed"
    second = build_views(IDENTITY, (store("pr", files),), journal_store=journals)[0]
    assert first.state == "applied"
    assert first.revision != second.revision


@pytest.mark.parametrize("bad", ["deep-progress", "surrogate-body"])
def test_bad_proposal_input_does_not_abort_healthy_sibling(tmp_path, bad):
    files = pr_files() | {"healthy.json": pr_files()["001.json"]}
    if bad == "deep-progress":
        files["001.json.progress.json"] = "[" * 20000 + "0" + "]" * 20000
    else:
        raw = json.loads(files["001.json"])
        raw["actions"][0]["body"] = "\ud800"
        files["001.json"] = json.dumps(raw)
    snapshot = store("pr", files)
    views = build_views(IDENTITY, (snapshot,), journal_store=JournalStore(tmp_path))
    invalid, healthy = views
    assert invalid.error
    assert invalid.raw_text == files["001.json"]
    assert healthy.state == "pending" and healthy.error is None
    if bad == "deep-progress":
        assert invalid.state == "uncertain" and invalid.edit_block
    else:
        assert invalid.state == "invalid"


def test_pr_progress_and_exact_log_name(tmp_path):
    journals = JournalStore(tmp_path)
    unrelated = pr_files() | {
        "applied.log": "2026-09-30T00:00:00Z x001.json pr=42 actions=1 urls=x\n"
    }
    view = build_views(IDENTITY, (store("pr", unrelated),), journal_store=journals)[0]
    assert view.edit_block is None
    files = pr_files() | {
        "001.json.progress.json": json.dumps(
            {"applied": [0], "urls": {"0": "https://example/receipt"}}
        )
    }
    applied = build_views(IDENTITY, (store("pr", files),), journal_store=journals)[0]
    assert applied.state == "applied"
    assert applied.actions[0].receipt == "https://example/receipt"
    logged = build_views(
        IDENTITY,
        (
            store(
                "pr",
                unrelated
                | {"applied.log": unrelated["applied.log"].replace("x001.json", "001.json")},
            ),
        ),
        journal_store=journals,
    )[0]
    assert logged.edit_block
    assert logged.state == "uncertain"


@pytest.mark.parametrize("rejected", [("applied.log",), ("001.json.progress.json",)])
def test_rejected_progress_is_unknown(tmp_path, rejected):
    view = build_views(
        IDENTITY,
        (store("pr", pr_files(), rejected=rejected),),
        journal_store=JournalStore(tmp_path),
    )[0]
    assert view.state == "uncertain"
    assert view.edit_block


def test_missing_body_and_nested_body_revision(tmp_path):
    journals = JournalStore(tmp_path)
    invalid = build_views(
        IDENTITY, (store("issue", {"001.json": issue_files()["001.json"]}),), journal_store=journals
    )[0]
    assert invalid.state == "invalid"
    payload = json.loads(pr_files()["001.json"])
    payload["actions"][0]["comments"][0].pop("body")
    payload["actions"][0]["comments"][0]["body_file"] = "nested.md"
    files = {"001.json": json.dumps(payload), "nested.md": "First"}
    first = build_views(IDENTITY, (store("pr", files),), journal_store=journals)[0]
    second = build_views(
        IDENTITY, (store("pr", files | {"nested.md": "Changed"}),), journal_store=journals
    )[0]
    assert first.actions[0].comments[0].text == "First"
    assert first.revision != second.revision


def test_safe_text_and_explicit_json_keep_literal_text(tmp_path):
    raw = "[red]x[/red]\x1b]52;c;secret\x07"
    assert "[red]x[/red]" in safe_text(raw)
    assert "\x1b" not in safe_text(raw) and "\x07" not in safe_text(raw)
    view = build_views(
        IDENTITY, (store("pr", {"001.json": raw}),), journal_store=JournalStore(tmp_path)
    )[0]
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
    files = issue_files()
    journals = JournalStore(tmp_path / "journals")
    key = journal_key(IDENTITY, "001.json")
    digest = proposal_digest("001.json", files["001.json"], {"body.md": files["body.md"]})
    before = build_views(IDENTITY, (store("issue", files),), journal_store=journals)[0]
    journals.create(key, digest, 3)
    journals.mark_prepared(key, 0, repo="acme/repo")
    journals.mark_applied(
        key, 0, repo="acme/repo", url="https://github.com/acme/repo/issues/17", issue=17
    )
    after = build_views(IDENTITY, (store("issue", files),), journal_store=journals)[0]
    assert after.state == "partial"
    assert after.actions[0].receipt == "https://github.com/acme/repo/issues/17"
    assert after.actions[0].state == "applied"
    assert after.revision != before.revision
    assert after.revision != digest


@pytest.mark.parametrize("corruption", ["json", "index", "duplicate"])
def test_corrupt_issue_journal_blocks_inspection(tmp_path, corruption):
    files = issue_files()
    journals = JournalStore(tmp_path / "journals")
    key = journal_key(IDENTITY, "001.json")
    journals.create(
        key, proposal_digest("001.json", files["001.json"], {"body.md": files["body.md"]}), 3
    )
    journals.mark_prepared(key, 0, repo="acme/repo")
    path = journals._path(key)
    data = json.loads(path.read_text())
    if corruption == "index":
        data["actions"][0]["index"] = 3
    elif corruption == "duplicate":
        data["actions"].append(data["actions"][0])
    path.write_text("{bad" if corruption == "json" else json.dumps(data))
    view = build_views(IDENTITY, (store("issue", files),), journal_store=journals)[0]
    assert view.state == "uncertain"
    assert view.error and view.edit_block
    assert all(a.state == "pending" for a in view.actions)


@pytest.mark.parametrize("kind", ["pr", "issue"])
def test_rejected_unsafe_names_do_not_hide_valid_neighbors(tmp_path, kind):
    files = pr_files() if kind == "pr" else issue_files()
    rejected = ("nested/001.json", "../002.json", "bad\x1b.json", "bad\x00.json")
    snapshot = store(kind, files, rejected=rejected)
    views = build_views(IDENTITY, (snapshot,), journal_store=JournalStore(tmp_path))
    assert [str(v.id) for v in views] == [f"{kind}/001.json"]
    assert views[0].state == "pending"
    container = ContainerView(IDENTITY, IDENTITY.full_name, True, None, (snapshot,), views)
    assert overview_json((container,))["containers"][0]["stores"][0]["rejected"] == list(rejected)


@pytest.mark.parametrize("kind", ["pr", "issue"])
@pytest.mark.parametrize("depth", [900, 1100])
def test_deep_json_is_invalid_without_hiding_neighbor_or_progress(tmp_path, kind, depth):
    raw = "[" * depth + "]" * depth
    files = (pr_files() if kind == "pr" else issue_files()) | {"bad.json": raw}
    journals = JournalStore(tmp_path / "journals")
    first = build_views(IDENTITY, (store(kind, files),), journal_store=journals)
    bad = next(v for v in first if v.id.name == "bad.json")
    valid = next(v for v in first if v.id.name == "001.json")
    assert valid.state == "pending"
    assert bad.state == "invalid"
    assert bad.raw_text == raw
    assert bad.error and bad.revision
    if kind == "pr":
        files["bad.json.progress.json"] = "{bad"
    else:
        key = journal_key(IDENTITY, "bad.json")
        journals.create(key, "b" * 64, 1)
        journals.mark_prepared(key, 0, repo="acme/repo")
    second = build_views(IDENTITY, (store(kind, files),), journal_store=journals)
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
    journals = JournalStore(tmp_path / "journals")
    canonical = build_views(IDENTITY, snapshots, journal_store=journals)[0]
    contextual = build_views(IDENTITY, snapshots, journal_store=journals, recorded_pr=73)[0]

    assert canonical.actions[0].target == ("" if manifest_pr is None else "42")
    assert contextual.actions[0].target == ("73" if manifest_pr is None else "42")
    assert canonical.state == ("awaiting-pr" if manifest_pr is None else "pending")
    assert contextual.state == "pending"
    assert contextual.revision == canonical.revision
    assert not (tmp_path / "journals").exists()


def test_inspection_never_resolves_remote_targets(tmp_path, mocker):
    mocker.patch("jailbee.pr.resolve_pr", side_effect=AssertionError("network"))
    for helper in ("current_login", "list_labels", "get_issue"):
        mocker.patch(f"jailbee.issue_github.{helper}", side_effect=AssertionError("network"))
    payload = json.loads(pr_files()["001.json"])
    payload.pop("pr")
    payload["actions"] = [{"type": "description", "body": "Draft description"}]
    files = {"001.json": json.dumps(payload)}
    journals = JournalStore(tmp_path)
    awaiting = build_views(IDENTITY, (store("pr", files),), journal_store=journals)[0]
    recorded = build_views(
        IDENTITY,
        (store("pr", files), store("issue", issue_files())),
        journal_store=journals,
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
        journal_store=JournalStore(tmp_path / "journals"),
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
