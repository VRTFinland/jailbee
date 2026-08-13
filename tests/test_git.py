"""Tests for git helpers."""

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from jailbee import git
from jailbee.git import (
    detect_default_branch,
    get_branch_tracking,
    get_origin_url,
)


def test_detect_default_branch_main(mocker, tmp_path):
    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.return_value = CompletedProcess(
        args=[], returncode=0, stdout="origin/main\n", stderr=""
    )

    result = detect_default_branch(tmp_path)

    assert result == "main"
    mock_run.assert_called_once()
    call_args = mock_run.call_args
    assert call_args.args[0][:3] == ["git", "symbolic-ref", "--short"]
    assert call_args.kwargs["cwd"] == tmp_path


def test_detect_default_branch_dev(mocker, tmp_path):
    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.return_value = CompletedProcess(
        args=[], returncode=0, stdout="origin/dev\n", stderr=""
    )

    assert detect_default_branch(tmp_path) == "dev"


def test_detect_default_branch_fallback_on_nonzero(mocker, tmp_path):
    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.return_value = CompletedProcess(args=[], returncode=1, stdout="", stderr="fatal: ...")

    assert detect_default_branch(tmp_path) == "main"


def test_detect_default_branch_fallback_on_oserror(mocker, tmp_path):
    mocker.patch(
        "jailbee.git.subprocess.run",
        side_effect=FileNotFoundError("git not found"),
    )

    assert detect_default_branch(tmp_path) == "main"


def test_detect_default_branch_strips_origin_prefix(mocker, tmp_path):
    """`git symbolic-ref --short refs/remotes/origin/HEAD` outputs `origin/main`."""
    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.return_value = CompletedProcess(
        args=[], returncode=0, stdout="origin/feature/x\n", stderr=""
    )

    # Strip leading "origin/" prefix only
    assert detect_default_branch(tmp_path) == "feature/x"


def test_detect_default_branch_unexpected_output(mocker, tmp_path):
    """If output doesn't start with 'origin/', fall back."""
    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.return_value = CompletedProcess(
        args=[], returncode=0, stdout="weird-output\n", stderr=""
    )

    assert detect_default_branch(tmp_path) == "main"


def test_get_origin_url_returns_url(mocker, tmp_path):
    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.return_value = CompletedProcess(
        args=[],
        returncode=0,
        stdout="git@github.com:example/example.git\n",
        stderr="",
    )

    assert get_origin_url(tmp_path) == "git@github.com:example/example.git"
    call_args = mock_run.call_args
    assert call_args.args[0] == ["git", "remote", "get-url", "origin"]
    assert call_args.kwargs["cwd"] == tmp_path


def test_get_origin_url_no_origin(mocker, tmp_path):
    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.return_value = CompletedProcess(
        args=[],
        returncode=2,
        stdout="",
        stderr="error: No such remote 'origin'\n",
    )

    assert get_origin_url(tmp_path) is None


def test_get_origin_url_empty_output(mocker, tmp_path):
    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.return_value = CompletedProcess(args=[], returncode=0, stdout="\n", stderr="")

    assert get_origin_url(tmp_path) is None


def test_get_origin_url_no_git_binary(mocker, tmp_path):
    mocker.patch(
        "jailbee.git.subprocess.run",
        side_effect=FileNotFoundError("git not found"),
    )

    assert get_origin_url(tmp_path) is None


def test_get_branch_tracking_configured(mocker, tmp_path):
    """branch.<br>.remote and branch.<br>.merge both set → return both."""
    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.side_effect = [
        CompletedProcess(args=[], returncode=0, stdout="origin\n", stderr=""),
        CompletedProcess(args=[], returncode=0, stdout="refs/heads/main\n", stderr=""),
    ]

    result = get_branch_tracking(tmp_path, "main")

    assert result == ("origin", "refs/heads/main")
    assert mock_run.call_count == 2
    assert mock_run.call_args_list[0].args[0] == [
        "git",
        "config",
        "--get",
        "branch.main.remote",
    ]
    assert mock_run.call_args_list[1].args[0] == [
        "git",
        "config",
        "--get",
        "branch.main.merge",
    ]


def test_get_branch_tracking_no_remote_set(mocker, tmp_path):
    """branch exists but no upstream → both config keys absent → None."""
    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.return_value = CompletedProcess(args=[], returncode=1, stdout="", stderr="")

    assert get_branch_tracking(tmp_path, "feature/new") is None


def test_get_branch_tracking_partial_config(mocker, tmp_path):
    """remote set but merge missing → treat as unconfigured."""
    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.side_effect = [
        CompletedProcess(args=[], returncode=0, stdout="origin\n", stderr=""),
        CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
    ]

    assert get_branch_tracking(tmp_path, "main") is None


def test_get_branch_tracking_no_git_binary(mocker, tmp_path):
    mocker.patch(
        "jailbee.git.subprocess.run",
        side_effect=FileNotFoundError("git not found"),
    )

    assert get_branch_tracking(tmp_path, "main") is None


