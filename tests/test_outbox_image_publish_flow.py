"""Image proposals go through capture, inspection, approval and publication."""

import json

import pytest

from jailbee import attachment_github, issue_github
from jailbee.attachment_github import UploadTarget
from jailbee.outbox import service, store_db
from jailbee.outbox.publish import PublishOptions, publish_selected
from tests.test_outbox_attachment_publication import URL
from tests.test_outbox_attachment_staging import capture
from tests.test_outbox_broker_ops import PLAIN, comment, manifest, put, stored
from tests.test_outbox_broker_ops import env as env


@pytest.fixture
def flow(env, monkeypatch, mocker):
    import jailbee.db

    monkeypatch.setattr(jailbee.db, "state_dir", lambda: env.rig.root.parent)
    monkeypatch.setattr("jailbee.outbox.publication_db.default_root", lambda: env.rig.root)
    (env.rig.root.parent / "orchestration").symlink_to(env.rig.root, target_is_directory=True)
    mocker.patch("subprocess.run", side_effect=AssertionError("unexpected subprocess"))
    mocker.patch("jailbee.git.get_remote_url", return_value="https://github.com/acme/repo.git")
    mocker.patch("jailbee.submodules.declared_submodule_remotes", return_value=())
    mocker.patch("jailbee.submodules.host_submodule_paths", return_value=[])
    mocker.patch.object(issue_github, "current_login", return_value="alice")
    mocker.patch.object(issue_github, "list_labels", return_value={})
    mocker.patch.object(
        issue_github,
        "get_issue",
        return_value=issue_github.IssueSnapshot(
            7, "Old", "Old body", (), "open", "https://github.com/acme/repo/issues/7", False
        ),
    )
    resolve = mocker.patch.object(
        attachment_github, "resolve_target", return_value=UploadTarget("github.com", "acme/repo", 1)
    )
    upload = mocker.patch.object(attachment_github, "upload_to_target", return_value=URL)
    send = mocker.patch.object(
        issue_github,
        "add_comment",
        return_value=issue_github.MutationReceipt(
            issue=7, url="https://github.com/acme/repo/issues/7#issuecomment-1"
        ),
    )
    descriptor = capture(env)
    response = put(
        env,
        manifest={
            **manifest(comment("Before ![Evidence](attachment:screen) after")),
            "attachments": {"screen": descriptor},
        },
    )
    assert response.ok, response
    row = next(row for row in env.rig.incus.list_containers.return_value if row["name"] == PLAIN)
    row["created_at"] = "2026-01-01T00:00:00Z"
    return env, resolve, upload, send


def inspect(flow):
    env = flow[0]
    view = service.load_container(env.rig.cfg, env.rig.incus, PLAIN, raise_errors=True)
    (proposal,) = view.proposals
    return proposal


def publish(flow, revision, confirm=True):
    env = flow[0]
    proposal = inspect(flow)
    return publish_selected(
        env.rig.cfg,
        env.rig.incus,
        PLAIN,
        proposal.id,
        options=PublishOptions(publisher="user"),
        confirm=lambda count: confirm,
        expected_revision=revision,
    )


def test_declining_approval_does_not_resolve_upload_or_publish(flow):
    view = inspect(flow)
    assert publish(flow, view.revision, confirm=False) == 0
    for call in flow[1:]:
        call.assert_not_called()


def test_approved_comment_uses_canonical_uploaded_url(flow):
    view = inspect(flow)
    assert publish(flow, view.revision) == 0
    flow[2].assert_called_once()
    assert flow[3].call_args.kwargs["body"] == f"Before ![Evidence]({URL}) after"


def test_retry_after_definite_comment_failure_reuses_uploaded_image(flow):
    flow[3].side_effect = issue_github.IssueGithubMutationError("rejected", uncertain=False)
    assert publish(flow, inspect(flow).revision) == 1
    flow[3].side_effect = None
    assert publish(flow, inspect(flow).revision) == 0
    flow[2].assert_called_once()
    assert flow[3].call_count == 2
    assert flow[3].call_args.kwargs["body"] == f"Before ![Evidence]({URL}) after"


def test_changed_attachment_metadata_rejects_old_revision_before_upload(flow):
    env = flow[0]
    original = inspect(flow)
    (row,) = stored(env)
    data = json.loads(row.manifest)
    data["attachments"]["screen"] = capture(env)
    store_db.host_store(env.rig.root).edit_proposal(
        PLAIN, "issue", row.name, row.revision, json.dumps(data)
    )
    assert publish(flow, original.revision) == 1
    for call in flow[1:]:
        call.assert_not_called()
