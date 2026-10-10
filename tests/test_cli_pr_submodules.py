"""`jailbee pr` publishing submodule PRs first."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.pr_submodule_flow import SubPrOutcome
from tests.test_cli_pr import (
    _adopted_setup,
    _pr_created,
    _publish_result,
    _review_pr_info,
    _review_setup,
    _setup,
    _stacked_setup,
)

runner = CliRunner()


def _wire(mocker, tmp_path, outcomes):
    _setup(mocker, tmp_path)
    mocker.patch("jailbee.sync.publish_branch_from_container", return_value=_publish_result())
    mocker.patch("jailbee.git.commit_subject", return_value="feat: x")
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())
    step = mocker.patch(
        "jailbee.pr_submodule_flow.publish_submodule_prs_first", return_value=outcomes
    )
    link = mocker.patch("jailbee.pr_links.link_pr_family")
    return step, create, link


def test_submodules_run_before_superproject_and_links_after(mocker, tmp_path):
    order = []
    step, create, link = _wire(mocker, tmp_path, [])
    step.side_effect = lambda *a, **k: order.append("subs") or []
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        side_effect=lambda *a, **k: order.append("super") or _publish_result(),
    )
    create.side_effect = lambda *a, **k: order.append("create") or _pr_created()
    link.side_effect = lambda *a, **k: order.append("links")
    result = runner.invoke(app, ["pr", "feat-foo", "--no-ai"])
    assert result.exit_code == 0, result.output
    assert order == ["subs", "super", "create", "links"]
    link.assert_called_once_with(
        step.call_args.args[0], step.call_args.args[1], "sampleapp-feat-foo", "feat-foo"
    )


def test_flags_and_comments_callback_reach_step(mocker, tmp_path):
    from jailbee import pr_flow

    guard = mocker.spy(pr_flow, "outbox_publication_guard")
    step, _, _ = _wire(mocker, tmp_path, [])
    result = runner.invoke(
        app, ["pr", "feat-foo", "--no-ai", "--no-outbox", "--ready", "--yes", "--no-submodules"]
    )
    assert result.exit_code == 0, result.output
    kw = step.call_args.kwargs
    assert kw["management"] is guard.call_args.kwargs["management"]
    assert (kw["enabled"], kw["yes"], kw["no_ai"], kw["no_outbox"], kw["ready"]) == (
        False,
        True,
        True,
        True,
        True,
    )
    offer = mocker.patch("jailbee.cli._offer_outbox_comments", return_value=2)
    management = object()
    assert kw["offer_comments"](456, management) == 2
    offer.assert_called_once_with(*step.call_args.args, number=456, management=management)


@pytest.mark.parametrize(
    "outcome",
    [
        SubPrOutcome("lib/a", "failed"),
        SubPrOutcome("lib/a", "created", 45, "https://github.com/acme/lib/pull/45", 1),
        SubPrOutcome("lib/a", "updated", 45, "https://github.com/acme/lib/pull/45", 2),
    ],
)
def test_submodule_failure_finishes_superproject_and_links_before_exit_1(mocker, tmp_path, outcome):
    _, create, link = _wire(mocker, tmp_path, [outcome])
    result = runner.invoke(app, ["pr", "feat-foo", "--no-ai"])
    create.assert_called_once()
    link.assert_called_once()
    assert result.exit_code == 1, result.output
    assert "https://github.com/acme/widgets/pull/123" in result.output


def test_submodule_failure_asks_before_pushing_the_superproject(mocker, tmp_path):
    """A failed submodule PR leaves its commits off that submodule's remote, and
    a host with `push.recurseSubmodules=check` then refuses the superproject
    push outright. On a terminal the user decides before anything is pushed."""
    _, create, _ = _wire(mocker, tmp_path, [SubPrOutcome("docs", "failed")])
    push = mocker.patch(
        "jailbee.sync.publish_branch_from_container", return_value=_publish_result()
    )
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    confirm = mocker.patch("typer.confirm", return_value=False)

    result = runner.invoke(app, ["pr", "feat-foo", "--no-ai"])

    assert result.exit_code == 1, result.output
    assert "docs" in confirm.call_args.args[0]
    push.assert_not_called()
    create.assert_not_called()


def test_submodule_failure_superproject_continues_when_confirmed(mocker, tmp_path):
    _, create, _ = _wire(mocker, tmp_path, [SubPrOutcome("docs", "failed")])
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("typer.confirm", return_value=True)

    result = runner.invoke(app, ["pr", "feat-foo", "--no-ai"])

    assert result.exit_code == 1, result.output
    create.assert_called_once()


def test_declined_submodule_is_not_a_failure(mocker, tmp_path):
    step, _, link = _wire(mocker, tmp_path, [SubPrOutcome("lib/a", "declined")])
    result = runner.invoke(app, ["pr", "feat-foo", "--no-ai"])
    assert result.exit_code == 0, result.output
    step.assert_called_once()
    link.assert_called_once()


@pytest.mark.parametrize("args", [["--pr", "5", "--as", "x"], ["--stacked", "--pr", "5"]])
def test_usage_error_never_reaches_submodule_step(mocker, tmp_path, args):
    step, _, link = _wire(mocker, tmp_path, [])
    result = runner.invoke(app, ["pr", "feat-foo", *args])
    assert result.exit_code == 2
    step.assert_not_called()
    link.assert_not_called()


def test_open_never_reaches_submodule_step(mocker, tmp_path):
    step, _, link = _wire(mocker, tmp_path, [])
    _setup(mocker, tmp_path, labels={"user.jailbee.pr": "123"})
    opened = mocker.patch("jailbee.pr.open_pr_in_browser")
    result = runner.invoke(app, ["pr", "feat-foo", "--open"])
    assert result.exit_code == 0, result.output
    opened.assert_called_once()
    step.assert_not_called()
    link.assert_not_called()


@pytest.mark.parametrize("path", ["review", "adopted", "stacked"])
def test_review_adopted_and_stacked_paths_orchestrate(mocker, tmp_path, path):
    step, _, link = _wire(mocker, tmp_path, [])
    args = ["pr", "feat-foo", "--no-ai", "--yes"]
    if path == "stacked":
        _, publish, _, _ = _stacked_setup(mocker, tmp_path)
        args += ["--stacked", "--as", "fix/worktime-review", "--no-retarget"]
    elif path == "adopted":
        _, _, publish = _adopted_setup(mocker, tmp_path)
        mocker.patch("jailbee.pr.view_existing_pr", return_value=_pr_created(already=True))
    else:
        _, _, publish = _review_setup(mocker, tmp_path)
        mocker.patch("jailbee.pr.resolve_pr", return_value=_review_pr_info())
        mocker.patch("jailbee.pr.view_existing_pr", return_value=_pr_created(already=True))
    order = []
    step.side_effect = lambda *a, **k: order.append("subs") or []
    publish.side_effect = lambda *a, **k: order.append("super") or _publish_result()
    link.side_effect = lambda *a, **k: order.append("links")
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert order == ["subs", "super", "links"]


def test_invalid_stacked_head_never_publishes_submodules(mocker, tmp_path):
    step, _, _ = _wire(mocker, tmp_path, [])
    _stacked_setup(mocker, tmp_path)
    result = runner.invoke(app, ["pr", "feat-foo", "--stacked", "--no-ai"])
    assert result.exit_code == 2, result.output
    step.assert_not_called()


@pytest.mark.parametrize("has_candidates", [False, True])
def test_real_step_off_tty_does_not_publish_without_yes(mocker, tmp_path, has_candidates):
    # Exercise the real selection gate, not a mock of the orchestration.
    from jailbee import pr_submodule_flow
    from jailbee.submodule_pr import SubCandidate

    _setup(mocker, tmp_path)
    mocker.patch("jailbee.sync.publish_branch_from_container", return_value=_publish_result())
    mocker.patch("jailbee.git.commit_subject", return_value="feat: x")
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())
    mocker.patch("jailbee.pr_links.link_pr_family")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    candidates = (
        [
            SubCandidate(
                path="lib/a",
                branch="feat/x",
                commits=1,
                dirty=False,
                head_sha="abc",
                recorded_sha="def",
                subject="feat: x",
            )
        ]
        if has_candidates
        else []
    )
    mocker.patch.object(pr_submodule_flow, "submodule_pr_candidates", return_value=candidates)
    publish = mocker.patch.object(pr_submodule_flow, "publish_submodule_pr")
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    result = runner.invoke(app, ["pr", "feat-foo", "--no-ai"])
    assert result.exit_code == 0, result.output
    create.assert_called_once()
    publish.assert_not_called()
    assert ("Submodule PR candidates" in result.output) is has_candidates
    if not has_candidates:
        assert "submodule" not in result.output.lower()


def test_shared_manager_reenters_real_guard_for_submodule_publication(mocker, tmp_path):
    from jailbee import pr_flow, pr_submodule_flow
    from jailbee.outbox.io import PrManagement
    from jailbee.outbox_io import ContainerIdentity
    from tests.test_pr_submodule_flow import _candidate, _env

    cfg, incus, _ = _env(mocker, tmp_path)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    mocker.patch.object(pr_submodule_flow, "submodule_pr_candidates", return_value=[_candidate()])
    manager = PrManagement(tmp_path / "locks")
    fallback = mocker.patch(
        "jailbee.outbox.io.PrManagement", side_effect=AssertionError("second manager")
    )
    seen = []
    with pr_flow.outbox_publication_guard(cfg, incus, "c", enabled=True, management=manager):
        outcomes = pr_submodule_flow.publish_submodule_prs_first(
            cfg,
            incus,
            "c",
            "s",
            enabled=True,
            yes=True,
            no_ai=True,
            no_outbox=False,
            ready=None,
            management=manager,
            offer_comments=lambda n, m: seen.append(m) or 0,
        )
        assert manager.identity == ContainerIdentity("c", "2026-09-30T12:00:00Z")
    assert [(o.action, o.number) for o in outcomes] == [("created", 7)]
    assert seen == [manager]
    fallback.assert_not_called()


def test_real_transport_git_error_isolated_before_next_and_superproject(mocker, tmp_path):
    from jailbee import pr_submodule_flow, submodule_pr
    from jailbee.git import GitError
    from jailbee.pr_flow import PrRecord
    from tests.test_pr_submodule_flow import _candidate

    _setup(mocker, tmp_path)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    mocker.patch.object(
        pr_submodule_flow,
        "submodule_pr_candidates",
        return_value=[_candidate("lib/a"), _candidate("lib/b")],
    )
    mocker.patch(
        "jailbee.submodule_pr.SubmodulePrState.read",
        return_value=PrRecord(None, None, False, False),
    )
    mocker.patch("jailbee.submodules.host_subrepo_exists", return_value=True)
    mocker.patch("jailbee.submodule_pr.resolve_remote", return_value="origin")
    mocker.patch("jailbee.submodule_pr.resolve_base_branch", return_value="main")
    order = []

    def transport(*args, **kwargs):
        order.append(kwargs["subpath"])
        if kwargs["subpath"] == "lib/a":
            raise GitError("fetch failed")

    mocker.patch.object(submodule_pr, "transport_submodule_to_host", side_effect=transport)
    mocker.patch("jailbee.pr.assert_github_remote")
    mocker.patch(
        "jailbee.submodule_pr.publish_submodule_branch",
        return_value=submodule_pr.SubPublishResult("ref", "feat/foo", False),
    )
    mocker.patch("jailbee.submodule_pr.SubmodulePrState.record")
    mocker.patch(
        "jailbee.sync.publish_branch_from_container",
        side_effect=lambda *a, **k: order.append("super") or _publish_result(),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="feat: x")
    create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())
    mocker.patch("jailbee.pr_links.link_pr_family")
    result = runner.invoke(app, ["pr", "feat-foo", "--no-ai", "--no-outbox", "--yes"])
    assert order == ["lib/a", "lib/b", "super"], result.output
    assert create.call_count == 2
    assert result.exit_code == 1, result.output
    assert "fetch failed" in result.output
    assert "lib/a" in result.output and "failed" in result.output
    assert "https://github.com/acme/widgets/pull/123" in result.output