def test_branch_exists_in_source_local_ref(mocker, tmp_path):
    from jailbee.git import branch_exists_in_source

    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.return_value = CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    assert branch_exists_in_source(tmp_path, "feat/x") is True
    # First call probes refs/heads/<branch>
    first_call_args = mock_run.call_args_list[0].args[0]
    assert first_call_args[:5] == ["git", "show-ref", "--verify", "--quiet", "refs/heads/feat/x"]
    assert mock_run.call_args_list[0].kwargs["cwd"] == tmp_path


def test_branch_exists_in_source_remote_ref(mocker, tmp_path):
    from jailbee.git import branch_exists_in_source

    # heads/<branch> fails (rc=1), remotes/origin/<branch> succeeds (rc=0)
    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.side_effect = [
        CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
        CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    ]

    assert branch_exists_in_source(tmp_path, "feat/x") is True
    assert mock_run.call_count == 2
    second_call_args = mock_run.call_args_list[1].args[0]
    assert second_call_args[4] == "refs/remotes/origin/feat/x"


def test_branch_exists_in_source_missing(mocker, tmp_path):
    from jailbee.git import branch_exists_in_source

    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.return_value = CompletedProcess(args=[], returncode=1, stdout="", stderr="")

    assert branch_exists_in_source(tmp_path, "feat/missing") is False
    assert mock_run.call_count == 2  # tried both heads and remotes


def test_branch_exists_in_source_no_git_binary(mocker, tmp_path):
    from jailbee.git import branch_exists_in_source

    mocker.patch(
        "jailbee.git.subprocess.run",
        side_effect=FileNotFoundError("git not found"),
    )

    assert branch_exists_in_source(tmp_path, "feat/x") is False


def test_branch_exists_locally_present(mocker, tmp_path):
    """refs/heads/<branch> exists → True; refs/remotes/origin/ is not consulted."""
    from jailbee.git import branch_exists_locally

    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.return_value = CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    assert branch_exists_locally(tmp_path, "feat/x") is True
    assert mock_run.call_count == 1
    call_args = mock_run.call_args_list[0].args[0]
    assert call_args == [
        "git",
        "show-ref",
        "--verify",
        "--quiet",
        "refs/heads/feat/x",
    ]
    assert mock_run.call_args_list[0].kwargs["cwd"] == tmp_path


def test_branch_exists_locally_origin_only_returns_false(mocker, tmp_path):
    """refs/heads missing → False even if refs/remotes/origin/<branch> exists."""
    from jailbee.git import branch_exists_locally

    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.return_value = CompletedProcess(args=[], returncode=1, stdout="", stderr="")

    assert branch_exists_locally(tmp_path, "feat/x") is False
    assert mock_run.call_count == 1


def test_branch_exists_locally_no_git_binary(mocker, tmp_path):
    from jailbee.git import branch_exists_locally

    mocker.patch(
        "jailbee.git.subprocess.run",
        side_effect=FileNotFoundError("git not found"),
    )

    assert branch_exists_locally(tmp_path, "feat/x") is False


def test_fetch_origin_ref_invokes_git_fetch(mocker, tmp_path):
    from jailbee.git import fetch_origin_ref

    mock_run = mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )

    fetch_origin_ref(tmp_path, "main")

    mock_run.assert_called_once()
    call = mock_run.call_args
    assert call.args[0] == ["git", "fetch", "origin", "main"]
    assert call.kwargs["cwd"] == tmp_path


def test_fetch_origin_ref_raises_on_nonzero(mocker, tmp_path):
    from jailbee.git import GitFetchError, fetch_origin_ref

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(args=[], returncode=1, stdout="", stderr="fatal: refusing"),
    )

    with pytest.raises(GitFetchError) as exc:
        fetch_origin_ref(tmp_path, "main")
    assert "fatal: refusing" in exc.value.stderr


def test_fetch_origin_ref_raises_when_git_missing(mocker, tmp_path):
    from jailbee.git import GitFetchError, fetch_origin_ref

    mocker.patch(
        "jailbee.git.subprocess.run",
        side_effect=FileNotFoundError("git not found"),
    )

    with pytest.raises(GitFetchError, match="not on PATH"):
        fetch_origin_ref(tmp_path, "main")


def test_rev_parse_origin_returns_sha(mocker, tmp_path):
    from jailbee.git import rev_parse_origin

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(args=[], returncode=0, stdout="abc123def456\n", stderr=""),
    )
    assert rev_parse_origin(tmp_path, "main") == "abc123def456"


def test_rev_parse_origin_returns_none_when_missing(mocker, tmp_path):
    from jailbee.git import rev_parse_origin

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
    )
    assert rev_parse_origin(tmp_path, "main") is None


def test_rev_parse_origin_returns_none_when_git_missing(mocker, tmp_path):
    from jailbee.git import rev_parse_origin

    mocker.patch(
        "jailbee.git.subprocess.run",
        side_effect=FileNotFoundError("git not found"),
    )
    assert rev_parse_origin(tmp_path, "main") is None


