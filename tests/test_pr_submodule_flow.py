"""Direct tests for `pr_submodule_flow.publish_submodule_pr`."""

from __future__ import annotations

import pytest
import typer

from tests.conftest import mock_pr_agent


def _candidate(path="lib/a", commits=2, branch="feat/foo"):
    from jailbee.submodule_pr import SubCandidate

    return SubCandidate(
        path=path, commits=commits, branch=branch, dirty=False,
        head_sha="aaa", recorded_sha="aaa", subject="feat: work",
    )


def _env(mocker, tmp_path, *, record=None):
    from jailbee.pr import PrCreated
    from jailbee.pr_flow import PrRecord
    from jailbee.pr_outbox import Outbox
    from jailbee.submodule_pr import SubPublishResult

    cfg = mocker.MagicMock()
    cfg.repo_root = tmp_path
    cfg.container_user.uid = 1000
    cfg.upstream_remote = "origin"
    mock_pr_agent(cfg, False)
    cfg.pr.ai_description = False
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        {"name": name, "created_at": "2026-09-30T12:00:00Z"}
        for name in ("sampleapp-feat-foo", "c")
    ]
    incus.config_get.return_value = None
    mocker.patch("jailbee.pr_flow.validate_outbox_source")
    mocker.patch("jailbee.submodule_pr.transport_submodule_to_host")
    mocker.patch("jailbee.submodule_pr.resolve_remote", return_value="origin")
    mocker.patch("jailbee.submodule_pr.resolve_base_branch", return_value="develop")
    mocker.patch(
        "jailbee.submodule_pr.SubmodulePrState.read",
        return_value=record or PrRecord(None, None, False, False),
    )
    mocker.patch("jailbee.submodule_pr.SubmodulePrState.record")
    mocker.patch("jailbee.pr.find_pr_for_branch", return_value=None)
    mocker.patch("jailbee.pr.assert_github_remote")
    mocker.patch("jailbee.pr_outbox.pending_pr_text", return_value=None)
    mocker.patch("jailbee.git.get_remote_url", return_value="https://github.com/acme/lib-a")
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=Outbox(files={}))
    mocker.patch(
        "jailbee.submodule_pr.publish_submodule_branch",
        return_value=SubPublishResult(src_ref="r", publish_name="feat/foo", forced=False),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="feat: work")
    create = mocker.patch(
        "jailbee.pr.create_pr",
        return_value=PrCreated(number=7, url="https://github.com/acme/lib-a/pull/7",
                               already_existed=False),
    )
    return cfg, incus, create


def test_create_returns_created_outcome_and_offers_comments(mocker, tmp_path):
    from jailbee.pr_submodule_flow import SubPrOptions, publish_submodule_pr

    cfg, incus, create = _env(mocker, tmp_path)
    offer = mocker.Mock(return_value=0)
    outcome = publish_submodule_pr(
        cfg, incus, "sampleapp-feat-foo", "feat-foo", _candidate(), SubPrOptions(),
        repo_dir="/home/dev/repo", confirm_plan=None, offer_comments=offer,
    )
    assert (outcome.subpath, outcome.action, outcome.number) == ("lib/a", "created", 7)
    assert outcome.url == "https://github.com/acme/lib-a/pull/7"
    create.assert_called_once()
    assert offer.call_args.args[0] == 7


def test_confirm_plan_none_skips_the_plan_block(mocker, tmp_path):
    from jailbee.pr_submodule_flow import SubPrOptions, publish_submodule_pr

    cfg, incus, _ = _env(mocker, tmp_path)
    confirm = mocker.Mock()
    publish_submodule_pr(
        cfg, incus, "c", "s", _candidate(), SubPrOptions(), repo_dir="/r",
        confirm_plan=None, offer_comments=lambda n, m: 0,
    )
    publish_submodule_pr(
        cfg, incus, "c", "s", _candidate(), SubPrOptions(), repo_dir="/r",
        confirm_plan=confirm, offer_comments=lambda n, m: 0,
    )
    confirm.assert_called_once()


def test_detached_without_name_raises_exit_2(mocker, tmp_path):
    from jailbee.pr_submodule_flow import SubPrOptions, publish_submodule_pr

    cfg, incus, _ = _env(mocker, tmp_path)
    with pytest.raises(typer.Exit) as exc:
        publish_submodule_pr(
            cfg, incus, "c", "s", _candidate(branch=None), SubPrOptions(no_ai=True),
            repo_dir="/r", confirm_plan=None, offer_comments=lambda n, m: 0,
        )
    assert exc.value.exit_code == 2


def test_outbox_failures_are_returned_not_raised(mocker, tmp_path):
    from jailbee.pr_submodule_flow import SubPrOptions, publish_submodule_pr

    cfg, incus, _ = _env(mocker, tmp_path)
    outcome = publish_submodule_pr(
        cfg, incus, "c", "s", _candidate(), SubPrOptions(), repo_dir="/r",
        confirm_plan=None, offer_comments=lambda n, m: 2,
    )
    assert outcome.outbox_failures == 2
