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
        return_value=completed(
            json.dumps(
                {
                    "title": "Title",
                    "body": "Body",
                    "labels": [{"name": "bug"}, {"name": "help"}],
                    "state": "OPEN",
                    "assignees": [{"login": "alice"}, {"login": "bob"}],
                }
            )
        ),
    )

    view = gh.issue_view(None, 12)

    assert view == gh.IssueView("Title", "Body", ("bug", "help"), "open", ("alice", "bob"))
    run.assert_called_once_with(
        ["gh", "issue", "view", "12", "--json", "title,body,labels,state,assignees"],
        cwd=None,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_issue_view_supports_explicit_github_repo(mocker: pytest.MockerFixture) -> None:
    run = mocker.patch.object(
        gh.subprocess,
        "run",
        return_value=completed(
            '{"title":"T","body":"B","labels":[],"state":"CLOSED","assignees":[]}'
        ),
    )

    view = gh.issue_view("github:owner/project", 9)

    assert view.state == "closed"
    assert run.call_args.args[0][-2:] == ["-R", "owner/project"]


def test_issue_view_uses_submodule_as_cwd(mocker: pytest.MockerFixture) -> None:
    run = mocker.patch.object(
        gh.subprocess,
        "run",
        return_value=completed(
            '{"title":"T","body":"B","labels":[],"state":"OPEN","assignees":[]}'
        ),
    )

    gh.issue_view("vendor/project", 4)

    assert run.call_args.kwargs["cwd"] == "vendor/project"
    assert "-R" not in run.call_args.args[0]


@pytest.mark.parametrize("response", [{"body": "Original\nbody"}, {"body": ""}])
def test_pr_body_reads_only_current_body(
    mocker: pytest.MockerFixture, response: dict[str, str]
) -> None:
    run = mocker.patch.object(gh.subprocess, "run", return_value=completed(json.dumps(response)))
    assert gh.pr_body(7) == response["body"]
    assert run.call_args.args[0] == ["gh", "pr", "view", "7", "--json", "body"]


@pytest.mark.parametrize("stdout", ["{}", '{"body":null}', "[]", "not-json"])
def test_pr_body_rejects_invalid_response(mocker: pytest.MockerFixture, stdout: str) -> None:
    mocker.patch.object(gh.subprocess, "run", return_value=completed(stdout))
    with pytest.raises(gh.GhError, match=r"retry.*gh pr view 7"):
        gh.pr_body(7)


def test_pr_view_builds_repo_from_url_and_omits_missing_number(
    mocker: pytest.MockerFixture,
) -> None:
    run = mocker.patch.object(
        gh.subprocess,
        "run",
        return_value=completed(
            '{"number":7,"headRefOid":"abc123","url":"https://github.com/acme/widget/pull/7"}'
        ),
    )

    view = gh.pr_view(None)

    assert view == gh.PrView(7, "abc123", "acme/widget")
    assert run.call_args.args[0] == ["gh", "pr", "view", "--json", "number,headRefOid,url"]


def test_pr_view_uses_bound_repository_cwd(mocker):
    run = mocker.patch.object(
        gh.subprocess,
        "run",
        return_value=completed(
            '{"number":7,"headRefOid":"abc","url":"https://github.com/acme/lib/pull/7"}'
        ),
    )
    assert gh.pr_view(7, cwd="/repo/lib").repo == "acme/lib"
    assert run.call_args.kwargs["cwd"] == "/repo/lib"
    assert run.call_args.args[0][:4] == ["gh", "pr", "view", "7"]


@pytest.mark.parametrize(
    "value", [{"nameWithOwner": "acme/widgets"}, {}, {"nameWithOwner": "invalid"}, []]
)
def test_repo_discovery_without_a_pr(mocker, value):
    run = mocker.patch.object(gh.subprocess, "run", return_value=completed(json.dumps(value)))
    if value == {"nameWithOwner": "acme/widgets"}:
        assert gh.repo_view() == "acme/widgets"
    else:
        with pytest.raises(gh.GhError, match="supply --repo"):
            gh.repo_view()
    assert run.call_args.args[0] == ["gh", "repo", "view", "--json", "nameWithOwner"]


def test_pr_view_includes_number_when_given(mocker: pytest.MockerFixture) -> None:
    run = mocker.patch.object(
        gh.subprocess,
        "run",
        return_value=completed(
            '{"number":7,"headRefOid":"abc123","url":"https://github.com/acme/widget/pull/7"}'
        ),
    )

    gh.pr_view(7)

    assert run.call_args.args[0][:4] == ["gh", "pr", "view", "7"]


@pytest.mark.parametrize(
    ("stderr", "expected"), [("  not found\n", "not found"), ("", "gh failed")]
)
def test_run_json_reports_nonzero_stderr(
    mocker: pytest.MockerFixture, stderr: str, expected: str
) -> None:
    mocker.patch.object(gh.subprocess, "run", return_value=completed(stderr=stderr, returncode=1))

    with pytest.raises(gh.GhError) as error:
        gh.run_json(["gh", "issue", "view", "1"])

    assert str(error.value) == expected


def test_run_json_reports_missing_binary(mocker: pytest.MockerFixture) -> None:
    mocker.patch.object(gh.subprocess, "run", side_effect=FileNotFoundError(2, "missing", "gh"))

    with pytest.raises(gh.GhError, match=r"^gh not found$"):
        gh.run_json(["gh", "issue", "view", "1"])


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(2, "missing directory", "vendor/project"),
        NotADirectoryError(20, "not a directory", "vendor/project"),
        PermissionError(13, "permission denied", "vendor/project"),
    ],
)
def test_run_json_reports_invalid_cwd_as_path_error(
    mocker: pytest.MockerFixture, error: OSError
) -> None:
    mocker.patch.object(gh.subprocess, "run", side_effect=error)

    with pytest.raises(gh.GhError, match="vendor/project"):
        gh.run_json(["gh", "issue", "view", "4"], cwd="vendor/project")