def test_show_file_at_ref_returns_content(mocker, tmp_path):
    from jailbee.git import show_file_at_ref

    run = mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=mocker.Mock(returncode=0, stdout="autostart:\n  on_create: []\n", stderr=""),
    )
    result = show_file_at_ref(tmp_path, "refs/heads/feature", ".gie/config.yaml")

    assert result == "autostart:\n  on_create: []\n"
    args = run.call_args[0][0]
    assert args == ["git", "show", "refs/heads/feature:.gie/config.yaml"]
    assert run.call_args[1]["cwd"] == tmp_path
    assert run.call_args[1]["check"] is False


def test_show_file_at_ref_returns_none_when_missing(mocker, tmp_path):
    from jailbee.git import show_file_at_ref

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=mocker.Mock(returncode=128, stdout="", stderr="path does not exist"),
    )
    assert show_file_at_ref(tmp_path, "refs/heads/feature", ".gie/config.yaml") is None


def test_show_file_at_ref_returns_none_when_git_absent(mocker, tmp_path):
    from jailbee.git import show_file_at_ref

    mocker.patch("jailbee.git.subprocess.run", side_effect=FileNotFoundError)
    assert show_file_at_ref(tmp_path, "refs/heads/feature", ".gie/config.yaml") is None


def test_show_file_at_ref_empty_file_is_empty_string_not_none(mocker, tmp_path):
    """An empty committed config is a real state, distinct from "no such file"."""
    from jailbee.git import show_file_at_ref

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=mocker.Mock(returncode=0, stdout="", stderr=""),
    )
    assert show_file_at_ref(tmp_path, "refs/heads/feature", ".gie/config.yaml") == ""


def test_get_current_branch_returns_branch_name(mocker, tmp_path):
    from jailbee.git import get_current_branch

    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.return_value = CompletedProcess(args=[], returncode=0, stdout="feat/foo\n", stderr="")

    assert get_current_branch(tmp_path) == "feat/foo"
    call_args = mock_run.call_args
    assert call_args.args[0] == ["git", "symbolic-ref", "--short", "HEAD"]
    assert call_args.kwargs["cwd"] == tmp_path


def test_get_current_branch_returns_none_when_detached_head(mocker, tmp_path):
    """Detached HEAD → `git symbolic-ref` exits non-zero → None."""
    from jailbee.git import get_current_branch

    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.return_value = CompletedProcess(
        args=[],
        returncode=128,
        stdout="",
        stderr="fatal: ref HEAD is not a symbolic ref\n",
    )

    assert get_current_branch(tmp_path) is None


def test_get_current_branch_returns_none_when_no_git_binary(mocker, tmp_path):
    from jailbee.git import get_current_branch

    mocker.patch(
        "jailbee.git.subprocess.run",
        side_effect=FileNotFoundError("git not found"),
    )

    assert get_current_branch(tmp_path) is None


def test_fetch_url_invokes_git_fetch_with_refspec(mocker, tmp_path):
    from jailbee.git import fetch_url

    mock_call = mocker.patch("jailbee.git.subprocess.call", return_value=0)

    fetch_url(tmp_path, "ext::echo hello", "+refs/heads/feat/foo:refs/jailbee/feat-foo/feat/foo")

    mock_call.assert_called_once()
    call = mock_call.call_args
    assert call.args[0] == [
        "git",
        "-c",
        "protocol.ext.allow=always",
        "fetch",
        "--no-recurse-submodules",
        "ext::echo hello",
        "+refs/heads/feat/foo:refs/jailbee/feat-foo/feat/foo",
    ]
    assert call.kwargs["cwd"] == tmp_path


def test_fetch_url_raises_on_nonzero(mocker, tmp_path):
    from jailbee.git import GitError, fetch_url

    mocker.patch("jailbee.git.subprocess.call", return_value=128)

    with pytest.raises(GitError, match=r"git fetch failed \(exit 128\)"):
        fetch_url(tmp_path, "ext::false", "+refs/heads/x:refs/y/x")


def test_rev_parse_returns_oid(mocker, tmp_path):
    from jailbee.git import rev_parse

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(
            args=[], returncode=0, stdout="abcdef1234567890\n", stderr=""
        ),
    )
    assert rev_parse(tmp_path, "refs/jailbee/feat-foo/feat/foo") == "abcdef1234567890"


def test_rev_parse_returns_none_when_ref_missing(mocker, tmp_path):
    from jailbee.git import rev_parse

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(
            args=[], returncode=128, stdout="", stderr="fatal: ambiguous"
        ),
    )
    assert rev_parse(tmp_path, "refs/missing/x") is None


def test_list_refs_returns_refs_under_prefix(mocker, tmp_path):
    from jailbee.git import list_refs

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(
            args=[],
            returncode=0,
            stdout=("refs/jailbee/feat-foo/feat/foo\nrefs/jailbee/feat-foo/feat/bar\n"),
            stderr="",
        ),
    )
    assert list_refs(tmp_path, "refs/jailbee/feat-foo/") == [
        "refs/jailbee/feat-foo/feat/foo",
        "refs/jailbee/feat-foo/feat/bar",
    ]


def test_list_refs_returns_empty_on_no_matches(mocker, tmp_path):
    from jailbee.git import list_refs

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )
    assert list_refs(tmp_path, "refs/jailbee/none/") == []


