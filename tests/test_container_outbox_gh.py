from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from jailbee.container_outbox import gh


def completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def test_issue_view_maps_labels_state_and_default_repo(mocker: pytest.MockerFixture) -> None:
    run = mocker.patch.object(
        gh.subprocess,
        "run",
        return_value=completed(json.dumps({
            "title": "Title", "body": "Body", "labels": [{"name": "bug"}, {"name": "help"}],
            "state": "OPEN",
        })),
    )

    view = gh.issue_view(None, 12)

    assert view == gh.IssueView("Title", "Body", ("bug", "help"), "open")
    run.assert_called_once_with(
        ["gh", "issue", "view", "12", "--json", "title,body,labels,state"],
        cwd=None, capture_output=True, text=True, check=False, timeout=30,
    )


def test_issue_view_supports_explicit_github_repo(mocker: pytest.MockerFixture) -> None:
    run = mocker.patch.object(gh.subprocess, "run", return_value=completed(
        '{"title":"T","body":"B","labels":[],"state":"CLOSED"}'
    ))

    view = gh.issue_view("github:owner/project", 9)

    assert view.state == "closed"
    assert run.call_args.args[0][-2:] == ["-R", "owner/project"]


def test_issue_view_uses_submodule_as_cwd(mocker: pytest.MockerFixture) -> None:
    run = mocker.patch.object(gh.subprocess, "run", return_value=completed(
        '{"title":"T","body":"B","labels":[],"state":"OPEN"}'
    ))

    gh.issue_view("vendor/project", 4)

    assert run.call_args.kwargs["cwd"] == "vendor/project"
    assert "-R" not in run.call_args.args[0]


def test_pr_view_builds_repo_from_url_and_omits_missing_number(mocker: pytest.MockerFixture) -> None:
    run = mocker.patch.object(gh.subprocess, "run", return_value=completed(
        '{"number":7,"headRefOid":"abc123","url":"https://github.com/acme/widget/pull/7"}'
    ))

    view = gh.pr_view(None)

    assert view == gh.PrView(7, "abc123", "acme/widget")
    assert run.call_args.args[0] == ["gh", "pr", "view", "--json", "number,headRefOid,url"]


def test_pr_view_includes_number_when_given(mocker: pytest.MockerFixture) -> None:
    run = mocker.patch.object(gh.subprocess, "run", return_value=completed(
        '{"number":7,"headRefOid":"abc123","url":"https://github.com/acme/widget/pull/7"}'
    ))

    gh.pr_view(7)

    assert run.call_args.args[0][:4] == ["gh", "pr", "view", "7"]


def test_run_json_reports_nonzero_stderr(mocker: pytest.MockerFixture) -> None:
    mocker.patch.object(gh.subprocess, "run", return_value=completed(stderr="  not found\n", returncode=1))

    with pytest.raises(gh.GhError, match="not found"):
        gh.run_json(["gh", "issue", "view", "1"])


def test_run_json_reports_missing_binary(mocker: pytest.MockerFixture) -> None:
    mocker.patch.object(gh.subprocess, "run", side_effect=FileNotFoundError)

    with pytest.raises(gh.GhError, match="gh not found"):
        gh.run_json(["gh", "issue", "view", "1"])


@pytest.mark.parametrize("stdout", ["not json", "[]"])
def test_issue_view_reports_malformed_response(mocker: pytest.MockerFixture, stdout: str) -> None:
    mocker.patch.object(gh.subprocess, "run", return_value=completed(stdout=stdout))

    with pytest.raises(gh.GhError, match="retry.*gh issue view"):
        gh.issue_view(None, 3)


def test_pr_view_reports_timeout_with_retry_command(mocker: pytest.MockerFixture) -> None:
    mocker.patch.object(gh.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 30))

    with pytest.raises(gh.GhError, match="retry.*gh pr view"):
        gh.pr_view(5)
