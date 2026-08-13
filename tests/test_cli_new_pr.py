"""CLI orchestration tests for `gie new --pr`."""

from __future__ import annotations

import pytest


def _patch_base_fetch(mocker, sha: str | None = "basesha"):
    """Stub the PR base-branch fetch (network op) for the whole module.

    Every `gie new --pr` test goes through it, so it is stubbed centrally;
    tests that care about it re-patch with their own return value.
    """
    return mocker.patch("jailbee.pr.fetch_base_ref", return_value=sha)


def _setup(tmp_path, mocker):
    """Reuse the shared new_cmd test environment from tests/test_cli.py."""
    from tests.test_cli import _setup_new_cmd_env

    _patch_base_fetch(mocker)
    return _setup_new_cmd_env(tmp_path, mocker)


def _run(args):
    from typer.testing import CliRunner

    from jailbee.cli import app

    return CliRunner().invoke(app, args)


@pytest.mark.parametrize(
    "extra,expected_msg",
    [
        (["--mount"], "mount"),
        (["--base", "feat/x"], "base"),
        (["--current"], "current"),
        (["--no-clone"], "no-clone"),
        (["feat/positional"], "positional"),
    ],
)
def test_new_pr_rejects_conflicting_flags(tmp_path, mocker, extra, expected_msg):
    _setup(tmp_path, mocker)
    result = _run(["new", "--pr", "1234", "--no-autostart", *extra])
    assert result.exit_code == 2, result.output
    assert expected_msg.lower() in result.output.lower()


def test_no_fetch_without_pr_errors(tmp_path, mocker):
    _setup(tmp_path, mocker)
    result = _run(["new", "feat/x", "--no-fetch", "--no-clone", "--no-autostart"])
    assert result.exit_code == 2, result.output
    assert "--no-fetch requires --pr" in result.output


def _open_pr_info():
    from jailbee.pr import PrInfo

    return PrInfo(
        number=1234,
        head_ref="feat/foo",
        head_sha="newsha",
        state="OPEN",
        base_ref="main",
    )


def _fetch_result(updated=True, prev=None, new="newsha", ref="refs/jailbee/pr/1234/head"):
    from jailbee.pr import FetchResult

    return FetchResult(updated=updated, prev_sha=prev, new_sha=new, ref=ref)


def test_new_pr_happy_path_delegates_to_new_container(tmp_path, mocker):
    _, new_container = _setup(tmp_path, mocker)
    resolve = mocker.patch("jailbee.pr.resolve_pr", return_value=_open_pr_info())
    fetch = mocker.patch(
        "jailbee.pr.fetch_pr_head",
        return_value=_fetch_result(),
    )

    result = _run(["new", "--pr", "1234", "--no-autostart"])

    assert result.exit_code == 0, result.output
    resolve.assert_called_once()
    assert resolve.call_args.args[1] == 1234
    fetch.assert_called_once()
    opts = new_container.call_args.args[2]
    assert opts.container_branch == "feat/foo"
    assert opts.base is None


def test_new_pr_pins_the_clone_to_the_fetched_head(tmp_path, mocker):
    """The container is built from the PR head SHA, not from a host branch.

    The head lives in `refs/jailbee/pr/<N>/head` (never a branch — git refuses to
    fetch into a checked-out one), so `new_container` cannot resolve it by
    name: it gets the commit directly.
    """
    _, new_container = _setup(tmp_path, mocker)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_open_pr_info())
    mocker.patch("jailbee.pr.fetch_pr_head", return_value=_fetch_result())

    result = _run(["new", "--pr", "1234", "--no-autostart"])

    assert result.exit_code == 0, result.output
    opts = new_container.call_args.args[2]
    assert opts.clone_commit == "newsha"


def test_new_pr_passes_base_ref_as_base_branch_label(tmp_path, mocker):
    """`gie new --pr N` puts PR's baseRefName into NewContainerOptions.base_branch_label.

    The container is checked out on the PR head branch, but for review we want
    `gie ls`/`gie diff` to compare against the PR's *target* branch (base_ref),
    not the head ref (which would compare the branch against itself → empty).
    """
    _, new_container = _setup(tmp_path, mocker)
    from jailbee.pr import PrInfo

    pr_info = PrInfo(
        number=1234,
        head_ref="contributor/feat",
        head_sha="newsha",
        state="OPEN",
        base_ref="dev",
    )
    mocker.patch("jailbee.pr.resolve_pr", return_value=pr_info)
    mocker.patch(
        "jailbee.pr.fetch_pr_head",
        return_value=_fetch_result(),
    )

    result = _run(["new", "--pr", "1234", "--no-autostart"])
    assert result.exit_code == 0, result.output
    opts = new_container.call_args.args[2]
    assert opts.base_branch_label == "dev"