def test_list_refs_returns_empty_on_nonzero(mocker, tmp_path):
    from jailbee.git import list_refs

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(args=[], returncode=1, stdout="", stderr="err"),
    )
    assert list_refs(tmp_path, "refs/jailbee/none/") == []


def test_delete_ref_invokes_update_ref_d(mocker, tmp_path):
    from jailbee.git import delete_ref

    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.return_value = CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    delete_ref(tmp_path, "refs/jailbee/feat-foo/feat/foo")
    assert mock_run.call_args.args[0] == [
        "git",
        "update-ref",
        "-d",
        "refs/jailbee/feat-foo/feat/foo",
    ]


def test_delete_ref_swallows_failure(mocker, tmp_path):
    """delete_ref must never raise — cleanup is best-effort."""
    from jailbee.git import delete_ref

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(args=[], returncode=1, stdout="", stderr="err"),
    )
    delete_ref(tmp_path, "refs/jailbee/x/y")


def test_log_oneline_returns_lines(mocker, tmp_path):
    from jailbee.git import log_oneline

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(
            args=[],
            returncode=0,
            stdout="abc1234 Fix\ndef5678 Add tests\n",
            stderr="",
        ),
    )
    assert log_oneline(tmp_path, "abc..def") == [
        "abc1234 Fix",
        "def5678 Add tests",
    ]


def test_log_oneline_returns_empty_on_nonzero(mocker, tmp_path):
    from jailbee.git import log_oneline

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(args=[], returncode=128, stdout="", stderr="bad"),
    )
    assert log_oneline(tmp_path, "x..y") == []


def test_local_branch_exists_true(mocker, tmp_path):
    from jailbee.git import local_branch_exists

    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.return_value = CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    assert local_branch_exists(tmp_path, "feat/foo") is True
    assert mock_run.call_args.args[0] == [
        "git",
        "show-ref",
        "--verify",
        "--quiet",
        "refs/heads/feat/foo",
    ]


def test_local_branch_exists_false(mocker, tmp_path):
    from jailbee.git import local_branch_exists

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
    )
    assert local_branch_exists(tmp_path, "feat/missing") is False


def test_remote_ref_exists_true(mocker, tmp_path):
    from jailbee.git import remote_ref_exists

    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.return_value = CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    assert remote_ref_exists(tmp_path, "origin", "feat/foo") is True
    assert mock_run.call_args.args[0] == [
        "git",
        "show-ref",
        "--verify",
        "--quiet",
        "refs/remotes/origin/feat/foo",
    ]


def test_remote_ref_exists_false(mocker, tmp_path):
    from jailbee.git import remote_ref_exists

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
    )
    assert remote_ref_exists(tmp_path, "origin", "feat/missing") is False


def test_is_merged_into_true(mocker, tmp_path):
    from jailbee.git import is_merged_into

    mock_run = mocker.patch("jailbee.git.subprocess.run")
    mock_run.return_value = CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    assert is_merged_into(tmp_path, "feat/foo", "HEAD") is True
    assert mock_run.call_args.args[0] == [
        "git",
        "merge-base",
        "--is-ancestor",
        "refs/heads/feat/foo",
        "HEAD",
    ]


def test_is_merged_into_false(mocker, tmp_path):
    from jailbee.git import is_merged_into

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
    )
    assert is_merged_into(tmp_path, "feat/foo", "HEAD") is False


def test_is_merged_into_handles_oserror(mocker, tmp_path):
    from jailbee.git import is_merged_into

    mocker.patch("jailbee.git.subprocess.run", side_effect=FileNotFoundError("git"))

    assert is_merged_into(tmp_path, "feat/foo", "HEAD") is False


def test_create_branch_without_tracking(mocker, tmp_path):
    from jailbee.git import create_branch

    mock_call = mocker.patch("jailbee.git.subprocess.call", return_value=0)

    create_branch(tmp_path, "feat/foo", start_point="refs/jailbee/feat-foo/feat/foo", track=None)

    mock_call.assert_called_once()
    call = mock_call.call_args
    assert call.args[0] == [
        "git",
        "checkout",
        "-b",
        "feat/foo",
        "refs/jailbee/feat-foo/feat/foo",
    ]
    assert call.kwargs["cwd"] == tmp_path


def test_create_branch_with_tracking_sets_upstream_after(mocker, tmp_path):
    from jailbee.git import create_branch

    mock_call = mocker.patch("jailbee.git.subprocess.call", return_value=0)

    create_branch(
        tmp_path,
        "feat/foo",
        start_point="refs/jailbee/feat-foo/feat/foo",
        track="origin/feat/foo",
    )

    assert mock_call.call_count == 2
    assert mock_call.call_args_list[0].args[0] == [
        "git",
        "checkout",
        "-b",
        "feat/foo",
        "refs/jailbee/feat-foo/feat/foo",
    ]
    assert mock_call.call_args_list[1].args[0] == [
        "git",
        "branch",
        "--set-upstream-to=origin/feat/foo",
        "feat/foo",
    ]


def test_create_branch_raises_on_checkout_failure(mocker, tmp_path):
    from jailbee.git import GitError, create_branch

    mocker.patch("jailbee.git.subprocess.call", return_value=1)

    with pytest.raises(GitError, match=r"git checkout -b failed \(exit 1\)"):
        create_branch(tmp_path, "feat/foo", start_point="x", track=None)


