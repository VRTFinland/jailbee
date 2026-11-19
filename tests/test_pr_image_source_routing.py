"""Image-backed description routing through PR creation."""

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.pr_ai import PrText
from jailbee.pr_outbox import OutboxPrText
from tests.test_cli_pr import _pr_created, _publish_result, _setup
from tests.test_pr_submodule_flow import _candidate, _env

BODY = "Before ![screen](attachment:screen)"
RENDERED = "Before ![screen](https://example.com/image.png)"


def source():
    return OutboxPrText(
        text=PrText(title="Image title", body=BODY, branch="feat/foo"),
        manifest="001-description.json",
        index=0,
    )


@pytest.mark.parametrize("override", [False, True])
def test_root_create_routes_only_winning_image_body(mocker, tmp_path, override):
    _setup(mocker, tmp_path)
    mocker.patch("jailbee.pr_submodule_flow.publish_submodule_prs_first", return_value=[])
    selected = source()
    mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=selected)
    mocker.patch("jailbee.sync.publish_branch_from_container", return_value=_publish_result())
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())
    consume = mocker.patch("jailbee.pr_flow.record_outbox_consumption")
    consumed = []

    def publish_description(cfg, incus, container, src, body, create, confirm, **kw):
        result = create(RENDERED)
        consumed.append(src)
        return result

    publish = mocker.patch(
        "jailbee.outbox.pr_description.create_with_description", side_effect=publish_description
    )
    args = ["pr", "push", "feat-foo", "--yes"]
    if override:
        args += ["--body", "Manual body"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, (result.output, result.exception)
    assert create.call_args.kwargs["body"] == ("Manual body" if override else RENDERED)
    if override:
        publish.assert_not_called()
    else:
        publish.assert_called_once()
        assert publish.call_args.args[3] is selected
    assert consumed == ([] if override else [selected])
    consume.assert_not_called()


def test_root_no_outbox_does_not_publish_image(mocker, tmp_path):
    _setup(mocker, tmp_path)
    mocker.patch("jailbee.pr_submodule_flow.publish_submodule_prs_first", return_value=[])
    pending = mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=source())
    mocker.patch("jailbee.sync.publish_branch_from_container", return_value=_publish_result())
    mocker.patch("jailbee.git.commit_subject", return_value="Feature")
    mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())
    publish = mocker.patch("jailbee.outbox.pr_description.create_with_description")
    result = CliRunner().invoke(app, ["pr", "push", "feat-foo", "--yes", "--no-outbox", "--no-ai"])
    assert result.exit_code == 0, (result.output, result.exception)
    pending.assert_not_called()
    publish.assert_not_called()


def test_submodule_create_routes_image_description(mocker, tmp_path):
    from jailbee.pr_submodule_flow import SubPrOptions, publish_submodule_pr

    cfg, incus, create = _env(mocker, tmp_path)
    selected = source()
    mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=selected)
    consume = mocker.patch("jailbee.pr_flow.record_outbox_consumption")
    consumed = []

    def publish_description(cfg, incus, container, src, body, create, confirm, **kw):
        result = create(RENDERED)
        consumed.append(src)
        return result

    publish = mocker.patch(
        "jailbee.outbox.pr_description.create_with_description", side_effect=publish_description
    )
    publish_submodule_pr(
        cfg,
        incus,
        "c",
        "s",
        _candidate(),
        SubPrOptions(),
        repo_dir="/r",
        confirm_plan=None,
        offer_comments=lambda n, m: 0,
    )
    publish.assert_called_once()
    assert publish.call_args.args[3] is selected
    assert create.call_args.kwargs["body"] == RENDERED
    assert consumed == [selected]
    consume.assert_not_called()
