"""CLI tests for `jailbee submodule pr`."""

from __future__ import annotations

from typer.testing import CliRunner

from jailbee.cli import app

runner = CliRunner()


def _candidate(path="lib/a", commits=2, branch="feat/foo", dirty=False, stale=False):
    from jailbee.submodule_pr import SubCandidate

    return SubCandidate(
        path=path,
        commits=commits,
        branch=branch,
        dirty=dirty,
        head_sha="aaa",
        recorded_sha="bbb" if stale else "aaa",
        subject="feat: work",
    )


def _created(number=123, already=False):
    from jailbee.pr import PrCreated

    return PrCreated(
        number=number,
        url=f"https://github.com/acme/lib-a/pull/{number}",
        already_existed=already,
    )


def _setup(mocker, tmp_path, *, candidates=None, state_record=None, mock_state_record=True):
    """Wire cfg/incus/detection/publish mocks for the happy path."""
    from jailbee.pr_flow import PrRecord

    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    cfg_mock.container_prefix = "sampleapp"
    cfg_mock.upstream_remote = "origin"
    cfg_mock.claude.enabled = False
    cfg_mock.claude.ai_pr_description = True
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)

    incus_mock = mocker.MagicMock()
    incus_mock.config_get.side_effect = lambda name, key: {
        "user.jailbee.base_branch": "main",
        "user.jailbee.branch": "feat/foo",
    }.get(key)
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(incus_mock, "sampleapp-feat-foo"),
    )
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.sync.assert_container_publishable", return_value="sampleapp-feat-foo")
    mocker.patch(
        "jailbee.submodule_pr.detect_candidates",
        return_value=candidates if candidates is not None else [_candidate()],
    )
    # Real transport clones/fetches over `incus.exec` and real `git`; the
    # fixture never creates an actual host sub-repo under tmp_path, so this
    # is mocked out for every test. It now runs BEFORE resolve_remote/
    # resolve_base_branch/assert_github_remote (FIX 2 — see
    # test_transport_runs_before_the_steps_that_read_the_host_subrepo, which
    # gives it a real side_effect to check the ordering).
    mocker.patch("jailbee.submodule_pr.transport_submodule_to_host")
    mocker.patch("jailbee.submodule_pr.resolve_remote", return_value="origin")
    mocker.patch("jailbee.submodule_pr.resolve_base_branch", return_value="develop")
    mocker.patch(
        "jailbee.submodule_pr.SubmodulePrState.read",
        return_value=state_record
        if state_record is not None
        else PrRecord(None, None, False, False),
    )
    record = (
        mocker.patch("jailbee.submodule_pr.SubmodulePrState.record") if mock_state_record else None
    )
    mocker.patch("jailbee.pr.find_pr_for_branch", return_value=None)
    # `assert_github_remote` shells out to `git remote get-url` in the host
    # sub-repo (`scope.repo_root`); the fixture never creates a real one
    # there (transport_submodule_to_host is mocked above), so this needs
    # mocking too. Before FIX 2, this call ran BEFORE the transport, so a
    # never-seen-on-host submodule hit this same "not a repo" failure for
    # real, not just in tests, and reported it as "git is not installed".
    # The test that cares re-patches it with a side_effect.
    mocker.patch("jailbee.pr.assert_github_remote")
    return cfg_mock, incus_mock, record


