"""Whole-outbox clear planning stays conservative around publication evidence."""

import pytest

from jailbee.outbox.delete import DeleteSelection, plan_clear
from jailbee.outbox.inspect import build_views
from jailbee.outbox.models import ContainerView, ProposalId
from jailbee.outbox_io import JournalStore, journal_key, proposal_digest
from tests.outbox_support import IDENTITY, issue_files, pr_files, store


def inspected(tmp_path, kind, files, *, rejected=(), journals=None):
    snapshot = store(kind, files, rejected=rejected)
    views = build_views(
        IDENTITY, (snapshot,), journal_store=journals or JournalStore(tmp_path / "journals")
    )
    return ContainerView(IDENTITY, IDENTITY.full_name, True, None, (snapshot,), views)


def test_unpublished_pr_and_issue_are_deleted_whole(tmp_path):
    pr = inspected(tmp_path, "pr", pr_files())
    issue = inspected(tmp_path, "issue", issue_files())

    pr_plan = plan_clear(pr)
    issue_plan = plan_clear(issue)

    assert [item.proposal for item in pr_plan.delete] == [ProposalId("pr", "001.json")]
    assert [item.proposal for item in issue_plan.delete] == [ProposalId("issue", "001.json")]
    assert pr_plan.delete[0].selection == DeleteSelection()
    assert issue_plan.delete[0].selection == DeleteSelection()
    assert pr_plan.keep == issue_plan.keep == ()


@pytest.mark.parametrize(
    "evidence",
    [
        {"001.json.progress.json": '{"applied":[0],"urls":{"0":"u"}}'},
        {"applied.log": "now 001.json pr=42 actions=1 urls=x"},
    ],
)
def test_pr_with_sidecar_or_log_is_kept(tmp_path, evidence):
    files = pr_files() | evidence
    plan = plan_clear(inspected(tmp_path, "pr", files))

    assert len(plan.keep) == 1
    assert "partially published" in plan.keep[0].reason
    assert "jailbee outbox apply" in plan.keep[0].reason


def test_settled_issue_journal_is_archive_deleted(tmp_path):
    files = issue_files()
    journals = JournalStore(tmp_path / "journals")
    key = journal_key(IDENTITY, "001.json")
    journals.create(
        key,
        proposal_digest("001.json", files["001.json"], {"body.md": "Original body"}),
        3,
    )
    journals.mark_prepared(key, 0, repo="acme/repo")
    journals.mark_applied(key, 0, repo="acme/repo", issue=43, url="https://example.test/43")

    item = plan_clear(inspected(tmp_path, "issue", files, journals=journals)).delete[0]

    assert item.selection == DeleteSelection(archive_journal=True)


def test_uncertain_issue_is_kept_with_resolve_hint(tmp_path):
    files = issue_files()
    journals = JournalStore(tmp_path / "journals")
    key = journal_key(IDENTITY, "001.json")
    journals.create(
        key,
        proposal_digest("001.json", files["001.json"], {"body.md": "Original body"}),
        3,
    )
    journals.mark_prepared(key, 0, repo="acme/repo")

    item = plan_clear(inspected(tmp_path, "issue", files, journals=journals)).keep[0]

    assert "jailbee issue resolve" in item.reason


def test_invalid_manifest_without_evidence_is_deleted(tmp_path):
    plan = plan_clear(inspected(tmp_path, "pr", {"001.json": "{bad"}))

    assert [item.proposal for item in plan.delete] == [ProposalId("pr", "001.json")]


def test_rejected_entries_become_warnings(tmp_path):
    plan = plan_clear(inspected(tmp_path, "pr", pr_files(), rejected=("001.json",)))

    assert "rejected: 001.json" in plan.warnings
    assert plan.delete == plan.keep == ()


def test_unavailable_container_plans_nothing():
    plan = plan_clear(ContainerView(None, "acme-x", False, "container is stopped", (), ()))

    assert plan.available is False
    assert plan.error == "container is stopped"
    assert plan.delete == plan.keep == ()


def test_plan_lines_name_every_item_and_the_total(tmp_path):
    from jailbee.outbox.delete import clear_plan_lines

    pr_files_with_evidence = pr_files()
    pr_files_with_evidence["001.json.progress.json"] = '{"applied":[0],"urls":{"0":"u"}}'
    pr_files_with_evidence["002.json"] = pr_files_with_evidence["001.json"]
    pr = store("pr", pr_files_with_evidence)
    issue = store("issue", issue_files())
    views = build_views(IDENTITY, (pr, issue), journal_store=JournalStore(tmp_path / "journals"))
    container = ContainerView(IDENTITY, IDENTITY.full_name, True, None, (pr, issue), views)
    plan = plan_clear(container)
    lines = clear_plan_lines((plan,))

    rendered = "\n".join(lines)
    assert all(str(item.proposal) in rendered for item in (*plan.delete, *plan.keep))
    kept_line = next(line for line in lines if line.startswith("  keep "))
    assert plan.keep[0].reason in kept_line
    assert lines[-1] == "Delete 2 proposals in 1 container."