def test_create_branch_raises_on_set_upstream_failure(mocker, tmp_path):
    from jailbee.git import GitError, create_branch

    mocker.patch("jailbee.git.subprocess.call", side_effect=[0, 1])

    with pytest.raises(GitError, match=r"git branch --set-upstream-to failed \(exit 1\)"):
        create_branch(tmp_path, "feat/foo", start_point="x", track="origin/feat/foo")


def test_checkout_branch_invokes_git_checkout(mocker, tmp_path):
    from jailbee.git import checkout_branch

    mock_call = mocker.patch("jailbee.git.subprocess.call", return_value=0)

    checkout_branch(tmp_path, "feat/foo")

    mock_call.assert_called_once()
    call = mock_call.call_args
    assert call.args[0] == ["git", "checkout", "feat/foo"]
    assert call.kwargs["cwd"] == tmp_path


def test_checkout_branch_raises_on_failure(mocker, tmp_path):
    from jailbee.git import GitError, checkout_branch

    mocker.patch("jailbee.git.subprocess.call", return_value=1)

    with pytest.raises(GitError, match=r"git checkout failed \(exit 1\)"):
        checkout_branch(tmp_path, "feat/foo")


def test_merge_ref_ff_only(mocker, tmp_path):
    from jailbee.git import merge_ref

    mock_call = mocker.patch("jailbee.git.subprocess.call", return_value=0)

    merge_ref(tmp_path, "refs/jailbee/feat-foo/feat/foo", message=None, no_ff=False, ff_only=True)

    call = mock_call.call_args
    assert call.args[0] == ["git", "merge", "--ff-only", "refs/jailbee/feat-foo/feat/foo"]
    assert call.kwargs["cwd"] == tmp_path


def test_merge_ref_no_ff_with_message(mocker, tmp_path):
    from jailbee.git import merge_ref

    mock_call = mocker.patch("jailbee.git.subprocess.call", return_value=0)

    merge_ref(
        tmp_path,
        "refs/jailbee/feat-foo/feat/foo",
        message="Merge branch 'feat/foo' from container feat-foo",
        no_ff=True,
        ff_only=False,
    )

    assert mock_call.call_args.args[0] == [
        "git",
        "merge",
        "--no-ff",
        "-m",
        "Merge branch 'feat/foo' from container feat-foo",
        "refs/jailbee/feat-foo/feat/foo",
    ]


def test_merge_ref_raises_on_failure(mocker, tmp_path):
    from jailbee.git import GitError, merge_ref

    mocker.patch("jailbee.git.subprocess.call", return_value=1)

    with pytest.raises(GitError, match=r"git merge failed \(exit 1\)"):
        merge_ref(tmp_path, "x", message=None, no_ff=False, ff_only=False)


def test_delete_branch_invokes_git_branch_d(mocker, tmp_path):
    from jailbee.git import delete_branch

    mock_call = mocker.patch("jailbee.git.subprocess.call", return_value=0)

    delete_branch(tmp_path, "feat/foo")

    call = mock_call.call_args
    assert call.args[0] == ["git", "branch", "-d", "feat/foo"]
    assert call.kwargs["cwd"] == tmp_path


def test_delete_branch_raises_on_failure(mocker, tmp_path):
    from jailbee.git import GitError, delete_branch

    mocker.patch("jailbee.git.subprocess.call", return_value=1)

    with pytest.raises(GitError, match=r"git branch -d failed \(exit 1\)"):
        delete_branch(tmp_path, "feat/foo")


def test_push_url_invokes_git_with_protocol_ext_allowed(mocker, tmp_path):
    mock_call = mocker.patch("jailbee.git.subprocess.call", return_value=0)
    git.push_url(tmp_path, "ext::echo dummy", "+refs/heads/main:refs/jailbee/host/main")

    mock_call.assert_called_once()
    args, kwargs = mock_call.call_args
    assert args[0] == [
        "git",
        "-c",
        "protocol.ext.allow=always",
        "push",
        "--no-recurse-submodules",
        "ext::echo dummy",
        "+refs/heads/main:refs/jailbee/host/main",
    ]
    assert kwargs == {"cwd": tmp_path}


def test_push_url_raises_git_error_on_non_zero_exit(mocker, tmp_path):
    mocker.patch("jailbee.git.subprocess.call", return_value=1)
    with pytest.raises(git.GitError) as excinfo:
        git.push_url(tmp_path, "ext::echo dummy", "+refs/heads/main:refs/jailbee/host/main")
    assert "exit 1" in str(excinfo.value)


def test_fast_forward_branch_returns_true_on_ff(mocker):
    from jailbee import git

    run = mocker.patch("jailbee.git.subprocess.run")
    run.return_value = mocker.MagicMock(returncode=0)
    ok = git.fast_forward_branch(Path("/repo"), "dev", "refs/jailbee/feat-x/feat/x")
    assert ok is True
    # the fetch refspec maps the source ref onto refs/heads/dev (no leading '+')
    called = run.call_args.args[0]
    assert called[:3] == ["git", "fetch", "."]
    assert "refs/jailbee/feat-x/feat/x:refs/heads/dev" in called


