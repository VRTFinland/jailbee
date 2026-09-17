from __future__ import annotations

import json
import subprocess
import traceback
from typing import Any

import pytest

from jailbee.issue_github import (
    IssueGithubMutationError,
    IssueGithubReadError,
    IssueSnapshot,
    MutationReceipt,
    add_comment,
    create_issue,
    current_login,
    edit_issue,
    get_issue,
    list_labels,
    replace_labels,
    set_state,
)


def _completed(
    returncode: int = 0, *, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _issue_payload(**overrides: object) -> str:
    payload: dict[str, object] = {
        "number": 42,
        "title": "Cache bug",
        "body": "Steps",
        "labels": [{"name": "bug"}, {"name": "needs-triage"}],
        "state": "open",
        "html_url": "https://github.com/acme/widgets/issues/42",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _assert_api_call(run: Any, repo_root: object, cmd: list[str], payload: str | None) -> None:
    run.assert_called_once_with(
        cmd,
        cwd=repo_root,
        input=payload,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_current_login_reads_the_host_gh_identity(tmp_path, mocker):
    run = mocker.patch(
        "jailbee.issue_github.subprocess.run",
        return_value=_completed(stdout='{"login": "host-user"}'),
    )

    assert current_login(tmp_path) == "host-user"
    _assert_api_call(run, tmp_path, ["gh", "api", "user"], None)


def test_get_issue_returns_a_typed_snapshot_and_exact_api_call(tmp_path, mocker):
    run = mocker.patch(
        "jailbee.issue_github.subprocess.run", return_value=_completed(stdout=_issue_payload())
    )

    snapshot = get_issue(tmp_path, "acme/widgets", 42)

    assert snapshot == IssueSnapshot(
        number=42,
        title="Cache bug",
        body="Steps",
        labels=("bug", "needs-triage"),
        state="open",
        url="https://github.com/acme/widgets/issues/42",
        is_pull_request=False,
    )
    _assert_api_call(run, tmp_path, ["gh", "api", "repos/acme/widgets/issues/42"], None)


def test_get_issue_normalizes_a_null_body_and_detects_pull_requests(tmp_path, mocker):
    mocker.patch(
        "jailbee.issue_github.subprocess.run",
        return_value=_completed(stdout=_issue_payload(body=None, pull_request={"url": "api-url"})),
    )

    snapshot = get_issue(tmp_path, "acme/widgets", 42)

    assert snapshot.body == ""
    assert snapshot.is_pull_request is True


def test_list_labels_flattens_slurped_pages_and_preserves_canonical_names(tmp_path, mocker):
    response = json.dumps(
        [
            [{"name": "Bug"}, {"name": "Needs-Triage"}],
            [{"name": "Help Wanted"}],
        ]
    )
    run = mocker.patch(
        "jailbee.issue_github.subprocess.run", return_value=_completed(stdout=response)
    )

    labels = list_labels(tmp_path, "acme/widgets")

    assert labels == {
        "bug": "Bug",
        "needs-triage": "Needs-Triage",
        "help wanted": "Help Wanted",
    }
    _assert_api_call(
        run,
        tmp_path,
        ["gh", "api", "repos/acme/widgets/labels", "--paginate", "--slurp"],
        None,
    )


@pytest.mark.parametrize("stdout", ["not-json", "[]", '{"login": null}'])
def test_read_paths_reject_unreadable_or_wrongly_shaped_responses(tmp_path, mocker, stdout):
    mocker.patch("jailbee.issue_github.subprocess.run", return_value=_completed(stdout=stdout))

    with pytest.raises(IssueGithubReadError, match="GitHub read failed"):
        current_login(tmp_path)


def test_read_path_reports_a_missing_gh_binary_without_leaking_os_details(tmp_path, mocker):
    token = "github_pat_environment_secret"
    mocker.patch("jailbee.issue_github.subprocess.run", side_effect=FileNotFoundError(token))

    with pytest.raises(IssueGithubReadError) as caught:
        get_issue(tmp_path, "acme/widgets", 42)

    assert token not in str(caught.value)
    assert token not in "".join(traceback.format_exception(caught.value))


def test_read_path_normalizes_nonzero_gh_results_without_echoing_stderr(tmp_path, mocker):
    token = "github_pat_environment_secret"
    mocker.patch(
        "jailbee.issue_github.subprocess.run",
        return_value=_completed(returncode=1, stderr=f"gh: auth failed for {token}"),
    )

    with pytest.raises(IssueGithubReadError) as caught:
        list_labels(tmp_path, "acme/widgets")

    assert "GitHub read failed" in str(caught.value)
    assert token not in str(caught.value)


def test_create_issue_posts_one_json_request_and_returns_the_issue_receipt(tmp_path, mocker):
    response = json.dumps({"number": 73, "html_url": "https://github.com/acme/widgets/issues/73"})
    run = mocker.patch(
        "jailbee.issue_github.subprocess.run", return_value=_completed(stdout=response)
    )

    receipt = create_issue(
        tmp_path,
        "acme/widgets",
        title="Cache bug",
        body="Steps",
        labels=("bug", "needs-triage"),
    )

    assert receipt == MutationReceipt(issue=73, url="https://github.com/acme/widgets/issues/73")
    _assert_api_call(
        run,
        tmp_path,
        ["gh", "api", "repos/acme/widgets/issues", "--method", "POST", "--input", "-"],
        '{"title": "Cache bug", "body": "Steps", "labels": ["bug", "needs-triage"]}',
    )


def test_edit_issue_patches_only_the_requested_fields(tmp_path, mocker):
    response = json.dumps({"number": 42, "html_url": "https://github.com/acme/widgets/issues/42"})
    run = mocker.patch(
        "jailbee.issue_github.subprocess.run", return_value=_completed(stdout=response)
    )

    receipt = edit_issue(tmp_path, "acme/widgets", 42, title=None, body="New steps")

    assert receipt == MutationReceipt(issue=42, url="https://github.com/acme/widgets/issues/42")
    _assert_api_call(
        run,
        tmp_path,
        [
            "gh",
            "api",
            "repos/acme/widgets/issues/42",
            "--method",
            "PATCH",
            "--input",
            "-",
        ],
        '{"body": "New steps"}',
    )


def test_replace_labels_sends_the_complete_post_delta_set_in_one_patch(tmp_path, mocker):
    response = json.dumps({"number": 42, "html_url": "https://github.com/acme/widgets/issues/42"})
    run = mocker.patch(
        "jailbee.issue_github.subprocess.run", return_value=_completed(stdout=response)
    )

    receipt = replace_labels(tmp_path, "acme/widgets", 42, labels=("Bug", "Needs-Triage"))

    assert receipt.issue == 42
    _assert_api_call(
        run,
        tmp_path,
        [
            "gh",
            "api",
            "repos/acme/widgets/issues/42",
            "--method",
            "PATCH",
            "--input",
            "-",
        ],
        '{"labels": ["Bug", "Needs-Triage"]}',
    )


def test_add_comment_posts_body_and_uses_the_target_issue_in_the_receipt(tmp_path, mocker):
    response = json.dumps(
        {"id": 9001, "html_url": "https://github.com/acme/widgets/issues/42#issuecomment-9001"}
    )
    run = mocker.patch(
        "jailbee.issue_github.subprocess.run", return_value=_completed(stdout=response)
    )

    receipt = add_comment(tmp_path, "acme/widgets", 42, body="Confirmed")

    assert receipt == MutationReceipt(
        issue=42,
        url="https://github.com/acme/widgets/issues/42#issuecomment-9001",
    )
    _assert_api_call(
        run,
        tmp_path,
        [
            "gh",
            "api",
            "repos/acme/widgets/issues/42/comments",
            "--method",
            "POST",
            "--input",
            "-",
        ],
        '{"body": "Confirmed"}',
    )


@pytest.mark.parametrize(
    ("state", "reason", "expected_payload"),
    [
        ("open", None, '{"state": "open", "state_reason": "reopened"}'),
        ("closed", "completed", '{"state": "closed", "state_reason": "completed"}'),
        (
            "closed",
            "not_planned",
            '{"state": "closed", "state_reason": "not_planned"}',
        ),
    ],
)
def test_set_state_maps_manifest_reasons_to_github_payloads(
    tmp_path, mocker, state, reason, expected_payload
):
    response = json.dumps({"number": 42, "html_url": "https://github.com/acme/widgets/issues/42"})
    run = mocker.patch(
        "jailbee.issue_github.subprocess.run", return_value=_completed(stdout=response)
    )

    receipt = set_state(tmp_path, "acme/widgets", 42, state=state, reason=reason)

    assert receipt.issue == 42
    _assert_api_call(
        run,
        tmp_path,
        [
            "gh",
            "api",
            "repos/acme/widgets/issues/42",
            "--method",
            "PATCH",
            "--input",
            "-",
        ],
        expected_payload,
    )


@pytest.mark.parametrize(
    ("stderr", "uncertain"),
    [
        ("gh: Validation Failed (HTTP 422)", False),
        ("gh: Internal Server Error (HTTP 500)", True),
        ("connection reset by peer", True),
    ],
)
def test_mutation_nonzero_results_classify_only_http_4xx_as_definite(
    tmp_path, mocker, stderr, uncertain
):
    mocker.patch(
        "jailbee.issue_github.subprocess.run",
        return_value=_completed(returncode=1, stderr=stderr),
    )

    with pytest.raises(IssueGithubMutationError) as caught:
        create_issue(tmp_path, "acme/widgets", title="T", body="B", labels=())

    assert caught.value.uncertain is uncertain


def test_mutation_missing_gh_is_a_definite_pre_dispatch_failure(tmp_path, mocker):
    mocker.patch("jailbee.issue_github.subprocess.run", side_effect=FileNotFoundError("gh"))

    with pytest.raises(IssueGithubMutationError) as caught:
        replace_labels(tmp_path, "acme/widgets", 42, labels=())

    assert caught.value.uncertain is False


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(["gh", "api"], 60),
        OSError("connection lost"),
        KeyboardInterrupt(),
    ],
)
def test_mutation_transport_or_interruption_exceptions_are_uncertain(tmp_path, mocker, failure):
    mocker.patch("jailbee.issue_github.subprocess.run", side_effect=failure)

    with pytest.raises(IssueGithubMutationError) as caught:
        add_comment(tmp_path, "acme/widgets", 42, body="Hello")

    assert caught.value.uncertain is True


def test_mutation_signal_exit_is_uncertain(tmp_path, mocker):
    mocker.patch(
        "jailbee.issue_github.subprocess.run",
        return_value=_completed(
            returncode=-15,
            stderr="terminated while an earlier diagnostic mentioned HTTP 422",
        ),
    )

    with pytest.raises(IssueGithubMutationError) as caught:
        edit_issue(tmp_path, "acme/widgets", 42, title="T", body=None)

    assert caught.value.uncertain is True


def test_mutation_success_with_unreadable_response_is_uncertain(tmp_path, mocker):
    mocker.patch("jailbee.issue_github.subprocess.run", return_value=_completed(stdout="not-json"))

    with pytest.raises(IssueGithubMutationError) as caught:
        set_state(tmp_path, "acme/widgets", 42, state="open", reason=None)

    assert caught.value.uncertain is True


def test_mutation_errors_never_expose_environment_tokens_or_payloads(tmp_path, mocker, monkeypatch):
    token = "github_pat_environment_secret"
    body = "private manifest payload"
    monkeypatch.setenv("GH_TOKEN", token)
    mocker.patch(
        "jailbee.issue_github.subprocess.run",
        return_value=_completed(
            returncode=1,
            stderr=f"gh: unauthorized token={token}; request body={body} (HTTP 401)",
        ),
    )

    with pytest.raises(IssueGithubMutationError) as caught:
        create_issue(tmp_path, "acme/widgets", title="T", body=body, labels=())

    assert caught.value.uncertain is False
    assert token not in str(caught.value)
    assert body not in str(caught.value)
    assert token not in caught.value.args
    assert body not in caught.value.args