def test_new_pr_fetches_the_base_branch_before_creating(tmp_path, mocker):
    """`gie new --pr N` refreshes origin/<baseRefName> in the source repo.

    Regression: only `pull/N/head` used to be fetched, so `lifecycle` seeded
    `refs/jailbee/base/<base>` from whatever stale `origin/<base>` the host
    happened to have. When that stale tip predated the PR's branch point, the
    three-dot diff took it as the merge base and folded every base-branch
    commit made since the last host fetch into the PR's diff — `gie ls`
    reported e.g. +15429/-2780 over 313 commits for an actual +476/-756 PR.
    """
    from jailbee.pr import PrInfo

    _setup(tmp_path, mocker)
    base_fetch = _patch_base_fetch(mocker, "freshbasesha")
    mocker.patch(
        "jailbee.pr.resolve_pr",
        return_value=PrInfo(
            number=1234,
            head_ref="feat/foo",
            head_sha="newsha",
            state="OPEN",
            base_ref="dev",
        ),
    )
    mocker.patch("jailbee.pr.fetch_pr_head", return_value=_fetch_result())

    result = _run(["new", "--pr", "1234", "--no-autostart"])

    assert result.exit_code == 0, result.output
    base_fetch.assert_called_once()
    assert base_fetch.call_args.args[1] == "dev"


def test_new_pr_warns_but_continues_when_base_fetch_fails(tmp_path, mocker):
    """A base branch deleted upstream (merged PR) must not block the container."""
    _, new_container = _setup(tmp_path, mocker)
    _patch_base_fetch(mocker, None)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_open_pr_info())
    mocker.patch("jailbee.pr.fetch_pr_head", return_value=_fetch_result())

    result = _run(["new", "--pr", "1234", "--no-autostart"])

    assert result.exit_code == 0, result.output
    assert "could not fetch" in result.output.lower()
    assert "main" in result.output
    new_container.assert_called_once()


def test_new_pr_no_fetch_skips_base_fetch(tmp_path, mocker):
    """`--no-fetch` means no network: the base fetch is skipped along with the head."""
    _setup(tmp_path, mocker)
    base_fetch = _patch_base_fetch(mocker)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_open_pr_info())
    mocker.patch("jailbee.pr.resolve_pr_head_sha", return_value="hostsha")

    result = _run(["new", "--pr", "1234", "--no-fetch", "--no-autostart"])

    assert result.exit_code == 0, result.output
    base_fetch.assert_not_called()


def test_new_pr_resolve_error_exits_2(tmp_path, mocker):
    _setup(tmp_path, mocker)
    from jailbee.pr import PrResolveError

    mocker.patch(
        "jailbee.pr.resolve_pr",
        side_effect=PrResolveError("PR #1234 not found"),
    )

    result = _run(["new", "--pr", "1234", "--no-autostart"])
    assert result.exit_code == 2
    assert "PR #1234 not found" in result.output


def test_new_pr_fetch_error_exits_2(tmp_path, mocker):
    _setup(tmp_path, mocker)
    from jailbee.pr import PrFetchError

    mocker.patch("jailbee.pr.resolve_pr", return_value=_open_pr_info())
    mocker.patch(
        "jailbee.pr.fetch_pr_head",
        side_effect=PrFetchError("git fetch failed for PR #1234: no network"),
    )

    result = _run(["new", "--pr", "1234", "--no-autostart"])
    assert result.exit_code == 2
    assert "no network" in result.output


def _pr_info_with_state(state):
    from jailbee.pr import PrInfo

    return PrInfo(
        number=1234,
        head_ref="feat/foo",
        head_sha="newsha",
        state=state,
        base_ref="main",
    )


@pytest.mark.parametrize(
    "state,expected_phrase",
    [
        ("CLOSED", "CLOSED"),
        ("MERGED", "MERGED"),
    ],
)
def test_new_pr_warns_on_closed_or_merged(tmp_path, mocker, state, expected_phrase):
    _setup(tmp_path, mocker)
    mocker.patch(
        "jailbee.pr.resolve_pr",
        return_value=_pr_info_with_state(state),
    )
    mocker.patch(
        "jailbee.pr.fetch_pr_head",
        return_value=_fetch_result(),
    )

    result = _run(["new", "--pr", "1234", "--no-autostart"])
    assert result.exit_code == 0
    assert expected_phrase in result.output


