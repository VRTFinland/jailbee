"""Tests for the pre-destroy risk assessment.

Fully mocked: `has_commit` is the only host-git call and is patched in every
test, so nothing here runs git or touches a real repo.
"""

from __future__ import annotations

from jailbee.git_status import GitStatus, SubmoduleChange
from jailbee.lifecycle import ContainerInfo
from tests.conftest import make_cfg


def _ci(**status_kwargs) -> ContainerInfo:
    """A probed, clean, clone-mode container — the shape `lifecycle` builds.

    ``repo`` is set because every production construction site sets it; the
    guard renders labels through ``ContainerInfo.display_name``, so a bare
    ``ContainerInfo`` without it would exercise a state no real listing
    produces.
    """
    base = {"wt": "clean", "ahead_diff": "clean", "ahead_count": "0", "conflict": "ok"}
    return ContainerInfo(
        name="myrepo-feat-x",
        state="Running",
        network="strict",
        ip=None,
        memory_limit=None,
        repo="myrepo",
        git_status=GitStatus(**{**base, **status_kwargs}),
    )


def test_clean_container_is_not_at_risk(tmp_path, mocker):
    from jailbee.destroy_guard import assess

    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)

    assert assess(make_cfg(tmp_path), _ci()) is None


def test_dirty_working_tree_alone_is_a_risk(tmp_path, mocker):
    from jailbee.destroy_guard import assess

    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)

    summary = assess(make_cfg(tmp_path), _ci(wt="+12 -3"))

    assert summary is not None
    assert any("working tree" in r for r in summary.reasons)
    assert "+12 -3" in summary.line


def test_unknown_working_tree_is_not_treated_as_dirty(tmp_path, mocker):
    """`?` and `—` mean 'not measured', which is handled by the caller's
    unknown-status note — not by claiming the tree is dirty."""
    from jailbee.destroy_guard import assess

    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)
    cfg = make_cfg(tmp_path)

    assert assess(cfg, _ci(wt="?")) is None
    assert assess(cfg, _ci(wt="—")) is None


def test_changed_submodule_is_a_risk(tmp_path, mocker):
    from jailbee.destroy_guard import assess

    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)
    ci = _ci(
        submodules=(
            SubmoduleChange(
                path="sub/bar",
                ahead_ins=40,
                ahead_del=2,
                ahead_commits=1,
                wt_ins=0,
                wt_del=0,
                status="modified",
            ),
        )
    )

    summary = assess(make_cfg(tmp_path), ci)

    assert summary is not None
    assert summary.reasons == ("submodule sub/bar (committed +40 -2)",)


def test_dirty_only_submodule_reports_the_uncommitted_numbers(tmp_path, mocker):
    """`_parse_submodules` admits an entry on a working-tree delta alone, so
    printing only the committed numbers rendered it as `+0 -0` — figures
    saying "nothing changed" while being the sole reason for the prompt."""
    from jailbee.destroy_guard import assess

    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)
    ci = _ci(
        submodules=(
            SubmoduleChange(
                path="sub/bar",
                ahead_ins=0,
                ahead_del=0,
                ahead_commits=0,
                wt_ins=1,
                wt_del=0,
                status="modified",
            ),
        )
    )

    summary = assess(make_cfg(tmp_path), ci)

    assert summary is not None
    assert summary.reasons == ("submodule sub/bar (uncommitted +1 -0)",)
    assert "+0 -0" not in summary.line


def test_added_and_removed_submodules_are_named_not_numbered(tmp_path, mocker):
    from jailbee.destroy_guard import assess

    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)
    cfg = make_cfg(tmp_path)

    def _sub(status: str) -> SubmoduleChange:
        return SubmoduleChange(
            path="sub/bar",
            ahead_ins=0,
            ahead_del=0,
            ahead_commits=0,
            wt_ins=0,
            wt_del=0,
            status=status,
        )

    added = assess(cfg, _ci(submodules=(_sub("new"),)))
    removed = assess(cfg, _ci(submodules=(_sub("removed"),)))

    assert added is not None and added.reasons == ("submodule sub/bar (added)",)
    assert removed is not None and removed.reasons == ("submodule sub/bar (removed)",)


def test_submodule_with_committed_and_uncommitted_work_names_both(tmp_path, mocker):
    from jailbee.destroy_guard import assess

    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)
    ci = _ci(
        submodules=(
            SubmoduleChange(
                path="sub/bar",
                ahead_ins=40,
                ahead_del=2,
                ahead_commits=1,
                wt_ins=3,
                wt_del=4,
                status="modified",
            ),
        )
    )

    summary = assess(make_cfg(tmp_path), ci)

    assert summary is not None
    assert summary.reasons == ("submodule sub/bar (committed +40 -2, uncommitted +3 -4)",)