@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        "[]",
        '{"title":"T","body":"B","labels":[],"state":"OPEN","assignees":"alice"}',
        '{"title":"T","body":"B","labels":[],"state":"OPEN","assignees":[{"name":"alice"}]}',
    ],
)
def test_issue_view_reports_malformed_response(mocker: pytest.MockerFixture, stdout: str) -> None:
    mocker.patch.object(gh.subprocess, "run", return_value=completed(stdout=stdout))

    with pytest.raises(gh.GhError, match=r"retry.*gh issue view"):
        gh.issue_view(None, 3)


def test_issue_view_adds_retry_command_to_gh_failure(mocker: pytest.MockerFixture) -> None:
    mocker.patch.object(
        gh.subprocess, "run", return_value=completed(stderr="issue not found", returncode=1)
    )

    with pytest.raises(gh.GhError, match=r"issue not found.*retry.*gh issue view 3"):
        gh.issue_view(None, 3)


def test_pr_view_reports_timeout_with_retry_command(mocker: pytest.MockerFixture) -> None:
    mocker.patch.object(gh.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 30))

    with pytest.raises(gh.GhError, match=r"retry.*gh pr view"):
        gh.pr_view(5)


def test_pr_view_adds_retry_command_to_gh_failure(mocker: pytest.MockerFixture) -> None:
    mocker.patch.object(gh.subprocess, "run", return_value=completed(stderr="no PR", returncode=1))

    with pytest.raises(gh.GhError, match=r"no PR.*retry.*gh pr view 5"):
        gh.pr_view(5)


@pytest.mark.parametrize("number", [True, 0, -1])
def test_pr_view_rejects_invalid_numbers(mocker: pytest.MockerFixture, number: object) -> None:
    mocker.patch.object(
        gh.subprocess,
        "run",
        return_value=completed(
            json.dumps(
                {"number": number, "headRefOid": "abc", "url": "https://github.com/o/r/pull/1"}
            )
        ),
    )

    with pytest.raises(gh.GhError, match="invalid PR response"):
        gh.pr_view(1)


def test_pr_view_rejects_non_github_url(mocker: pytest.MockerFixture) -> None:
    mocker.patch.object(
        gh.subprocess,
        "run",
        return_value=completed(
            '{"number":1,"headRefOid":"abc","url":"https://example.com/o/r/pull/1"}'
        ),
    )

    with pytest.raises(gh.GhError, match="invalid PR response"):
        gh.pr_view(1)