def test_fast_forward_branch_returns_false_on_non_ff(mocker):
    from jailbee import git

    run = mocker.patch("jailbee.git.subprocess.run")
    run.return_value = mocker.MagicMock(returncode=1)
    assert git.fast_forward_branch(Path("/repo"), "dev", "refs/x") is False


def test_host_tree_dirty_true_when_status_nonempty(mocker):
    from jailbee import git

    run = mocker.patch("jailbee.git.subprocess.run")
    run.return_value = mocker.MagicMock(returncode=0, stdout=" M file\n")
    assert git.host_tree_dirty(Path("/repo")) is True


def test_host_tree_dirty_false_when_clean(mocker):
    from jailbee import git

    run = mocker.patch("jailbee.git.subprocess.run")
    run.return_value = mocker.MagicMock(returncode=0, stdout="")
    assert git.host_tree_dirty(Path("/repo")) is False


def test_submodule_update_invokes_recursive_with_file_protocol(mocker, tmp_path):
    from jailbee import git

    call = mocker.patch("jailbee.git.subprocess.call", return_value=0)
    git.submodule_update(tmp_path)

    args = call.call_args.args[0]
    assert "submodule" in args and "update" in args
    assert "--init" in args and "--recursive" in args
    assert "protocol.file.allow=always" in args


def test_submodule_update_raises_on_failure(mocker, tmp_path):
    from jailbee import git

    mocker.patch("jailbee.git.subprocess.call", return_value=1)
    with pytest.raises(git.GitError):
        git.submodule_update(tmp_path)


def test_fetch_url_multi_passes_all_refspecs(mocker, tmp_path):
    from jailbee import git

    call = mocker.patch("jailbee.git.subprocess.call", return_value=0)
    git.fetch_url_multi(tmp_path, "ext::url", ["+HEAD:refs/x/HEAD", "+refs/heads/*:refs/x/heads/*"])
    args = call.call_args.args[0]
    assert args[:6] == [
        "git",
        "-c",
        "protocol.ext.allow=always",
        "fetch",
        "--no-recurse-submodules",
        "ext::url",
    ]
    assert args[6:] == ["+HEAD:refs/x/HEAD", "+refs/heads/*:refs/x/heads/*"]


def test_push_url_multi_raises_on_failure(mocker, tmp_path):
    from jailbee import git

    mocker.patch("jailbee.git.subprocess.call", return_value=1)
    with pytest.raises(git.GitError):
        git.push_url_multi(tmp_path, "ext::url", ["+HEAD:refs/y/HEAD"])


def test_clone_url_invokes_clone_with_ext_protocol(mocker, tmp_path):
    from jailbee import git

    call = mocker.patch("jailbee.git.subprocess.call", return_value=0)
    git.clone_url("ext::url", tmp_path / "sub")
    args = call.call_args.args[0]
    assert args[:5] == [
        "git",
        "-c",
        "protocol.ext.allow=always",
        "clone",
        "--no-recurse-submodules",
    ]
    assert args[5] == "ext::url"
    assert args[6] == str(tmp_path / "sub")


def test_submodule_status_paths_parses_recursive_output(mocker, tmp_path):
    from jailbee import git

    out = " 1111111 lib (v1)\n-2222222 lib/nested\n+3333333 other (heads/x)\n"
    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=mocker.Mock(returncode=0, stdout=out),
    )
    assert git.submodule_status_paths(tmp_path) == ["lib", "lib/nested", "other"]


def test_submodule_status_paths_empty_when_no_submodules(mocker, tmp_path):
    from jailbee import git

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=mocker.Mock(returncode=0, stdout=""),
    )
    assert git.submodule_status_paths(tmp_path) == []


def test_run_capture_success_returns_stdout(mocker):
    fake = mocker.Mock(returncode=0, stdout="deadbeef\n")
    run = mocker.patch("jailbee.git.subprocess.run", return_value=fake)

    ok, out = git.run_capture("/some/repo", ["rev-parse", "HEAD"])

    assert ok is True
    assert out == "deadbeef\n"
    run.assert_called_once_with(
        ["git", "rev-parse", "HEAD"],
        cwd="/some/repo",
        capture_output=True,
        text=True,
        check=False,
    )


def test_run_capture_nonzero_returns_false(mocker):
    fake = mocker.Mock(returncode=1, stdout="")
    mocker.patch("jailbee.git.subprocess.run", return_value=fake)

    assert git.run_capture("/some/repo", ["merge-base", "--is-ancestor", "a", "b"]) == (False, "")


def test_run_capture_oserror_returns_false(mocker):
    mocker.patch("jailbee.git.subprocess.run", side_effect=FileNotFoundError)

    assert git.run_capture("/missing", ["status", "--porcelain"]) == (False, "")


# ---------------------------------------------------------------------------
# push_to_origin
# ---------------------------------------------------------------------------


