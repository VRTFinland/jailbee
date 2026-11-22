"""Tests for resolving the host's authoritative comparison target."""

from jailbee.host_target import resolve_target


def test_resolve_target_prefers_local_branch_when_head_is_detached(mocker, tmp_path):
    rev_parse = mocker.patch("jailbee.git.rev_parse")
    rev_parse.side_effect = lambda _root, ref: {
        "refs/heads/vaaka-combined": "local-sha",
        "refs/remotes/origin/vaaka-combined": "tracking-sha",
    }.get(ref)
    is_ancestor = mocker.patch("jailbee.git.is_ancestor", return_value=False)

    snapshot = resolve_target(tmp_path, "vaaka-combined", "origin")

    assert snapshot.sha == "local-sha"
    assert snapshot.source == "local"
    assert snapshot.tracking_relation == "diverged"
    assert snapshot.upstream_ref == "refs/remotes/origin/vaaka-combined"
    assert is_ancestor.call_count == 2


def test_resolve_target_uses_tracking_when_local_branch_is_absent(mocker, tmp_path):
    rev_parse = mocker.patch("jailbee.git.rev_parse")
    rev_parse.side_effect = lambda _root, ref: {
        "refs/remotes/origin/vaaka-combined": "tracking-sha"
    }.get(ref)

    snapshot = resolve_target(tmp_path, "vaaka-combined", "origin")

    assert snapshot.sha == "tracking-sha"
    assert snapshot.source == "tracking"
    assert snapshot.tracking_relation == "unavailable"


def test_resolve_target_is_unavailable_when_no_ref_resolves(mocker, tmp_path):
    rev_parse = mocker.patch("jailbee.git.rev_parse", return_value=None)

    snapshot = resolve_target(tmp_path, "vaaka-combined", "origin")

    assert snapshot.source == "unavailable"
    assert snapshot.sha is None
    assert snapshot.tracking_relation == "unavailable"
    assert rev_parse.call_count == 2


def test_resolve_target_marks_tracking_ahead_when_local_is_ancestor(mocker, tmp_path):
    mocker.patch(
        "jailbee.git.rev_parse",
        side_effect=["local-sha", "tracking-sha"],
    )
    mocker.patch("jailbee.git.is_ancestor", side_effect=[True, False])

    snapshot = resolve_target(tmp_path, "vaaka-combined", "origin")

    assert snapshot.tracking_relation == "tracking-ahead"


def test_resolve_target_marks_local_ahead_when_tracking_is_ancestor(mocker, tmp_path):
    mocker.patch(
        "jailbee.git.rev_parse",
        side_effect=["local-sha", "tracking-sha"],
    )
    mocker.patch("jailbee.git.is_ancestor", side_effect=[False, True])

    snapshot = resolve_target(tmp_path, "vaaka-combined", "origin")

    assert snapshot.tracking_relation == "local-ahead"


def test_resolve_target_marks_equal_refs(mocker, tmp_path):
    mocker.patch("jailbee.git.rev_parse", return_value="same-sha")

    snapshot = resolve_target(tmp_path, "vaaka-combined", "origin")

    assert snapshot.tracking_relation == "equal"


def test_resolve_target_marks_relation_unavailable_on_ancestor_error(mocker, tmp_path):
    mocker.patch("jailbee.git.rev_parse", side_effect=["local-sha", "tracking-sha"])
    mocker.patch("jailbee.git.is_ancestor", side_effect=[None, None])

    snapshot = resolve_target(tmp_path, "vaaka-combined", "origin")

    assert snapshot.source == "local"
    assert snapshot.sha == "local-sha"
    assert snapshot.tracking_relation == "unavailable"


# ---- resolve_sync_target: the ref a container's DIFF/↑/↓ compare against ----


def _host_snapshot(sha: str | None = "host-sha"):
    from jailbee.host_target import TargetSnapshot

    return TargetSnapshot("main", sha, "local", "refs/remotes/origin/main", "equal")


def test_sync_target_of_a_plain_container_is_the_host_branch(mocker, tmp_path):
    from jailbee.host_target import resolve_sync_target

    rev_parse = mocker.patch("jailbee.git.rev_parse")

    target = resolve_sync_target(tmp_path, None, _host_snapshot())

    assert (target.kind, target.sha, target.label, target.fallback) == (
        "host",
        "host-sha",
        "main",
        False,
    )
    rev_parse.assert_not_called()


def test_sync_target_of_a_pr_bound_container_is_the_pr_head(mocker, tmp_path):
    from jailbee.host_target import resolve_sync_target

    rev_parse = mocker.patch("jailbee.git.rev_parse", return_value="pr-sha")

    target = resolve_sync_target(tmp_path, 12, _host_snapshot())

    rev_parse.assert_called_once_with(tmp_path, "refs/jailbee/pr/12/head")
    assert (target.kind, target.sha, target.label, target.ref, target.fallback) == (
        "pr",
        "pr-sha",
        "PR #12",
        "refs/jailbee/pr/12/head",
        False,
    )


def test_pr_bound_container_without_a_pr_head_ref_falls_back_to_the_host_branch(mocker, tmp_path):
    from jailbee.host_target import resolve_sync_target

    mocker.patch("jailbee.git.rev_parse", return_value=None)

    target = resolve_sync_target(tmp_path, 12, _host_snapshot())

    assert (target.kind, target.sha, target.label, target.fallback) == (
        "host",
        "host-sha",
        "main",
        True,
    )


def test_pr_bound_container_of_an_unloadable_repo_falls_back_without_reading_refs(mocker):
    from jailbee.host_target import resolve_sync_target

    rev_parse = mocker.patch("jailbee.git.rev_parse")

    target = resolve_sync_target(None, 12, _host_snapshot(None))

    assert (target.kind, target.sha, target.fallback) == ("host", None, True)
    rev_parse.assert_not_called()