def test_create_submodule_pr_happy_path(mocker, tmp_path):
    from jailbee.submodule_pr import SubPublishResult

    _setup(mocker, tmp_path)
    publish = mocker.patch(
        "jailbee.submodule_pr.publish_submodule_branch",
        return_value=SubPublishResult(
            src_ref="refs/jailbee-sub/feat-foo/lib/a/heads/feat/foo",
            publish_name="feat/foo",
            forced=False,
        ),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="feat: work")
    create = mocker.patch("jailbee.pr.create_pr", return_value=_created())

    result = runner.invoke(app, ["submodule", "pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert publish.call_args.kwargs["subpath"] == "lib/a"
    assert create.call_args.args[0] == tmp_path / "lib/a"
    assert create.call_args.kwargs["base"] == "develop"
    assert create.call_args.kwargs["head"] == "feat/foo"
    assert create.call_args.kwargs["label"] == "jailbee submodule pr"
    assert "#123" in result.output


def test_explicit_path_is_passed_to_selection(mocker, tmp_path):
    from jailbee.submodule_pr import SubPublishResult

    _setup(mocker, tmp_path, candidates=[_candidate("lib/a"), _candidate("lib/b")])
    publish = mocker.patch(
        "jailbee.submodule_pr.publish_submodule_branch",
        return_value=SubPublishResult(src_ref="r", publish_name="feat/foo", forced=False),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="feat: work")
    mocker.patch("jailbee.pr.create_pr", return_value=_created())

    result = runner.invoke(app, ["submodule", "pr", "feat-foo", "lib/b"])

    assert result.exit_code == 0, result.output
    assert publish.call_args.kwargs["subpath"] == "lib/b"


def test_ambiguous_target_exits_2_and_lists_the_candidates(mocker, tmp_path):
    _setup(mocker, tmp_path, candidates=[_candidate("lib/a"), _candidate("lib/b")])

    result = runner.invoke(app, ["submodule", "pr", "feat-foo"])

    assert result.exit_code == 2
    assert "lib/a" in result.output
    assert "lib/b" in result.output


def test_no_candidates_exits_0_with_an_explanation(mocker, tmp_path):
    _setup(mocker, tmp_path, candidates=[_candidate(commits=0)])

    result = runner.invoke(app, ["submodule", "pr", "feat-foo"])

    assert result.exit_code == 0
    assert "no submodule" in result.output.lower()


def test_unknown_path_exits_2(mocker, tmp_path):
    _setup(mocker, tmp_path)

    result = runner.invoke(app, ["submodule", "pr", "feat-foo", "lib/nope"])

    assert result.exit_code == 2


def test_dirty_submodule_warns(mocker, tmp_path):
    from jailbee.submodule_pr import SubPublishResult

    _setup(mocker, tmp_path, candidates=[_candidate(dirty=True)])
    mocker.patch(
        "jailbee.submodule_pr.publish_submodule_branch",
        return_value=SubPublishResult(src_ref="r", publish_name="feat/foo", forced=False),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="feat: work")
    mocker.patch("jailbee.pr.create_pr", return_value=_created())

    result = runner.invoke(app, ["submodule", "pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert "uncommitted" in result.output.lower()


def test_stale_gitlink_is_reported_as_information(mocker, tmp_path):
    from jailbee.submodule_pr import SubPublishResult

    _setup(mocker, tmp_path, candidates=[_candidate(stale=True)])
    mocker.patch(
        "jailbee.submodule_pr.publish_submodule_branch",
        return_value=SubPublishResult(src_ref="r", publish_name="feat/foo", forced=False),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="feat: work")
    mocker.patch("jailbee.pr.create_pr", return_value=_created())

    result = runner.invoke(app, ["submodule", "pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert "gitlink" in result.output.lower()


def test_detached_submodule_without_a_name_exits_2(mocker, tmp_path):
    _setup(mocker, tmp_path, candidates=[_candidate(branch=None)])

    result = runner.invoke(app, ["submodule", "pr", "feat-foo"])

    assert result.exit_code == 2
    assert "--as" in result.output


def test_as_is_rejected_once_a_pr_is_recorded(mocker, tmp_path):
    from jailbee.pr_flow import PrRecord

    _setup(
        mocker,
        tmp_path,
        state_record=PrRecord(number=12, head="user/x", author=True, adopted=False),
    )

    result = runner.invoke(app, ["submodule", "pr", "feat-foo", "--as", "user/y"])

    assert result.exit_code == 2
    assert "--as" in result.output


def test_update_path_views_the_existing_pr(mocker, tmp_path):
    from jailbee.pr_flow import PrRecord
    from jailbee.submodule_pr import SubPublishResult

    _setup(
        mocker,
        tmp_path,
        state_record=PrRecord(number=12, head="user/x", author=True, adopted=False),
    )
    mocker.patch(
        "jailbee.submodule_pr.publish_submodule_branch",
        return_value=SubPublishResult(src_ref="r", publish_name="user/x", forced=False),
    )
    view = mocker.patch("jailbee.pr.view_existing_pr", return_value=_created(12, True))
    create = mocker.patch("jailbee.pr.create_pr")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)

    result = runner.invoke(app, ["submodule", "pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    create.assert_not_called()
    view.assert_called_once()


def test_adopted_pr_publishes_to_the_adopted_head_even_when_recording_it_fails(mocker, tmp_path):
    """FIX 3 regression: `adopt_existing_pr_for_branch` records the adoption
    via `state.record`, which is best-effort (a container-label write can
    fail). Re-reading `state` afterwards would then hand back a blank
    `PrRecord` (the write never landed), and `resolve_pr_text_and_head`
    would treat a still-attached submodule as headless and fail with a
    nonsense "no head branch name was chosen" error — even though the user
    just confirmed adopting a real, open PR. The run must publish to the
    adopted head built in-process instead, independent of whether the label
    write succeeded.
    """
    from jailbee.incus import IncusError
    from jailbee.pr import PrInfo
    from jailbee.submodule_pr import SubPublishResult

    _cfg_mock, incus_mock, _record_mock = _setup(mocker, tmp_path, mock_state_record=False)
    # SubmodulePrState.record is unmocked for this test (mock_state_record=
    # False), so it runs for real and calls incus.config_set — make that
    # fail, simulating a container that cannot take a label write right when
    # the PR is adopted.
    incus_mock.config_set.side_effect = IncusError("container is frozen")
    mocker.patch(
        "jailbee.pr.find_pr_for_branch",
        return_value=PrInfo(
            number=42,
            head_ref="user/existing",
            head_sha="abc123",
            state="OPEN",
            base_ref="main",
            author_login="octocat",
            is_cross_repository=False,
        ),
    )
    publish = mocker.patch(
        "jailbee.submodule_pr.publish_submodule_branch",
        return_value=SubPublishResult(src_ref="r", publish_name="user/existing", forced=False),
    )
    view = mocker.patch("jailbee.pr.view_existing_pr", return_value=_created(42, True))
    create = mocker.patch("jailbee.pr.create_pr")

    result = runner.invoke(app, ["submodule", "pr", "feat-foo", "--yes"])

    assert result.exit_code == 0, result.output
    create.assert_not_called()
    view.assert_called_once_with(tmp_path / "lib/a", "user/existing")
    assert publish.call_args.kwargs["publish_name"] == "user/existing"


def test_non_github_submodule_upstream_exits_1_before_publishing(mocker, tmp_path):
    from jailbee.pr import PrResolveError

    _setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.pr.assert_github_remote",
        side_effect=PrResolveError("requires a GitHub 'origin' remote"),
    )
    publish = mocker.patch("jailbee.submodule_pr.publish_submodule_branch")

    result = runner.invoke(app, ["submodule", "pr", "feat-foo"])

    assert result.exit_code == 1
    assert "GitHub" in result.output
    publish.assert_not_called()


def test_transport_runs_before_the_steps_that_read_the_host_subrepo(mocker, tmp_path):
    """FIX 2 regression: for a submodule the host has never seen (added
    inside the container, or a host clone where `git submodule update
    --init` never ran for this path), the host sub-repo does not exist
    until `transport_submodule_to_host` creates it. `resolve_remote`,
    `resolve_base_branch` and `assert_github_remote` all read that sub-repo,
    so the transport must run before all three — an earlier ordering ran it
    only inside `publish_submodule_branch`, after those three already read
    (and misread, or crashed on) a directory that did not exist yet.
    """
    from jailbee.submodule_pr import SubPublishResult

    _setup(mocker, tmp_path)
    calls: list[str] = []
    mocker.patch(
        "jailbee.submodule_pr.transport_submodule_to_host",
        side_effect=lambda *a, **k: calls.append("transport"),
    )

    def _remote(*a, **k):
        calls.append("resolve_remote")
        return "origin"

    def _base(*a, **k):
        calls.append("resolve_base_branch")
        return "develop"

    def _github(*a, **k):
        calls.append("assert_github_remote")

    mocker.patch("jailbee.submodule_pr.resolve_remote", side_effect=_remote)
    mocker.patch("jailbee.submodule_pr.resolve_base_branch", side_effect=_base)
    mocker.patch("jailbee.pr.assert_github_remote", side_effect=_github)
    mocker.patch(
        "jailbee.submodule_pr.publish_submodule_branch",
        return_value=SubPublishResult(src_ref="r", publish_name="feat/foo", forced=False),
    )
    mocker.patch("jailbee.git.commit_subject", return_value="feat: work")
    mocker.patch("jailbee.pr.create_pr", return_value=_created())

    result = runner.invoke(app, ["submodule", "pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    assert calls[0] == "transport"
    assert calls.index("transport") < calls.index("resolve_remote")
    assert calls.index("transport") < calls.index("resolve_base_branch")
    assert calls.index("transport") < calls.index("assert_github_remote")


def test_publish_failure_exits_1(mocker, tmp_path):
    from jailbee.submodule_pr import SubmodulePrError

    _setup(mocker, tmp_path)
    mocker.patch(
        "jailbee.submodule_pr.publish_submodule_branch",
        side_effect=SubmodulePrError("rejected"),
    )

    result = runner.invoke(app, ["submodule", "pr", "feat-foo"])

    assert result.exit_code == 1
    assert "rejected" in result.output


def test_open_without_a_recorded_pr_exits_1(mocker, tmp_path):
    from jailbee.pr_flow import PrRecord

    _setup(mocker, tmp_path, state_record=PrRecord(None, None, False, False))
    mocker.patch("jailbee.submodule_pr.recorded_paths", return_value=[])
    detect = mocker.patch("jailbee.submodule_pr.detect_candidates")

    result = runner.invoke(app, ["submodule", "pr", "feat-foo", "--open"])

    assert result.exit_code == 1
    detect.assert_not_called()


def test_open_with_several_recorded_paths_exits_2(mocker, tmp_path):
    """Same "disambiguate with PATH" condition as the normal path: exit 2,
    not 1, so a script checking $? gets one meaning regardless of --open."""
    from jailbee.pr_flow import PrRecord

    _setup(mocker, tmp_path, state_record=PrRecord(None, None, False, False))
    mocker.patch("jailbee.submodule_pr.recorded_paths", return_value=["lib/a", "lib/b"])
    detect = mocker.patch("jailbee.submodule_pr.detect_candidates")

    result = runner.invoke(app, ["submodule", "pr", "feat-foo", "--open"])

    assert result.exit_code == 2
    assert "PATH" in result.output
    detect.assert_not_called()


def test_open_uses_the_recorded_pr(mocker, tmp_path):
    from jailbee.pr_flow import PrRecord

    _setup(
        mocker,
        tmp_path,
        state_record=PrRecord(number=12, head="user/x", author=True, adopted=False),
    )
    mocker.patch("jailbee.submodule_pr.recorded_paths", return_value=["lib/a"])
    browser = mocker.patch("jailbee.pr.open_pr_in_browser")
    publish = mocker.patch("jailbee.submodule_pr.publish_submodule_branch")

    result = runner.invoke(app, ["submodule", "pr", "feat-foo", "--open"])

    assert result.exit_code == 0, result.output
    browser.assert_called_once_with(tmp_path / "lib/a", 12)
    publish.assert_not_called()


def test_update_flag_on_detached_submodule_without_branch_warns(mocker, tmp_path):
    """A previously-published submodule that is now detached, with no
    --branch given, has no source branch to regenerate a description from or
    a state to toggle. --ready must warn, not silently no-op."""
    from jailbee.pr_flow import PrRecord
    from jailbee.submodule_pr import SubPublishResult

    _setup(
        mocker,
        tmp_path,
        candidates=[_candidate(branch=None)],
        state_record=PrRecord(number=12, head="user/x", author=True, adopted=False),
    )
    mocker.patch(
        "jailbee.submodule_pr.publish_submodule_branch",
        return_value=SubPublishResult(src_ref="r", publish_name="user/x", forced=False),
    )
    mocker.patch("jailbee.pr.view_existing_pr", return_value=_created(12, True))
    apply_updates = mocker.patch("jailbee.pr_flow.apply_pr_updates")

    result = runner.invoke(app, ["submodule", "pr", "feat-foo", "--ready"])

    assert result.exit_code == 0, result.output
    apply_updates.assert_not_called()
    assert "--ready" in result.output
    assert "detached" in result.output.lower() or "--branch" in result.output


def test_update_on_detached_submodule_without_branch_and_no_flags_does_not_crash(mocker, tmp_path):
    """Same detached-and-unresolved-branch state as above, but with no
    --description/--title/--body/--ready flag: nothing was silently ignored,
    so no warning — and it must not crash `render_pr_outcome`'s
    `update is not None` assertion either."""
    from jailbee.pr_flow import PrRecord
    from jailbee.submodule_pr import SubPublishResult

    _setup(
        mocker,
        tmp_path,
        candidates=[_candidate(branch=None)],
        state_record=PrRecord(number=12, head="user/x", author=True, adopted=False),
    )
    mocker.patch(
        "jailbee.submodule_pr.publish_submodule_branch",
        return_value=SubPublishResult(src_ref="r", publish_name="user/x", forced=False),
    )
    mocker.patch("jailbee.pr.view_existing_pr", return_value=_created(12, True))
    apply_updates = mocker.patch("jailbee.pr_flow.apply_pr_updates")

    result = runner.invoke(app, ["submodule", "pr", "feat-foo"])

    assert result.exit_code == 0, result.output
    apply_updates.assert_not_called()
    assert "--ready" not in result.output
    assert "--description" not in result.output