def test_submodule_with_only_a_commit_count_reports_the_count(tmp_path, mocker):
    """An empty-diff commit inside a submodule still moved the pointer."""
    from jailbee.destroy_guard import assess

    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)
    ci = _ci(
        submodules=(
            SubmoduleChange(
                path="sub/bar",
                ahead_ins=0,
                ahead_del=0,
                ahead_commits=2,
                wt_ins=0,
                wt_del=0,
                status="modified",
            ),
        )
    )

    summary = assess(make_cfg(tmp_path), ci)

    assert summary is not None
    assert summary.reasons == ("submodule sub/bar (2 commits)",)


def test_stranded_commits_are_a_risk(tmp_path, mocker):
    """Ahead of base, absent from the host, not on any remote."""
    from jailbee.destroy_guard import assess

    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)

    summary = assess(
        make_cfg(tmp_path), _ci(ahead_count="3", head_sha="abc123", remote_contained=False)
    )

    assert summary is not None
    assert any("3 commits" in r for r in summary.reasons)


def test_commits_already_on_the_host_are_not_a_risk(tmp_path, mocker):
    """A previous `gie git pull` moved the work — destroying loses nothing."""
    from jailbee.destroy_guard import assess

    has = mocker.patch("jailbee.destroy_guard.has_commit", return_value=True)

    result = assess(
        make_cfg(tmp_path), _ci(ahead_count="3", head_sha="abc123", remote_contained=False)
    )

    assert result is None
    assert has.call_args.args[1] == "abc123"


def test_commits_behind_a_remote_ref_are_not_a_risk(tmp_path, mocker):
    from jailbee.destroy_guard import assess

    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)

    result = assess(
        make_cfg(tmp_path), _ci(ahead_count="3", head_sha="abc123", remote_contained=True)
    )

    assert result is None


def test_unknown_remote_containment_still_counts_as_stranded(tmp_path, mocker):
    """`remote_contained is None` means unknown; unknown is not safety."""
    from jailbee.destroy_guard import assess

    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)

    summary = assess(
        make_cfg(tmp_path), _ci(ahead_count="3", head_sha="abc123", remote_contained=None)
    )

    assert summary is not None


def test_unknown_ahead_count_still_flags_stranded_commits(tmp_path, mocker):
    """`ahead_count == "?"` means the probe could not resolve the container's
    base (the PR-review case the `?` exists for) — not that the commits are
    safe. The count only feeds the message; `head_sha` answers the question."""
    from jailbee.destroy_guard import assess

    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)

    summary = assess(
        make_cfg(tmp_path), _ci(ahead_count="?", head_sha="abc123", remote_contained=False)
    )

    assert summary is not None
    assert summary.reasons == ("commits not on the host (count unknown)",)


def test_unknown_ahead_count_with_the_commit_on_the_host_is_not_a_risk(tmp_path, mocker):
    """The safety valve for the rule above: a container parked on a commit the
    host already holds (a fresh clone, or after `gie git pull`) must stay
    silent even with an unmeasurable count — `has_commit` tests the object,
    not a ref, so this is the normal state of a clean container."""
    from jailbee.destroy_guard import assess

    has = mocker.patch("jailbee.destroy_guard.has_commit", return_value=True)

    assert (
        assess(make_cfg(tmp_path), _ci(ahead_count="?", head_sha="abc123", remote_contained=False))
        is None
    )
    assert has.call_args.args[1] == "abc123"


def test_unknown_ahead_count_on_a_remote_contained_commit_is_not_a_risk(tmp_path, mocker):
    from jailbee.destroy_guard import assess

    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)

    assert (
        assess(make_cfg(tmp_path), _ci(ahead_count="?", head_sha="abc123", remote_contained=True))
        is None
    )


def test_unknown_ahead_count_without_a_head_sha_adds_no_commit_reason(tmp_path, mocker):
    """Nothing identifies the commit, so there is nothing to look up: no
    `has_commit` call and no commit reason. (`wt` is dirty here only to keep
    the container out of the compound-unknown branch.)"""
    from jailbee.destroy_guard import assess

    has = mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)

    summary = assess(make_cfg(tmp_path), _ci(wt="+1 -0", ahead_count="?", head_sha=""))

    assert summary is not None
    assert summary.reasons == ("working tree +1 -0",)
    has.assert_not_called()


