"""Unit tests for jailbee.pr."""

from __future__ import annotations

import json
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_pr_info_is_frozen_dataclass():
    from jailbee.pr import PrInfo

    pr = PrInfo(number=1, head_ref="feat/x", head_sha="abc", state="OPEN", base_ref="main")
    assert pr.number == 1
    assert pr.head_ref == "feat/x"
    assert pr.head_sha == "abc"
    assert pr.state == "OPEN"
    assert pr.base_ref == "main"
    with pytest.raises(FrozenInstanceError):
        pr.number = 2  # type: ignore[misc]


def test_fetch_result_is_frozen_dataclass():
    from jailbee.pr import FetchResult

    fr = FetchResult(updated=True, prev_sha=None, new_sha="abc", ref="refs/jailbee/pr/1/head")
    assert fr.updated is True
    assert fr.prev_sha is None
    assert fr.new_sha == "abc"
    assert fr.ref == "refs/jailbee/pr/1/head"
    with pytest.raises(FrozenInstanceError):
        fr.updated = False  # type: ignore[misc]


def test_exceptions_form_hierarchy():
    from jailbee.pr import PrError, PrFetchError, PrResolveError

    assert issubclass(PrResolveError, PrError)
    assert issubclass(PrFetchError, PrError)


def test_resolve_pr_happy_path(tmp_path, mocker):
    from jailbee.pr import PrInfo, resolve_pr

    payload = (FIXTURES / "gh_pr_view_open.json").read_text()

    def fake_run(cmd, **kwargs):
        if cmd[0] == "git" and "get-url" in cmd:
            return _completed(stdout="git@github.com:acme/widgets.git\n")
        if cmd[0] == "gh" and cmd[1:3] == ["pr", "view"]:
            return _completed(stdout=payload)
        raise AssertionError(f"unexpected command: {cmd}")

    mocker.patch("subprocess.run", side_effect=fake_run)
    result = resolve_pr(tmp_path, 1234)
    assert result == PrInfo(
        number=1234,
        head_ref="feat/foo",
        head_sha="abc123def456abc123def456abc123def456abcd",
        state="OPEN",
        base_ref="main",
        author_login="octocat",
        is_cross_repository=False,
        head_repo_owner="acme",
    )


def test_resolve_pr_no_origin(tmp_path, mocker):
    from jailbee.pr import PrResolveError, resolve_pr

    mocker.patch(
        "subprocess.run",
        return_value=_completed(returncode=2, stderr="fatal: no such remote\n"),
    )
    with pytest.raises(PrResolveError, match="GitHub 'origin' remote"):
        resolve_pr(tmp_path, 1234)


def test_resolve_pr_origin_not_github(tmp_path, mocker):
    from jailbee.pr import PrResolveError, resolve_pr

    mocker.patch(
        "subprocess.run",
        return_value=_completed(stdout="git@gitlab.com:acme/widgets.git\n"),
    )
    with pytest.raises(PrResolveError, match="GitHub 'origin' remote"):
        resolve_pr(tmp_path, 1234)


def test_resolve_pr_gh_not_installed(tmp_path, mocker):
    from jailbee.pr import PrResolveError, resolve_pr

    def fake_run(cmd, **kwargs):
        if "get-url" in cmd:
            return _completed(stdout="git@github.com:acme/widgets.git\n")
        raise FileNotFoundError("gh")

    mocker.patch("subprocess.run", side_effect=fake_run)
    with pytest.raises(PrResolveError, match=r"(?i)install"):
        resolve_pr(tmp_path, 1234)


def test_resolve_pr_not_authenticated(tmp_path, mocker):
    from jailbee.pr import PrResolveError, resolve_pr

    def fake_run(cmd, **kwargs):
        if "get-url" in cmd:
            return _completed(stdout="git@github.com:acme/widgets.git\n")
        return _completed(returncode=1, stderr="gh: not logged in to github.com\n")

    mocker.patch("subprocess.run", side_effect=fake_run)
    with pytest.raises(PrResolveError, match="authenticated"):
        resolve_pr(tmp_path, 1234)


