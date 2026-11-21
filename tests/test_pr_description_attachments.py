import pytest

from jailbee import attachment_github, pr_flow, pr_outbox
from jailbee.attachment_github import UploadTarget
from jailbee.outbox.models import OutboxChanged
from jailbee.outbox.pr_description import create_with_description
from jailbee.outbox.publication_db import open_publications
from jailbee.pr import PrCreated
from jailbee.pr_ai import PrText
from jailbee.pr_outbox import OutboxPrText
from tests.test_branch_pr import source_publisher as source_publisher
from tests.test_branch_pr_attachments import URL, prepare


def selected(p):
    outbox = pr_outbox.read_outbox(p["incus"], "full", uid=p["cfg"].container_user.uid, strict=True)
    raw = outbox.files["m.json"]
    manifest = pr_outbox.parse_manifest("m.json", raw, outbox.files)
    text = PrText(title="Title", body=manifest.actions[0].body, branch="feature/a")
    return OutboxPrText(
        text,
        "m.json",
        0,
        outbox.identity,
        pr_outbox.description_source_digest(outbox, "m.json", 0, text),
        raw,
        (),
    )


def setup_upload(monkeypatch):
    calls = []
    monkeypatch.setattr(
        attachment_github, "resolve_target", lambda root, repo: UploadTarget("github.com", repo, 1)
    )
    monkeypatch.setattr(
        attachment_github, "upload_to_target", lambda *args: calls.append(args) or URL
    )
    return calls


def run(p, source, create, confirm=lambda _: True):
    return create_with_description(
        p["cfg"], p["incus"], "full", source, source.text.body, create, confirm, publisher="user"
    )


def test_create_uploads_once_and_records_description(source_publisher, monkeypatch, capsys):
    p = source_publisher
    descriptor = prepare(p)
    source = selected(p)
    calls = setup_upload(monkeypatch)

    def confirm(question):
        output = capsys.readouterr().out
        assert "Title" in output and source.text.body in output
        assert descriptor["sha256"] in output and "characters" in output
        assert not calls
        return True

    def create(body):
        assert body == f"![one]({URL}) ![two]({URL})"
        assert open_publications().load("uuid-full", "pr", "m.json").actions[0].state == "uncertain"
        return PrCreated(42, "https://github.com/acme/widgets/pull/42", False)

    result = run(p, source, create, confirm)
    assert result.number == 42
    assert len(calls) == 1
    assert "m.json" not in p["files"]


def test_override_never_uploads(source_publisher, monkeypatch):
    p = source_publisher
    prepare(p)
    source = selected(p)
    monkeypatch.setattr(
        attachment_github, "resolve_target", lambda *args: pytest.fail("upload attempted")
    )
    with pytest.raises(OutboxChanged, match="overridden"):
        create_with_description(
            p["cfg"],
            p["incus"],
            "full",
            source,
            "override",
            lambda _: pytest.fail("create"),
            lambda _: True,
            publisher="user",
        )


def test_unknown_creation_outcome_blocks_retry(source_publisher, monkeypatch):
    p = source_publisher
    prepare(p)
    source = selected(p)
    calls = setup_upload(monkeypatch)

    def fail(body):
        raise RuntimeError("connection lost")

    with pytest.raises(RuntimeError, match="connection lost"):
        run(p, source, fail)
    with pytest.raises(OutboxChanged):
        run(p, source, lambda _: pytest.fail("duplicate create"))
    assert len(calls) == 1


def test_existing_pr_does_not_consume_and_reuses_upload(source_publisher, monkeypatch):
    p = source_publisher
    prepare(p)
    source = selected(p)
    calls = setup_upload(monkeypatch)
    existing = PrCreated(42, "https://github.com/acme/widgets/pull/42", True)
    assert run(p, source, lambda _: existing) == existing
    assert "m.json" in p["files"]
    assert run(p, source, lambda _: existing) == existing
    assert len(calls) == 1


def test_changed_source_aborts_before_upload(source_publisher, monkeypatch):
    p = source_publisher
    prepare(p)
    source = selected(p)
    p["files"]["m.json"] += " "
    monkeypatch.setattr(
        attachment_github, "resolve_target", lambda *args: pytest.fail("upload attempted")
    )
    with pytest.raises(OutboxChanged):
        run(p, source, lambda _: pytest.fail("create"))


@pytest.mark.parametrize("fail", [False, True])
def test_existing_pr_update_resolves_images_and_journals(
    source_publisher, monkeypatch, mocker, fail
):
    p = source_publisher
    prepare(p)
    source = selected(p)
    setup_upload(monkeypatch)
    mocker.patch("jailbee.git.get_remote_url", return_value="https://github.com/acme/widgets")
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.prompting._confirm", return_value=True)
    edit = mocker.patch("jailbee.pr.edit_pr")
    if fail:
        from jailbee.pr import PrEditError

        edit.side_effect = PrEditError("connection lost")
    result = pr_flow.apply_pr_updates(
        p["cfg"],
        p["incus"],
        "full",
        pr_flow.PrScope(p["cfg"].repo_root, "origin", "", None),
        number=42,
        branch="feature/a",
        base="main",
        title=None,
        body=None,
        description=False,
        ready=None,
        ai_on=False,
        foreign_head=False,
        url="https://github.com/acme/widgets/pull/42",
        use_outbox=True,
        outbox_hint=source,
    )
    assert edit.call_args.kwargs["body"] == f"![one]({URL}) ![two]({URL})"
    if fail:
        assert not result.body_changed
        assert open_publications().load("uuid-full", "pr", "m.json").actions[0].state == "uncertain"
    else:
        assert result.body_changed
        assert "m.json" not in p["files"]
