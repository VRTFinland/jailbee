import json
from types import SimpleNamespace

from jailbee.dashboard import outbox
from jailbee.outbox.commands import show_selected
from jailbee.outbox.models import AttachmentUploadView, AttachmentUse, AttachmentView, ProposalId


def test_show_displays_attachment_metadata_and_placement(mocker, capsys):
    attachment = AttachmentView(
        "img", "chart.png", "image/png", 123, "abc123", (AttachmentUse(1, 3),)
    )
    action = SimpleNamespace(
        index=1,
        kind="comment",
        repo="o/r",
        target="42",
        state="pending",
        title=None,
        body="Original body",
        markdown=False,
        receipt=None,
        comments=(),
    )
    view = SimpleNamespace(
        state="pending",
        revision="r1",
        error=None,
        edit_block=None,
        actions=(action,),
        attachments=(attachment,),
        uploads=(
            AttachmentUploadView(
                "digest",
                "blob1",
                "github.com",
                42,
                "chart.png",
                "image/png",
                123,
                "abc123",
                "uncertain",
                None,
            ),
        ),
        raw_text="raw",
    )
    mocker.patch(
        "jailbee.outbox.commands._selected", return_value=(None, SimpleNamespace(name="box"), view)
    )
    assert (
        show_selected(None, None, "box", ProposalId("issue", "a.json"), output="text", color=False)
        == 0
    )
    output = capsys.readouterr().out
    for text in (
        "Original body",
        "img",
        "chart.png",
        "image/png",
        "123",
        "abc123",
        "Action 1",
        "line 3",
        "uncertain",
        "blob1",
        "github.com",
    ):
        assert text in output


def test_listing_and_preview_include_relevant_attachment_metadata():
    attachment = {
        "id": "img",
        "name": "chart.png",
        "content_type": "image/png",
        "size": 123,
        "sha256": "abc123",
        "uses": [{"action_index": 1, "line": 3}],
    }
    actions = [
        {"index": i, "kind": "comment", "body": "Original body", "markdown": False} for i in (0, 1)
    ]
    proposal = {
        "id": "issue/a.json",
        "state": "pending",
        "revision": "r1",
        "actions": actions,
        "attachments": [attachment],
    }
    listing = {"containers": [{"name": "box", "proposals": [proposal]}]}
    containers = outbox.parse_outbox_listing(json.dumps(listing))
    assert len(containers[0].proposals[0].attachments) == 1
    state = outbox.OutboxState("Outbox", containers)
    for action in (None, 1):
        text = "\n".join(
            line.plain
            for line in outbox.preview_lines(state, ("box", "issue/a.json", action, None), 80)
        )
        for expected in (
            "Original body",
            "img",
            "chart.png",
            "image/png",
            "123",
            "abc123",
            "action 1",
            "line 3",
        ):
            assert expected in text
    other = "\n".join(
        line.plain for line in outbox.preview_lines(state, ("box", "issue/a.json", 0, None), 80)
    )
    assert "chart.png" not in other