def test_zero_ahead_count_never_consults_the_host(tmp_path, mocker):
    """The unknown-count branch must not widen into every clean container: a
    measured count of 0 short-circuits before any `has_commit` call."""
    from jailbee.destroy_guard import assess

    has = mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)

    assert assess(make_cfg(tmp_path), _ci(ahead_count="0", head_sha="abc123")) is None
    has.assert_not_called()


def test_no_git_status_returns_none(tmp_path, mocker):
    """`gie ls --no-git`, or a failed probe: the guard cannot assess. The
    caller adds its own 'git status unknown' note; silence is not safety."""
    from jailbee.destroy_guard import assess

    has = mocker.patch("jailbee.destroy_guard.has_commit")
    ci = ContainerInfo(
        name="myrepo-feat-x",
        state="Running",
        network="strict",
        ip=None,
        memory_limit=None,
    )

    assert assess(make_cfg(tmp_path), ci) is None
    has.assert_not_called()


def test_fully_failed_probe_is_a_risk_not_silence(tmp_path, mocker):
    """A fully-failed `incus exec` on a Running container leaves every field
    unmeasured — the exact shape `probe_container_git` falls back to on
    `IncusError`/timeout/malformed output. That must not read as "clean"."""
    from jailbee.destroy_guard import assess

    has = mocker.patch("jailbee.destroy_guard.has_commit")
    ci = _ci(wt="?", ahead_diff="?", ahead_count="?", conflict="?")

    summary = assess(make_cfg(tmp_path), ci)

    assert summary is not None
    assert any("inspect" in r for r in summary.reasons)
    has.assert_not_called()


def test_stopped_container_without_git_status_is_not_flagged(tmp_path, mocker):
    """Stopped containers are never probed (`git_status` stays None) — the
    new compound-unknown reason must not widen into a warning on every one."""
    from jailbee.destroy_guard import assess

    has = mocker.patch("jailbee.destroy_guard.has_commit")
    ci = ContainerInfo(
        name="myrepo-feat-x",
        state="Stopped",
        network=None,
        ip=None,
        memory_limit=None,
    )

    assert assess(make_cfg(tmp_path), ci) is None
    has.assert_not_called()


def test_line_joins_every_reason(tmp_path, mocker):
    from jailbee.destroy_guard import assess

    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)
    ci = _ci(
        wt="+12 -3",
        ahead_count="3",
        head_sha="abc123",
        remote_contained=False,
        submodules=(
            SubmoduleChange(
                path="sub/bar",
                ahead_ins=40,
                ahead_del=2,
                ahead_commits=1,
                wt_ins=0,
                wt_del=0,
                status="modified",
            ),
        ),
    )

    summary = assess(make_cfg(tmp_path), ci)

    assert summary is not None
    assert summary.line.startswith("feat-x: ")
    assert summary.line.count(" · ") == len(summary.reasons) - 1


def _bare(mode: str = "clone", state: str = "Stopped") -> ContainerInfo:
    return ContainerInfo(
        name="myrepo-feat-x",
        state=state,
        network=None,
        ip=None,
        memory_limit=None,
        repo="myrepo",
        mode=mode,
    )


def test_status_is_unknown_for_an_unprobed_clone_container() -> None:
    from jailbee.destroy_guard import status_is_unknown

    assert status_is_unknown(_bare()) is True


def test_status_is_not_unknown_for_a_mount_container() -> None:
    """Mount mode is "not applicable", not "unknown": the working tree *is*
    the host's directory and survives the destroy, so the shared
    "may discard uncommitted work" note would be provably false."""
    from jailbee.destroy_guard import status_is_unknown

    assert status_is_unknown(_bare(mode="mount", state="Running")) is False


def test_status_is_not_unknown_once_probed(tmp_path) -> None:
    from jailbee.destroy_guard import status_is_unknown

    assert status_is_unknown(_ci()) is False


def test_unknown_status_warning_names_every_container() -> None:
    from jailbee.destroy_guard import unknown_status_warning

    msg = unknown_status_warning(["feat-x", "feat-y"])

    assert "feat-x, feat-y" in msg
    assert "git status unknown" in msg


def test_unknown_status_warning_uses_plural_pronoun_for_several_containers() -> None:
    from jailbee.destroy_guard import unknown_status_warning

    msg = unknown_status_warning(["feat-x", "feat-y"])

    assert "could not measure them" in msg
    assert "could not measure it" not in msg


def test_unknown_status_warning_uses_singular_pronoun_for_one_container() -> None:
    from jailbee.destroy_guard import unknown_status_warning

    msg = unknown_status_warning(["feat-x"])

    assert "could not measure it" in msg
    assert "could not measure them" not in msg