def test_resolve_pr_not_found(tmp_path, mocker):
    from jailbee.pr import PrResolveError, resolve_pr

    def fake_run(cmd, **kwargs):
        if "get-url" in cmd:
            return _completed(stdout="git@github.com:acme/widgets.git\n")
        return _completed(returncode=1, stderr="no pull requests found for 9999\n")

    mocker.patch("subprocess.run", side_effect=fake_run)
    with pytest.raises(PrResolveError, match="not found"):
        resolve_pr(tmp_path, 9999)


@pytest.mark.parametrize(
    "fixture_name,expected_state",
    [
        ("gh_pr_view_open.json", "OPEN"),
        ("gh_pr_view_merged.json", "MERGED"),
        ("gh_pr_view_closed.json", "CLOSED"),
    ],
)
def test_resolve_pr_state_parametrised(tmp_path, mocker, fixture_name, expected_state):
    from jailbee.pr import resolve_pr

    payload = (FIXTURES / fixture_name).read_text()

    def fake_run(cmd, **kwargs):
        if "get-url" in cmd:
            return _completed(stdout="git@github.com:acme/widgets.git\n")
        return _completed(stdout=payload)

    mocker.patch("subprocess.run", side_effect=fake_run)
    result = resolve_pr(tmp_path, 1234)
    assert result.state == expected_state


def test_resolve_pr_reports_fork_head(tmp_path, mocker):
    from jailbee.pr import resolve_pr

    payload = json.dumps(
        {
            "number": 7,
            "headRefName": "patch-1",
            "headRefOid": "f" * 40,
            "state": "OPEN",
            "baseRefName": "main",
            "author": {"login": "contributor"},
            "isCrossRepository": True,
            "headRepositoryOwner": {"login": "contributor"},
        }
    )

    def fake_run(cmd, **kwargs):
        if "get-url" in cmd:
            return _completed(stdout="git@github.com:acme/widgets.git\n")
        return _completed(stdout=payload)

    mocker.patch("subprocess.run", side_effect=fake_run)
    result = resolve_pr(tmp_path, 7)

    assert result.is_cross_repository is True
    assert result.head_repo_owner == "contributor"
    assert result.author_login == "contributor"


def test_resolve_pr_tolerates_null_author_and_missing_owner(tmp_path, mocker):
    """A deleted GitHub account serialises as `author: null`; older gh payloads
    may omit the repository-owner object entirely. Neither may raise."""
    from jailbee.pr import resolve_pr

    payload = json.dumps(
        {
            "number": 8,
            "headRefName": "feat/x",
            "headRefOid": "a" * 40,
            "state": "OPEN",
            "baseRefName": "main",
            "author": None,
        }
    )

    def fake_run(cmd, **kwargs):
        if "get-url" in cmd:
            return _completed(stdout="git@github.com:acme/widgets.git\n")
        return _completed(stdout=payload)

    mocker.patch("subprocess.run", side_effect=fake_run)
    result = resolve_pr(tmp_path, 8)

    assert result.author_login is None
    assert result.head_repo_owner is None
    assert result.is_cross_repository is False


# ---------------------------------------------------------------------------
# find_pr_for_branch
# ---------------------------------------------------------------------------