def test_push_to_origin_builds_refspec_and_inherits_output(mocker, tmp_path):
    from jailbee.git import push_to_origin

    call = mocker.patch("jailbee.git.subprocess.call", return_value=0)

    push_to_origin(tmp_path, "refs/jailbee/feat-foo/feat/foo", "feat/foo")

    call.assert_called_once_with(
        ["git", "push", "origin", "refs/jailbee/feat-foo/feat/foo:refs/heads/feat/foo"],
        cwd=tmp_path,
    )


def test_push_to_origin_never_passes_force(mocker, tmp_path):
    from jailbee.git import push_to_origin

    call = mocker.patch("jailbee.git.subprocess.call", return_value=0)

    push_to_origin(tmp_path, "refs/jailbee/x/main", "main")

    args = call.call_args.args[0]
    assert "--force" not in args
    assert "--force-with-lease" not in args
    assert not any(spec.startswith("+") for spec in args)


def test_push_to_origin_raises_git_error_on_failure(mocker, tmp_path):
    from jailbee.git import GitError, push_to_origin

    mocker.patch("jailbee.git.subprocess.call", return_value=1)

    with pytest.raises(GitError, match="git push failed"):
        push_to_origin(tmp_path, "refs/jailbee/x/feat/y", "feat/y")


def test_check_ref_format_valid(mocker):
    from jailbee.git import check_ref_format

    run = mocker.patch("jailbee.git.subprocess.run")
    run.return_value = CompletedProcess([], 0, "", "")
    assert check_ref_format("feat/foo") is True
    assert run.call_args.args[0] == ["git", "check-ref-format", "--branch", "feat/foo"]


def test_check_ref_format_invalid(mocker):
    from jailbee.git import check_ref_format

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess([], 1, "", "bad"),
    )
    assert check_ref_format("bad..name") is False


def test_remote_branch_sha_returns_sha(mocker, tmp_path):
    from jailbee.git import remote_branch_sha

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess([], 0, "deadbeef\trefs/heads/feat/foo\n", ""),
    )
    assert remote_branch_sha(tmp_path, "origin", "feat/foo") == "deadbeef"


def test_remote_branch_sha_none_when_absent(mocker, tmp_path):
    from jailbee.git import remote_branch_sha

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess([], 0, "", ""),
    )
    assert remote_branch_sha(tmp_path, "origin", "nope") is None


def test_rename_branch_builds_command(mocker, tmp_path):
    from jailbee.git import rename_branch

    call = mocker.patch("jailbee.git.subprocess.call", return_value=0)
    rename_branch(tmp_path, "dev-1", "user/nice")
    call.assert_called_once_with(["git", "branch", "-m", "dev-1", "user/nice"], cwd=tmp_path)


def test_rename_branch_raises_on_failure(mocker, tmp_path):
    import pytest

    from jailbee.git import GitError, rename_branch

    mocker.patch("jailbee.git.subprocess.call", return_value=1)
    with pytest.raises(GitError, match="git branch -m failed"):
        rename_branch(tmp_path, "a", "b")


def test_set_upstream_builds_command(mocker, tmp_path):
    from jailbee.git import set_upstream

    call = mocker.patch("jailbee.git.subprocess.call", return_value=0)
    set_upstream(tmp_path, "user/nice", "origin/user/nice")
    call.assert_called_once_with(
        ["git", "branch", "--set-upstream-to=origin/user/nice", "user/nice"], cwd=tmp_path
    )


def test_push_to_origin_force_with_lease(mocker, tmp_path):
    from jailbee.git import push_to_origin

    call = mocker.patch("jailbee.git.subprocess.call", return_value=0)
    push_to_origin(tmp_path, "refs/jailbee/x/dev-1", "user/nice", force_with_lease="deadbeef")
    call.assert_called_once_with(
        [
            "git",
            "push",
            "--force-with-lease=refs/heads/user/nice:deadbeef",
            "origin",
            "refs/jailbee/x/dev-1:refs/heads/user/nice",
        ],
        cwd=tmp_path,
    )


# ---------------------------------------------------------------------------
# commit_subject
# ---------------------------------------------------------------------------


def test_commit_subject_returns_subject_line(mocker, tmp_path):
    from jailbee.git import commit_subject

    run = mocker.patch(
        "jailbee.git.run_capture",
        return_value=(True, "feat: add the thing\n"),
    )

    assert commit_subject(tmp_path, "refs/jailbee/feat-foo/feat/foo") == "feat: add the thing"
    run.assert_called_once_with(
        str(tmp_path), ["log", "-1", "--format=%s", "refs/jailbee/feat-foo/feat/foo"]
    )


def test_commit_subject_returns_none_on_failure(mocker, tmp_path):
    from jailbee.git import commit_subject

    mocker.patch("jailbee.git.run_capture", return_value=(False, ""))

    assert commit_subject(tmp_path, "refs/jailbee/x/y") is None


def test_commit_subject_returns_none_on_empty_output(mocker, tmp_path):
    from jailbee.git import commit_subject

    mocker.patch("jailbee.git.run_capture", return_value=(True, "\n"))

    assert commit_subject(tmp_path, "refs/jailbee/x/y") is None


