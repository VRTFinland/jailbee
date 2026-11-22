"""`jailbee outbox resolve` and its `review resolve` / `issue resolve` aliases."""

from __future__ import annotations

from typer.testing import CliRunner

from jailbee.cli import app

URL = "https://github.com/o/r/pull/1#issuecomment-1"


def _invoke(argv: list[str]):
    return CliRunner().invoke(app, argv)


def test_pr_proposal_forwards_to_the_review_resolve_body(mocker):
    body = mocker.patch("jailbee.cli.run_review_resolve")

    outbox = _invoke(["outbox", "resolve", "c", "pr/m.json", "2", "--applied", "--url", URL])
    alias = _invoke(["review", "resolve", "c", "m.json", "2", "--applied", URL])

    assert outbox.exit_code == alias.exit_code == 0
    assert body.call_args_list[0] == body.call_args_list[1]
    assert body.call_args_list[0].args[1:4] == ("c", "m.json", 2)
    assert body.call_args_list[0].kwargs == {"url": URL, "retry": False}


def test_issue_proposal_forwards_to_the_issue_resolve_body(mocker):
    body = mocker.patch("jailbee.cli.run_issue_resolve")

    outbox = _invoke(
        [
            *["outbox", "resolve", "c", "issue/m.json", "1", "--applied"],
            "--url",
            URL,
            "--issue",
            "7",
            "-y",
        ]
    )
    alias = _invoke(
        ["issue", "resolve", "c", "m.json", "1", "--applied", "--url", URL, "--issue", "7", "-y"]
    )

    assert outbox.exit_code == alias.exit_code == 0
    assert body.call_args_list[0] == body.call_args_list[1]
    assert body.call_args_list[0].kwargs == {
        "applied": True,
        "url": URL,
        "issue": 7,
        "retry": False,
        "yes": True,
    }


def test_missing_kind_is_asked(mocker):
    from jailbee import prompting

    body = mocker.patch("jailbee.cli.run_issue_resolve")
    mocker.patch.object(prompting, "choose_one", return_value="issue")

    result = _invoke(["outbox", "resolve", "c", "--retry"])

    assert result.exit_code == 0, result.output
    assert body.call_args.args[2] is None  # manifest is asked for later by the body


def test_bare_manifest_name_is_a_usage_error(mocker):
    mocker.patch("jailbee.cli.run_issue_resolve")
    result = _invoke(["outbox", "resolve", "c", "m.json", "--retry"])
    assert result.exit_code == 2


def test_pr_proposal_requires_exactly_one_resolution(mocker):
    mocker.patch("jailbee.cli.run_review_resolve")
    assert _invoke(["outbox", "resolve", "c", "pr/m.json", "0"]).exit_code == 2
    assert _invoke(["outbox", "resolve", "c", "pr/m.json", "0", "--applied"]).exit_code == 2
    assert (
        _invoke(["outbox", "resolve", "c", "pr/m.json", "0", "--retry", "--issue", "3"]).exit_code
        == 2
    )