def test_new_pr_no_fetch_skips_fetch(tmp_path, mocker):
    _, new_container = _setup(tmp_path, mocker)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_open_pr_info())
    resolve_head = mocker.patch("jailbee.pr.resolve_pr_head_sha", return_value="hostsha")
    fetch = mocker.patch("jailbee.pr.fetch_pr_head")

    result = _run(["new", "--pr", "1234", "--no-fetch", "--no-autostart"])

    assert result.exit_code == 0, result.output
    fetch.assert_not_called()
    resolve_head.assert_called_once()
    opts = new_container.call_args.args[2]
    assert opts.container_branch == "feat/foo"
    assert opts.clone_commit == "hostsha"


def test_new_pr_no_fetch_missing_head_exits_2(tmp_path, mocker):
    _setup(tmp_path, mocker)
    from jailbee.pr import PrError

    mocker.patch("jailbee.pr.resolve_pr", return_value=_open_pr_info())
    mocker.patch(
        "jailbee.pr.resolve_pr_head_sha",
        side_effect=PrError("PR #1234's head is not on the host"),
    )

    result = _run(["new", "--pr", "1234", "--no-fetch", "--no-autostart"])
    assert result.exit_code == 2
    assert "not on the host" in result.output


def test_new_pr_passes_pr_number_into_opts(tmp_path, mocker):
    """`gie new --pr N` forwards the PR number into NewContainerOptions.pr;
    persisting it as the `user.jailbee.pr` label is `new_container`'s job (tested
    in test_lifecycle.py), so it survives an autostart failure."""
    _, new_container = _setup(tmp_path, mocker)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_open_pr_info())
    mocker.patch(
        "jailbee.pr.fetch_pr_head",
        return_value=_fetch_result(),
    )

    result = _run(["new", "--pr", "1234", "--no-autostart"])
    assert result.exit_code == 0, result.output

    opts = new_container.call_args.args[2]
    assert opts.pr == 1234


def test_new_pr_from_a_fork_marks_the_head_untrusted(tmp_path, mocker):
    """A fork's head is what the autostart privilege gate treats as unvouched."""
    import dataclasses

    _, new_container = _setup(tmp_path, mocker)
    fork_pr = dataclasses.replace(
        _open_pr_info(), is_cross_repository=True, head_repo_owner="outsider"
    )
    mocker.patch("jailbee.pr.resolve_pr", return_value=fork_pr)
    mocker.patch("jailbee.pr.fetch_pr_head", return_value=_fetch_result())

    result = _run(["new", "--pr", "1234", "--no-autostart"])
    assert result.exit_code == 0, result.output

    assert new_container.call_args.args[2].untrusted_head is True


def test_new_pr_within_the_repo_is_not_untrusted(tmp_path, mocker):
    """An internal PR's head is a branch in this repo's own origin.

    Identical content to `gie new <branch>`, pushed by someone who can already
    run code in these containers — so it must not be gated differently.
    """
    _, new_container = _setup(tmp_path, mocker)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_open_pr_info())
    mocker.patch("jailbee.pr.fetch_pr_head", return_value=_fetch_result())

    result = _run(["new", "--pr", "1234", "--no-autostart"])
    assert result.exit_code == 0, result.output

    opts = new_container.call_args.args[2]
    assert opts.pr == 1234
    assert opts.untrusted_head is False


def test_new_pr_does_not_prompt_when_branch_exists_in_source(tmp_path, mocker):
    """A host branch matching the PR head is normal — reviewing your own PR.

    `--pr` pins the clone to the fetched head commit and ignores that branch
    entirely, so the "Use existing branch?" confirmation has nothing to warn
    about and must not fire.
    """
    _, new_container = _setup(tmp_path, mocker)
    mocker.patch("jailbee.pr.resolve_pr", return_value=_open_pr_info())
    mocker.patch(
        "jailbee.pr.fetch_pr_head",
        return_value=_fetch_result(),
    )
    mocker.patch("jailbee.git.branch_exists_in_source", return_value=True)

    # No `input=` — if the prompt fired, CliRunner's empty stdin would abort.
    result = _run(["new", "--pr", "1234", "--no-autostart"])

    assert result.exit_code == 0, result.output
    assert "Use existing branch" not in result.output
    new_container.assert_called_once()


def test_new_pr_with_name_override(tmp_path, mocker):
    _, new_container = _setup(tmp_path, mocker)
    new_container.return_value = "custom-name"
    mocker.patch("jailbee.pr.resolve_pr", return_value=_open_pr_info())
    mocker.patch(
        "jailbee.pr.fetch_pr_head",
        return_value=_fetch_result(),
    )
    result = _run(["new", "--pr", "1234", "--name", "custom-name", "--no-autostart"])
    assert result.exit_code == 0, result.output

    opts = new_container.call_args.args[2]
    assert opts.name == "custom-name"
    assert opts.container_branch == "feat/foo"
    assert opts.pr == 1234