def test_list_branches_returns_short_names(mocker, tmp_path):
    from jailbee.git import list_branches

    mock_run = mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(
            args=[],
            returncode=0,
            stdout="main\nfeat/foo\nbugfix/bar\n",
            stderr="",
        ),
    )
    assert list_branches(tmp_path) == ["main", "feat/foo", "bugfix/bar"]
    assert mock_run.call_args.args[0] == [
        "git",
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/",
    ]


def test_list_branches_returns_empty_on_nonzero(mocker, tmp_path):
    from jailbee.git import list_branches

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(args=[], returncode=128, stdout="", stderr="not a repo"),
    )
    assert list_branches(tmp_path) == []


def test_list_branches_returns_empty_when_git_missing(mocker, tmp_path):
    """Shell completion calls this; a missing git binary must not raise."""
    from jailbee.git import list_branches

    mocker.patch(
        "jailbee.git.subprocess.run",
        side_effect=FileNotFoundError("git"),
    )
    assert list_branches(tmp_path) == []


# ---------------------------------------------------------------------------
# get_head_sha
# ---------------------------------------------------------------------------


def test_get_head_sha_returns_the_sha(tmp_path, mocker):
    from jailbee.git import get_head_sha

    run = mocker.patch("jailbee.git.subprocess.run")
    run.return_value = mocker.Mock(returncode=0, stdout="abc123\n", stderr="")

    assert get_head_sha(tmp_path) == "abc123"
    assert run.call_args.args[0] == ["git", "rev-parse", "HEAD"]


def test_get_head_sha_returns_none_on_failure(tmp_path, mocker):
    """A repo with no commits yet exits non-zero — not an error, just no head."""
    from jailbee.git import get_head_sha

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=mocker.Mock(returncode=128, stdout="", stderr="fatal: ..."),
    )

    assert get_head_sha(tmp_path) is None


def test_get_head_sha_returns_none_when_git_is_missing(tmp_path, mocker):
    from jailbee.git import get_head_sha

    mocker.patch("jailbee.git.subprocess.run", side_effect=FileNotFoundError)

    assert get_head_sha(tmp_path) is None


# ---------------------------------------------------------------------------
# has_commit
# ---------------------------------------------------------------------------


def test_has_commit_true_when_cat_file_succeeds(tmp_path, mocker):
    from jailbee.git import has_commit

    run = mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=mocker.Mock(returncode=0, stdout="", stderr=""),
    )

    assert has_commit(tmp_path, "abc123") is True
    assert run.call_args.args[0] == ["git", "cat-file", "-e", "abc123^{commit}"]


def test_has_commit_false_when_object_is_absent(tmp_path, mocker):
    from jailbee.git import has_commit

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=mocker.Mock(returncode=1, stdout="", stderr=""),
    )

    assert has_commit(tmp_path, "abc123") is False


def test_has_commit_false_for_an_empty_sha_without_running_git(tmp_path, mocker):
    """An unknown head is not a git question — never shell out for it."""
    from jailbee.git import has_commit

    run = mocker.patch("jailbee.git.subprocess.run")

    assert has_commit(tmp_path, "") is False
    run.assert_not_called()


# ---------------------------------------------------------------------------
# diff_shortstat_between
# ---------------------------------------------------------------------------


def test_diff_shortstat_between_returns_stdout(tmp_path, mocker):
    from jailbee.git import diff_shortstat_between

    run = mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=mocker.Mock(
            returncode=0, stdout=" 2 files changed, 12 insertions(+), 3 deletions(-)\n", stderr=""
        ),
    )

    out = diff_shortstat_between(tmp_path, "HEAD", "abc123")

    assert out is not None
    assert "12 insertions" in out
    assert run.call_args.args[0] == [
        "git",
        "diff",
        "--shortstat",
        "--ignore-submodules=dirty",
        "HEAD...abc123",
    ]


def test_diff_shortstat_between_returns_empty_string_when_clean(tmp_path, mocker):
    """Empty stdout means 'no changes', which must not be confused with None
    ('could not compute') — the two render as `clean` and `?` respectively."""
    from jailbee.git import diff_shortstat_between

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=mocker.Mock(returncode=0, stdout="", stderr=""),
    )

    assert diff_shortstat_between(tmp_path, "HEAD", "abc123") == ""


def test_diff_shortstat_between_returns_none_on_failure(tmp_path, mocker):
    from jailbee.git import diff_shortstat_between

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=mocker.Mock(returncode=128, stdout="", stderr="fatal: bad object"),
    )

    assert diff_shortstat_between(tmp_path, "HEAD", "abc123") is None


# ---------------------------------------------------------------------------
# count_commits_between
# ---------------------------------------------------------------------------


def test_count_commits_between_returns_the_count(tmp_path, mocker):
    from jailbee.git import count_commits_between

    run = mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=mocker.Mock(returncode=0, stdout="3\n", stderr=""),
    )

    assert count_commits_between(tmp_path, "HEAD", "abc123") == "3"
    assert run.call_args.args[0] == ["git", "rev-list", "--count", "HEAD..abc123"]


def test_count_commits_between_returns_none_on_failure(tmp_path, mocker):
    from jailbee.git import count_commits_between

    mocker.patch("jailbee.git.subprocess.run", side_effect=OSError)

    assert count_commits_between(tmp_path, "HEAD", "abc123") is None