def _pr_view_payload(**overrides):
    payload = {
        "number": 42,
        "headRefName": "feat/foo",
        "headRefOid": "b" * 40,
        "state": "OPEN",
        "baseRefName": "main",
        "author": {"login": "someone"},
        "isCrossRepository": False,
        "headRepositoryOwner": {"login": "acme"},
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_find_pr_for_branch_resolves_the_pr_whose_head_is_the_branch(tmp_path, mocker):
    from jailbee.pr import GH_PR_VIEW_JSON_FIELDS, find_pr_for_branch

    run = mocker.patch("subprocess.run", return_value=_completed(stdout=_pr_view_payload()))
    found = find_pr_for_branch(tmp_path, "feat/foo")

    assert found is not None
    assert (found.number, found.head_ref, found.state) == (42, "feat/foo", "OPEN")
    assert found.author_login == "someone"
    assert run.call_args.args[0] == [
        "gh",
        "pr",
        "view",
        "feat/foo",
        "--json",
        GH_PR_VIEW_JSON_FIELDS,
    ]
    assert run.call_args.kwargs["cwd"] == tmp_path


def test_find_pr_for_branch_returns_none_when_the_branch_has_no_pr(tmp_path, mocker):
    from jailbee.pr import find_pr_for_branch

    mocker.patch(
        "subprocess.run",
        return_value=_completed(
            returncode=1, stderr='no pull requests found for branch "feat/foo"\n'
        ),
    )
    assert find_pr_for_branch(tmp_path, "feat/foo") is None


def test_find_pr_for_branch_returns_none_when_gh_is_missing(tmp_path, mocker):
    """Best-effort: a missing gh must not block opening a PR later on."""
    from jailbee.pr import find_pr_for_branch

    mocker.patch("subprocess.run", side_effect=FileNotFoundError("gh"))
    assert find_pr_for_branch(tmp_path, "feat/foo") is None


def test_find_pr_for_branch_returns_none_on_unparsable_output(tmp_path, mocker):
    from jailbee.pr import find_pr_for_branch

    mocker.patch("subprocess.run", return_value=_completed(stdout="not json"))
    assert find_pr_for_branch(tmp_path, "feat/foo") is None


def _pr_info(head_sha="newsha", head_ref="feat/foo"):
    from jailbee.pr import PrInfo

    return PrInfo(number=1234, head_ref=head_ref, head_sha=head_sha, state="OPEN", base_ref="main")


def test_pr_head_ref_is_namespaced_per_pr_number():
    from jailbee.pr import pr_head_ref

    assert pr_head_ref(1234) == "refs/jailbee/pr/1234/head"


def test_fetch_pr_head_targets_the_gie_pr_ref_forced(tmp_path, mocker):
    """The head lands in gie's own ref namespace, force-updated.

    Forced because the PR head is authoritative and can be force-pushed
    upstream; gie's copy must follow it, never reject the update.
    """
    from jailbee.pr import FetchResult, fetch_pr_head

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd:
            return _completed(returncode=1)  # ref absent
        if cmd[0] == "git" and "fetch" in cmd:
            return _completed()
        raise AssertionError(f"unexpected command: {cmd}")

    run = mocker.patch("subprocess.run", side_effect=fake_run)
    result = fetch_pr_head(tmp_path, _pr_info())
    assert result == FetchResult(
        updated=True, prev_sha=None, new_sha="newsha", ref="refs/jailbee/pr/1234/head"
    )
    fetch_calls = [c for c in run.call_args_list if "fetch" in c.args[0]]
    assert fetch_calls == [
        mocker.call(
            ["git", "fetch", "origin", "+pull/1234/head:refs/jailbee/pr/1234/head"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
    ]


def test_fetch_pr_head_never_touches_refs_heads(tmp_path, mocker):
    """Regression: git refuses to fetch into a branch checked out in a worktree.

    `gie new --pr N` used to fetch into `refs/heads/<head_ref>`, which fails
    outright ("refusing to fetch into branch ... checked out at ...") whenever
    the host has that very branch checked out — the common case for reviewing
    your own PR. Nothing in the fetch may name `refs/heads/*` any more.
    """
    from jailbee.pr import fetch_pr_head

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd:
            return _completed(returncode=1)
        if "fetch" in cmd:
            return _completed()
        raise AssertionError(f"unexpected command: {cmd}")

    run = mocker.patch("subprocess.run", side_effect=fake_run)
    fetch_pr_head(tmp_path, _pr_info())

    for call in run.call_args_list:
        assert not any("refs/heads/" in arg for arg in call.args[0]), call.args[0]


def test_fetch_pr_head_already_at_head_is_noop(tmp_path, mocker):
    from jailbee.pr import FetchResult, fetch_pr_head

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd:
            return _completed(stdout="newsha\n")  # ref already at head
        if "fetch" in cmd:
            return _completed()
        raise AssertionError(f"unexpected command: {cmd}")

    mocker.patch("subprocess.run", side_effect=fake_run)
    result = fetch_pr_head(tmp_path, _pr_info(head_sha="newsha"))
    assert result == FetchResult(
        updated=False, prev_sha="newsha", new_sha="newsha", ref="refs/jailbee/pr/1234/head"
    )


def test_fetch_pr_head_reports_previous_sha(tmp_path, mocker):
    from jailbee.pr import FetchResult, fetch_pr_head

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd:
            return _completed(stdout="oldsha\n")
        if "fetch" in cmd:
            return _completed()
        raise AssertionError(f"unexpected command: {cmd}")

    mocker.patch("subprocess.run", side_effect=fake_run)
    result = fetch_pr_head(tmp_path, _pr_info(head_sha="newsha"))
    assert result == FetchResult(
        updated=True, prev_sha="oldsha", new_sha="newsha", ref="refs/jailbee/pr/1234/head"
    )


def test_fetch_pr_head_prev_sha_is_read_from_the_gie_ref(tmp_path, mocker):
    """The `updated` flag compares against gie's ref, not any local branch."""
    from jailbee.pr import fetch_pr_head

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd:
            return _completed(stdout="oldsha\n")
        if "fetch" in cmd:
            return _completed()
        raise AssertionError(f"unexpected command: {cmd}")

    run = mocker.patch("subprocess.run", side_effect=fake_run)
    fetch_pr_head(tmp_path, _pr_info())

    rev_parse_calls = [c for c in run.call_args_list if "rev-parse" in c.args[0]]
    assert rev_parse_calls == [
        mocker.call(
            ["git", "rev-parse", "--verify", "--quiet", "refs/jailbee/pr/1234/head"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
    ]


def test_fetch_pr_head_failure_surfaces_stderr(tmp_path, mocker):
    from jailbee.pr import PrFetchError, fetch_pr_head

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd:
            return _completed(returncode=1)
        if "fetch" in cmd:
            return _completed(
                returncode=128,
                stderr="fatal: unable to access 'https://github.com/...': network\n",
            )
        raise AssertionError(f"unexpected command: {cmd}")

    mocker.patch("subprocess.run", side_effect=fake_run)
    with pytest.raises(PrFetchError, match="network"):
        fetch_pr_head(tmp_path, _pr_info())


def test_fetch_pr_head_retries_a_transient_failure_when_accepted(tmp_path, mocker):
    from jailbee.pr import FetchResult, fetch_pr_head

    fetch_attempts = []

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd:
            return _completed(stdout="oldsha\n")
        if "fetch" in cmd:
            fetch_attempts.append(cmd)
            if len(fetch_attempts) == 1:
                return _completed(returncode=128, stderr="fatal: Could not read from remote\n")
            return _completed()
        raise AssertionError(f"unexpected command: {cmd}")

    mocker.patch("subprocess.run", side_effect=fake_run)
    mocker.patch("jailbee.retry._stdin_is_interactive", return_value=True)
    mocker.patch("builtins.input", return_value="y")

    result = fetch_pr_head(tmp_path, _pr_info(head_sha="newsha"))

    assert result == FetchResult(
        updated=True, prev_sha="oldsha", new_sha="newsha", ref="refs/jailbee/pr/1234/head"
    )
    assert len(fetch_attempts) == 2


def test_fetch_pr_head_retry_is_not_offered_off_tty(tmp_path, mocker):
    from jailbee.pr import PrFetchError, fetch_pr_head

    fetch_attempts = []

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd:
            return _completed(stdout="oldsha\n")
        if "fetch" in cmd:
            fetch_attempts.append(cmd)
            return _completed(returncode=128, stderr="fatal: Could not read from remote\n")
        raise AssertionError(f"unexpected command: {cmd}")

    mocker.patch("subprocess.run", side_effect=fake_run)
    mocker.patch("jailbee.retry._stdin_is_interactive", return_value=False)
    prompt = mocker.patch("builtins.input")

    with pytest.raises(PrFetchError, match="Could not read"):
        fetch_pr_head(tmp_path, _pr_info())

    assert len(fetch_attempts) == 1
    prompt.assert_not_called()


def test_resolve_pr_git_not_installed(tmp_path, mocker):
    from jailbee.pr import PrResolveError, resolve_pr

    mocker.patch("subprocess.run", side_effect=FileNotFoundError("git"))
    with pytest.raises(PrResolveError, match=r"(?i)git"):
        resolve_pr(tmp_path, 1234)


def test_fetch_pr_head_git_not_installed_on_rev_parse(tmp_path, mocker):
    from jailbee.pr import PrFetchError, fetch_pr_head

    mocker.patch("subprocess.run", side_effect=FileNotFoundError("git"))
    with pytest.raises(PrFetchError, match=r"(?i)git"):
        fetch_pr_head(tmp_path, _pr_info())


# ---------------------------------------------------------------------------
# fetch_base_ref
# ---------------------------------------------------------------------------


def test_fetch_base_ref_updates_remote_tracking_and_returns_sha(tmp_path, mocker):
    """The base branch is fetched into refs/remotes/origin/<base>, forced.

    Forced because a base branch can be rebased/force-pushed upstream; the
    remote-tracking ref must follow the actual remote tip, which is what
    `lifecycle` seeds `refs/jailbee/base/<base>` from.
    """
    from jailbee.pr import fetch_base_ref

    def fake_run(cmd, **kwargs):
        if "fetch" in cmd:
            return _completed()
        if "rev-parse" in cmd:
            return _completed(stdout="basesha\n")
        raise AssertionError(f"unexpected command: {cmd}")

    run = mocker.patch("subprocess.run", side_effect=fake_run)
    assert fetch_base_ref(tmp_path, "dev") == "basesha"
    fetch_calls = [c for c in run.call_args_list if "fetch" in c.args[0]]
    assert fetch_calls == [
        mocker.call(
            ["git", "fetch", "origin", "+refs/heads/dev:refs/remotes/origin/dev"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
    ]


def test_fetch_base_ref_returns_none_when_fetch_fails(tmp_path, mocker):
    """A deleted/renamed base branch upstream must not raise — best-effort only."""
    from jailbee.pr import fetch_base_ref

    def fake_run(cmd, **kwargs):
        if "fetch" in cmd:
            return _completed(returncode=128, stderr="couldn't find remote ref\n")
        raise AssertionError(f"unexpected command: {cmd}")

    mocker.patch("subprocess.run", side_effect=fake_run)
    assert fetch_base_ref(tmp_path, "gone") is None


def test_fetch_base_ref_returns_none_when_rev_parse_fails(tmp_path, mocker):
    from jailbee.pr import fetch_base_ref

    def fake_run(cmd, **kwargs):
        if "fetch" in cmd:
            return _completed()
        return _completed(returncode=1)

    mocker.patch("subprocess.run", side_effect=fake_run)
    assert fetch_base_ref(tmp_path, "dev") is None


def test_fetch_base_ref_returns_none_when_git_missing(tmp_path, mocker):
    from jailbee.pr import fetch_base_ref

    mocker.patch("subprocess.run", side_effect=FileNotFoundError("git"))
    assert fetch_base_ref(tmp_path, "dev") is None


# ---------------------------------------------------------------------------
# resolve_pr_head_sha (the --no-fetch path)
# ---------------------------------------------------------------------------


def test_resolve_pr_head_sha_prefers_the_gie_pr_ref(tmp_path, mocker):
    from jailbee.pr import resolve_pr_head_sha

    def fake_run(cmd, **kwargs):
        if "refs/jailbee/pr/1234/head" in cmd:
            return _completed(stdout="giesha\n")
        raise AssertionError(f"unexpected command: {cmd}")

    mocker.patch("subprocess.run", side_effect=fake_run)
    assert resolve_pr_head_sha(tmp_path, _pr_info()) == "giesha"


def test_resolve_pr_head_sha_falls_back_to_the_local_branch(tmp_path, mocker):
    """A user who fetched the head by hand keeps working with --no-fetch."""
    from jailbee.pr import resolve_pr_head_sha

    def fake_run(cmd, **kwargs):
        if "refs/jailbee/pr/1234/head" in cmd:
            return _completed(returncode=1)
        if "refs/heads/feat/foo" in cmd:
            return _completed(stdout="localsha\n")
        raise AssertionError(f"unexpected command: {cmd}")

    mocker.patch("subprocess.run", side_effect=fake_run)
    assert resolve_pr_head_sha(tmp_path, _pr_info()) == "localsha"


def test_resolve_pr_head_sha_raises_when_head_is_nowhere(tmp_path, mocker):
    from jailbee.pr import PrError, resolve_pr_head_sha

    mocker.patch("subprocess.run", return_value=_completed(returncode=1))
    with pytest.raises(PrError, match="--no-fetch"):
        resolve_pr_head_sha(tmp_path, _pr_info())


def test_resolve_pr_head_sha_git_not_installed(tmp_path, mocker):
    from jailbee.pr import PrError, resolve_pr_head_sha

    mocker.patch("subprocess.run", side_effect=FileNotFoundError("git"))
    with pytest.raises(PrError, match=r"(?i)git"):
        resolve_pr_head_sha(tmp_path, _pr_info())


# ---------------------------------------------------------------------------
# create_pr
# ---------------------------------------------------------------------------

_ORIGIN = "git@github.com:acme/widgets.git\n"


def _fake_run_for_create(create_result, view_result=None):
    """Route subprocess.run calls: origin check → gh pr create → gh pr view."""

    def fake_run(cmd, **kwargs):
        if cmd[0] == "git" and "get-url" in cmd:
            return _completed(stdout=_ORIGIN)
        if cmd[0] == "gh" and cmd[1:3] == ["pr", "create"]:
            return create_result
        if cmd[0] == "gh" and cmd[1:3] == ["pr", "view"] and view_result is not None:
            return view_result
        raise AssertionError(f"unexpected command: {cmd}")

    return fake_run


def test_create_pr_happy_path_draft(tmp_path, mocker):
    from jailbee.pr import PrCreated, create_pr

    run = mocker.patch(
        "subprocess.run",
        side_effect=_fake_run_for_create(
            _completed(stdout="https://github.com/acme/widgets/pull/123\n")
        ),
    )

    result = create_pr(tmp_path, head="feat/foo", base="main", title="feat: foo", body="pending")

    assert result == PrCreated(
        number=123, url="https://github.com/acme/widgets/pull/123", already_existed=False
    )
    create_cmd = run.call_args_list[-1].args[0]
    assert create_cmd[:3] == ["gh", "pr", "create"]
    assert "--draft" in create_cmd
    head_i = create_cmd.index("--head")
    assert ["--head", "feat/foo"] == create_cmd[head_i : head_i + 2]
    base_i = create_cmd.index("--base")
    assert ["--base", "main"] == create_cmd[base_i : base_i + 2]


def test_create_pr_no_draft_omits_flag(tmp_path, mocker):
    from jailbee.pr import create_pr

    run = mocker.patch(
        "subprocess.run",
        side_effect=_fake_run_for_create(
            _completed(stdout="https://github.com/acme/widgets/pull/9\n")
        ),
    )

    result = create_pr(tmp_path, head="feat/foo", base="main", title="t", body="b", draft=False)

    assert result.number == 9
    assert "--draft" not in run.call_args_list[-1].args[0]


def test_create_pr_already_exists_falls_back_to_view(tmp_path, mocker):
    from jailbee.pr import create_pr

    create_fail = _completed(
        returncode=1,
        stderr='a pull request for branch "feat/foo" into branch "main" already exists:\n'
        "https://github.com/acme/widgets/pull/77\n",
    )
    view_ok = _completed(
        stdout='{"number": 77, "url": "https://github.com/acme/widgets/pull/77"}\n'
    )
    mocker.patch("subprocess.run", side_effect=_fake_run_for_create(create_fail, view_ok))

    result = create_pr(tmp_path, head="feat/foo", base="main", title="t", body="b")

    assert result.already_existed is True
    assert result.number == 77
    assert result.url == "https://github.com/acme/widgets/pull/77"


def test_create_pr_gh_missing(tmp_path, mocker):
    from jailbee.pr import PrCreateError, create_pr

    def fake_run(cmd, **kwargs):
        if "get-url" in cmd:
            return _completed(stdout=_ORIGIN)
        raise FileNotFoundError("gh")

    mocker.patch("subprocess.run", side_effect=fake_run)
    with pytest.raises(PrCreateError, match=r"(?i)install"):
        create_pr(tmp_path, head="h", base="b", title="t", body="b")


def test_create_pr_not_authenticated(tmp_path, mocker):
    from jailbee.pr import PrCreateError, create_pr

    mocker.patch(
        "subprocess.run",
        side_effect=_fake_run_for_create(
            _completed(returncode=1, stderr="gh: not logged in to github.com\n")
        ),
    )
    with pytest.raises(PrCreateError, match="authenticated"):
        create_pr(tmp_path, head="h", base="b", title="t", body="b")


def test_create_pr_unparseable_url(tmp_path, mocker):
    from jailbee.pr import PrCreateError, create_pr

    mocker.patch(
        "subprocess.run",
        side_effect=_fake_run_for_create(_completed(stdout="something weird\n")),
    )
    with pytest.raises(PrCreateError, match="parse"):
        create_pr(tmp_path, head="h", base="b", title="t", body="b")


def test_create_pr_requires_github_origin(tmp_path, mocker):
    from jailbee.pr import PrResolveError, create_pr

    mocker.patch(
        "subprocess.run",
        return_value=_completed(stdout="git@gitlab.com:acme/widgets.git\n"),
    )
    with pytest.raises(PrResolveError, match="GitHub 'origin' remote"):
        create_pr(tmp_path, head="h", base="b", title="t", body="b")


def test_pr_create_error_is_pr_error():
    from jailbee.pr import PrCreateError, PrError

    assert issubclass(PrCreateError, PrError)


def test_open_pr_in_browser_invokes_gh_view_web(tmp_path, mocker):
    from jailbee.pr import open_pr_in_browser

    run = mocker.patch("subprocess.run", return_value=_completed())

    open_pr_in_browser(tmp_path, 123)

    cmd = run.call_args.args[0]
    assert cmd == ["gh", "pr", "view", "123", "--web"]
    assert run.call_args.kwargs["cwd"] == tmp_path


def test_create_pr_author_error_is_not_misclassified_as_auth(tmp_path, mocker):
    """'author' contains 'auth' as a substring but must NOT trigger the auth error path."""
    from jailbee.pr import PrCreateError, create_pr

    mocker.patch(
        "subprocess.run",
        side_effect=_fake_run_for_create(
            _completed(returncode=1, stderr="GraphQL: Author is required\n")
        ),
    )
    with pytest.raises(PrCreateError, match="Author is required"):
        create_pr(tmp_path, head="h", base="b", title="t", body="b")


def test_resolve_pr_authorization_error_not_misclassified(tmp_path, mocker):
    """'authorization' contains 'auth' but not 'authentication'; falls through to generic error."""
    from jailbee.pr import PrResolveError, resolve_pr

    def fake_run(cmd, **kwargs):
        if "get-url" in cmd:
            return _completed(stdout="git@github.com:acme/widgets.git\n")
        return _completed(
            returncode=1,
            stderr="error: OAuth app authorization rules blocked this request\n",
        )

    mocker.patch("subprocess.run", side_effect=fake_run)
    with pytest.raises(PrResolveError, match="OAuth app authorization rules"):
        resolve_pr(tmp_path, 1234)


# ---------------------------------------------------------------------------
# view_existing_pr, edit_pr, set_ready
# ---------------------------------------------------------------------------


def test_view_existing_pr_returns_number_and_url(tmp_path, mocker):
    from jailbee import pr

    payload = '{"number": 77, "url": "https://github.com/acme/widgets/pull/77"}'
    run = mocker.patch("subprocess.run", return_value=_completed(stdout=payload))
    result = pr.view_existing_pr(tmp_path, "feat/foo")
    assert result.number == 77
    assert result.url == "https://github.com/acme/widgets/pull/77"
    assert result.already_existed is True
    assert run.call_args.args[0][:4] == ["gh", "pr", "view", "feat/foo"]


def test_edit_pr_sends_title_and_body(tmp_path, mocker):
    from jailbee import pr

    run = mocker.patch("subprocess.run", return_value=_completed())
    pr.edit_pr(tmp_path, 7, title="T", body="B")
    args = run.call_args.args[0]
    assert args[:4] == ["gh", "pr", "edit", "7"]
    assert "--title" in args and "T" in args
    assert "--body" in args and "B" in args


def test_edit_pr_omits_unset_field(tmp_path, mocker):
    from jailbee import pr

    run = mocker.patch("subprocess.run", return_value=_completed())
    pr.edit_pr(tmp_path, 7, title="only")
    args = run.call_args.args[0]
    assert "--title" in args
    assert "--body" not in args


def test_edit_pr_noop_when_both_none(tmp_path, mocker):
    from jailbee import pr

    run = mocker.patch("subprocess.run")
    pr.edit_pr(tmp_path, 7)
    run.assert_not_called()


def test_edit_pr_not_authenticated_raises(tmp_path, mocker):
    from jailbee import pr

    mocker.patch(
        "subprocess.run",
        return_value=_completed(returncode=1, stderr="gh: To get started, run: gh auth login"),
    )
    with pytest.raises(pr.PrEditError, match="authenticated"):
        pr.edit_pr(tmp_path, 7, title="T")


def test_edit_pr_gh_missing_raises(tmp_path, mocker):
    from jailbee import pr

    mocker.patch("subprocess.run", side_effect=FileNotFoundError("gh"))
    with pytest.raises(pr.PrEditError, match="gh"):
        pr.edit_pr(tmp_path, 7, title="T")


def test_set_ready_true_runs_ready(tmp_path, mocker):
    from jailbee import pr

    run = mocker.patch("subprocess.run", return_value=_completed())
    pr.set_ready(tmp_path, 9, True)
    assert run.call_args.args[0] == ["gh", "pr", "ready", "9"]


def test_set_ready_false_adds_undo(tmp_path, mocker):
    from jailbee import pr

    run = mocker.patch("subprocess.run", return_value=_completed())
    pr.set_ready(tmp_path, 9, False)
    assert "--undo" in run.call_args.args[0]


def test_set_ready_failure_raises(tmp_path, mocker):
    from jailbee import pr

    mocker.patch("subprocess.run", return_value=_completed(returncode=1, stderr="boom"))
    with pytest.raises(pr.PrEditError):
        pr.set_ready(tmp_path, 9, True)
