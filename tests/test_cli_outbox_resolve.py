"""`jailbee outbox resolve` and its `review resolve` / `issue resolve` aliases."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jailbee import prompting
from jailbee.cli import app
from jailbee.outbox.models import ContainerView, ProposalId, ProposalView

URL = "https://github.com/o/r/pull/1#issuecomment-1"
MOD = "jailbee.cli_outbox_resolve"


def _invoke(argv: list[str]):
    return CliRunner().invoke(app, argv)


@pytest.fixture
def host(mocker):
    """No real config, daemon or container lookup behind `outbox resolve`."""
    cfg = mocker.MagicMock()
    mocker.patch("jailbee.config.load_repo_config", return_value=cfg)
    mocker.patch("jailbee.incus.Incus")
    existing = mocker.patch(f"{MOD}._existing_container", return_value="full-c")
    return existing


def _view(name: str, *proposals: tuple[str, str]) -> ContainerView:
    rows = tuple(
        ProposalView(ProposalId.parse(pid), "r", "", (), state, None, None)  # type: ignore[arg-type]
        for pid, state in proposals
    )
    return ContainerView(None, name, True, None, (), rows)


def test_pr_proposal_forwards_to_the_pr_body(mocker, host):
    body = mocker.patch(f"{MOD}.resolve_pr")

    result = _invoke(["outbox", "resolve", "c", "pr/m.json", "2", "--applied", "--url", URL])

    assert result.exit_code == 0, result.output
    assert body.call_args.args[2:5] == ("full-c", "m.json", 2)
    assert body.call_args.kwargs == {"applied": True, "url": URL, "retry": False}


def test_issue_proposal_forwards_to_the_issue_body(mocker, host):
    body = mocker.patch(f"{MOD}.resolve_issue")
    args = ["--issue", "7", "-y"]

    result = _invoke(
        ["outbox", "resolve", "c", "issue/m.json", "1", "--applied", "--url", URL, *args]
    )

    assert result.exit_code == 0, result.output
    assert body.call_args.args[2:5] == ("full-c", "m.json", 1)
    assert body.call_args.kwargs == {
        "applied": True,
        "url": URL,
        "issue": 7,
        "retry": False,
        "yes": True,
    }


def test_aliases_reach_the_same_bodies(mocker):
    pr = mocker.patch(f"{MOD}.resolve_pr")
    issue = mocker.patch(f"{MOD}.resolve_issue")
    mocker.patch("jailbee.cli._load_or_exit")
    mocker.patch("jailbee.incus.Incus")
    mocker.patch(f"{MOD}._review_container", return_value="full-c")
    mocker.patch(f"{MOD}._issue_container", return_value="full-c")

    assert _invoke(["review", "resolve", "c", "m.json", "2", "--applied", URL]).exit_code == 0
    assert _invoke(["issue", "resolve", "c", "m.json", "1", "--retry"]).exit_code == 0

    assert pr.call_args.kwargs == {"applied": True, "url": URL, "retry": False}
    assert issue.call_args.kwargs["retry"] is True


def test_missing_container_and_proposal_are_asked_container_first(mocker, host):
    mocker.patch(
        "jailbee.outbox.commands.discover",
        return_value=(
            _view("full-a", ("pr/p.json", "pending")),
            _view("full-b", ("pr/q.json", "uncertain"), ("issue/i.json", "uncertain")),
        ),
    )
    pr = mocker.patch(f"{MOD}.resolve_pr")
    issue = mocker.patch(f"{MOD}.resolve_issue")
    choose = mocker.patch.object(
        prompting, "choose_one", side_effect=["full-b", ProposalId.parse("issue/i.json")]
    )

    result = _invoke(["outbox", "resolve", "--retry"])

    assert result.exit_code == 0, result.output
    asked = [c.args[0] for c in choose.call_args_list]
    assert asked == ["container", "proposal"]
    # Only the container with something uncertain is offered, and only its uncertain proposals.
    assert [o.value for o in choose.call_args_list[0].args[1]] == ["full-b"]
    assert len(choose.call_args_list[1].args[1]) == 2
    pr.assert_not_called()
    assert issue.call_args.args[2:4] == ("full-b", "i.json")


def test_nothing_pending_is_success_off_a_terminal(mocker, host):
    mocker.patch(
        "jailbee.outbox.commands.discover",
        return_value=(_view("full-a", ("pr/p.json", "pending")),),
    )
    pr = mocker.patch(f"{MOD}.resolve_pr")

    result = _invoke(["outbox", "resolve", "--retry"])

    assert result.exit_code == 0, result.output
    assert "Nothing pending" in result.output
    pr.assert_not_called()


def test_destroyed_instance_reference_is_taken_as_is(mocker, host):
    body = mocker.patch(f"{MOD}.resolve_pr")
    ref = "acme-feat-foo@0123abcd-0000-4000-8000-000000000000"

    result = _invoke(["outbox", "resolve", ref, "pr/m.json", "0", "--retry"])

    assert result.exit_code == 0, result.output
    host.assert_not_called()
    assert body.call_args.args[2] == ref


def test_bare_manifest_name_is_a_usage_error(mocker, host):
    mocker.patch(f"{MOD}.resolve_issue")
    result = _invoke(["outbox", "resolve", "c", "m.json", "--retry"])
    assert result.exit_code == 2


@pytest.mark.parametrize(
    "flags",
    [["--applied", "--retry"], ["--retry", "--url", URL]],
)
def test_pr_proposal_rejects_conflicting_resolutions(host, mocker, flags):
    mocker.patch(f"{MOD}._read_pr_outbox_or_exit")
    assert _invoke(["outbox", "resolve", "c", "pr/m.json", "0", *flags]).exit_code == 2


def test_pr_proposal_rejects_issue_number(mocker, host):
    mocker.patch(f"{MOD}._read_pr_outbox_or_exit")
    result = _invoke(["outbox", "resolve", "c", "pr/m.json", "0", "--retry", "--issue", "3"])
    assert result.exit_code == 2


def test_pr_without_mode_or_url_is_asked_not_a_usage_error(mocker):
    from jailbee.cli_outbox_resolve import resolve_pr

    settle = mocker.patch("jailbee.branch_pr.resolve_uncertain_source", return_value=True)
    mocker.patch(f"{MOD}._read_pr_outbox_or_exit")
    mocker.patch("jailbee.lifecycle.short_name", return_value="c")
    choose = mocker.patch.object(prompting, "choose_one", return_value="applied")
    ask = mocker.patch.object(prompting, "ask_text", return_value=URL)

    resolve_pr(
        mocker.MagicMock(),
        mocker.MagicMock(),
        "full-c",
        "m.json",
        0,
        applied=False,
        url=None,
        retry=False,
    )

    assert choose.call_args.args[0] == "resolution"
    assert ask.call_args.args[0].startswith("GitHub URL")
    assert settle.call_args.kwargs["landed_url"] == URL


def test_pr_retry_chosen_at_the_prompt_forgets_the_outcome(mocker):
    from jailbee.cli_outbox_resolve import resolve_pr

    settle = mocker.patch("jailbee.branch_pr.resolve_uncertain_source", return_value=False)
    mocker.patch(f"{MOD}._read_pr_outbox_or_exit")
    mocker.patch("jailbee.lifecycle.short_name", return_value="c")
    mocker.patch.object(prompting, "choose_one", return_value="retry")

    resolve_pr(
        mocker.MagicMock(),
        mocker.MagicMock(),
        "full-c",
        "m.json",
        0,
        applied=False,
        url=None,
        retry=False,
    )

    assert settle.call_args.kwargs["landed_url"] is None
