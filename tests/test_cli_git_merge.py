"""CLI tests for `jailbee git merge` — merge one container into another.

The summary is the requirement: a multi-source run that stops halfway is only
usable if the user can see the boundary, so every exit path prints what landed,
what stopped it, what was not attempted, and how to resume.

Assertions go through `flat_output` because Rich wraps at 80 columns off a
terminal, and a wrapped line is still the right line.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.submodules import GitlinkResolution
from jailbee.sync import (
    ConflictReport,
    LocalBranchUpdate,
    MergeConflictError,
    MergeInContainerResult,
    PushResult,
    SyncError,
)
from tests.conftest import flat_output

runner = CliRunner()


@pytest.fixture
def merge_repo(tmp_path, mocker, make_cfg):
    """A real `Config` plus one MagicMock `Incus`; every name resolves to itself.

    `lifecycle.short_name` is deliberately *not* mocked: it is a pure prefix
    strip, so a real `cfg` exercises the full -> short conversion the command
    must do before calling into `sync` (which re-resolves short names itself).
    """
    cfg = make_cfg(tmp_path / "sampleapp")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.MagicMock()
    mocker.patch(
        "jailbee.cli._resolve_existing",
        side_effect=lambda _cfg, name: (incus, f"sampleapp-{name}"),
    )
    return cfg, incus


def _result(
    *,
    source: str = "c1",
    branch: str = "feat/a",
    container_branch: str = "feat/target",
    fast_forward_only: bool = False,
    head_oid: str = "deadbee1234567",
    local_branch: LocalBranchUpdate | None = None,
) -> MergeInContainerResult:
    """A `merge_container_into_container` result, shaped as `sync` shapes it.

    Real dataclasses rather than mocks: the renderers read
    `push.container_ref` / `push.local_branch` / `fast_forward_only`, and a
    MagicMock would answer every one of them with something truthy.
    """
    return MergeInContainerResult(
        push=PushResult(
            source=branch,
            source_ref=f"refs/jailbee/{source}/{branch}",
            container_ref=f"refs/jailbee/from/{source}/{branch}",
            old_oid=None,
            new_oid="1111111aaaaaaa",
            local_branch=local_branch,
        ),
        container_branch=container_branch,
        fast_forward_only=fast_forward_only,
        head_oid=head_oid,
    )


def _conflict_report() -> ConflictReport:
    return ConflictReport(
        resolution=GitlinkResolution(resolved=[], unresolved=[]),
        nongitlink=["src/app.py"],
        branch="feat/b",
        location="jailbee shell c4",
    )


# --- the happy path ------------------------------------------------------------


def test_git_merge_processes_sources_in_order(merge_repo, mocker):
    """Each source is merged into the target, one at a time, in the order given."""
    cfg, incus = merge_repo
    called = mocker.patch(
        "jailbee.sync.merge_container_into_container", return_value=_result()
    )

    result = runner.invoke(app, ["git", "merge", "c1", "c2", "--into", "c4"])

    assert result.exit_code == 0, result.output
    assert [c.args[2] for c in called.call_args_list] == ["c1", "c2"]
    assert all(c.args[3] == "c4" for c in called.call_args_list)
    # cfg and incus are threaded through, and the target's Incus is the one
    # every merge runs on.
    assert all(c.args[0] is cfg and c.args[1] is incus for c in called.call_args_list)
    assert all(c.kwargs == {"branch": None, "plain": False} for c in called.call_args_list)
    assert "merged into c4: c1, c2" in flat_output(result.output)


def test_git_merge_reports_the_merge_it_ran(merge_repo, mocker):
    """A non-plain success names the target branch, the mode and the new HEAD."""
    mocker.patch(
        "jailbee.sync.merge_container_into_container",
        return_value=_result(fast_forward_only=True, head_oid="abc9999fedcba"),
    )

    result = runner.invoke(app, ["git", "merge", "c1", "--into", "c4"])

    assert result.exit_code == 0, result.output
    flat = flat_output(result.output)
    assert "c1: merged 'feat/a' into 'feat/target' in 'c4' (fast-forward)" in flat
    assert "HEAD now at abc9999" in flat


def test_git_merge_passes_the_branch_override_through(merge_repo, mocker):
    called = mocker.patch(
        "jailbee.sync.merge_container_into_container", return_value=_result(branch="feat/x")
    )

    result = runner.invoke(app, ["git", "merge", "c1", "--into", "c4", "-b", "feat/x"])

    assert result.exit_code == 0, result.output
    assert called.call_args.kwargs["branch"] == "feat/x"


# --- --plain must not claim a merge --------------------------------------------


def test_git_merge_plain_reports_a_transport_not_a_merge(merge_repo, mocker):
    """`--plain` runs no merge at all, so the summary must not say "merged"."""
    called = mocker.patch(
        "jailbee.sync.merge_container_into_container", return_value=_result()
    )

    result = runner.invoke(app, ["git", "merge", "c1", "c2", "--into", "c4", "--plain"])

    assert result.exit_code == 0, result.output
    assert all(c.kwargs["plain"] is True for c in called.call_args_list)
    flat = flat_output(result.output)
    assert "transported into c4 (no merge run): c1, c2" in flat
    assert "merged into c4" not in flat
    # `fast_forward_only` and `head_oid` are not meaningful for a plain result
    # (see MergeInContainerResult), so neither may be rendered from one.
    assert "fast-forward" not in flat
    assert "HEAD now at" not in flat
    assert "refs/jailbee/from/c1/feat/a" in flat


# --- stopping at the first failure ---------------------------------------------


def test_git_merge_stops_at_the_first_conflict_and_says_what_landed(merge_repo, mocker):
    report = _conflict_report()

    def side_effect(cfg, incus, source, target, *, branch=None, plain=False):
        if source == "c2":
            raise MergeConflictError("conflicts", report=report)
        return _result(source=source)

    called = mocker.patch(
        "jailbee.sync.merge_container_into_container", side_effect=side_effect
    )

    result = runner.invoke(app, ["git", "merge", "c1", "c2", "c3", "--into", "c4"])

    assert result.exit_code == 1
    # c3 was never attempted — the run stops at the conflict.
    assert [c.args[2] for c in called.call_args_list] == ["c1", "c2"]
    flat = flat_output(result.output)
    assert "merged into c4: c1" in flat
    assert "stopped at c2" in flat
    assert "not attempted: c3" in flat
    # The resume recipe, so the user knows how to continue. The conflicted
    # source is re-run too: finishing the merge by hand makes that a no-op.
    assert "jailbee shell c4" in flat
    assert "jailbee git merge c2 c3 --into c4" in flat


def test_git_merge_renders_the_submodule_conflict_report(merge_repo, mocker):
    """The conflict's own `ConflictReport` block is printed, not just the message."""
    mocker.patch(
        "jailbee.sync.merge_container_into_container",
        side_effect=MergeConflictError("conflicts", report=_conflict_report()),
    )

    result = runner.invoke(app, ["git", "merge", "c1", "--into", "c4"])

    assert result.exit_code == 1
    flat = flat_output(result.output)
    assert "src/app.py" in flat
    assert "superproject left in merge state" in flat
    # Nothing landed, and the summary says so rather than staying silent.
    assert "merged into c4: nothing" in flat


def test_git_merge_reports_a_plain_failure_without_the_conflict_recipe(merge_repo, mocker):
    """A `SyncError` is not a conflict: there is nothing to resolve in the target."""
    mocker.patch(
        "jailbee.sync.merge_container_into_container",
        side_effect=[_result(source="c1"), SyncError("Container 'c2' is not running.")],
    )

    result = runner.invoke(app, ["git", "merge", "c1", "c2", "c3", "--into", "c4"])

    assert result.exit_code == 1
    flat = flat_output(result.output)
    assert "stopped at c2: Container 'c2' is not running." in flat
    assert "not attempted: c3" in flat
    assert "After fixing the problem, continue with:" in flat
    assert "jailbee git merge c2 c3 --into c4" in flat
    assert "git add" not in flat


def test_git_merge_resume_recipe_carries_the_flags_in_effect(merge_repo, mocker):
    """A `--plain` run's recipe must not tell the user to run a real merge."""
    mocker.patch(
        "jailbee.sync.merge_container_into_container",
        side_effect=[_result(source="c1"), SyncError("boom")],
    )

    result = runner.invoke(app, ["git", "merge", "c1", "c2", "c3", "--into", "c4", "--plain"])

    assert result.exit_code == 1
    assert "jailbee git merge c2 c3 --into c4 --plain" in flat_output(result.output)


def test_git_merge_resume_recipe_carries_the_branch_override(merge_repo, mocker):
    """With `-b`, the recipe must read the same branch the failed run did."""
    mocker.patch(
        "jailbee.sync.merge_container_into_container",
        side_effect=SyncError("boom"),
    )

    result = runner.invoke(app, ["git", "merge", "c1", "--into", "c4", "-b", "feat/x"])

    assert result.exit_code == 1
    assert "jailbee git merge c1 --into c4 -b feat/x" in flat_output(result.output)


# --- the two outcomes that used to surface as raw git text ---------------------


def test_git_merge_explains_a_same_name_ff_only_refusal(merge_repo, mocker):
    """git's "Not possible to fast-forward" says nothing about two containers."""
    mocker.patch(
        "jailbee.sync.merge_container_into_container",
        side_effect=SyncError(
            "git merge failed in container 'c4': `incus exec c4` failed "
            "(exit 128): fatal: Not possible to fast-forward, aborting."
        ),
    )

    result = runner.invoke(app, ["git", "merge", "c1", "--into", "c4"])

    assert result.exit_code == 1
    flat = flat_output(result.output)
    assert "the branch read from 'c1' is the one 'c4' has checked out" in flat
    assert "--ff-only" in flat
    assert "refs/jailbee/from/c1/<branch>" in flat
    # git's own words are still reported, not replaced by the diagnosis.
    assert "Not possible to fast-forward" in flat


def test_git_merge_hint_is_silent_for_any_other_failure(merge_repo, mocker):
    mocker.patch(
        "jailbee.sync.merge_container_into_container",
        side_effect=SyncError("git merge failed in container 'c4': CONFLICT (content)"),
    )

    result = runner.invoke(app, ["git", "merge", "c1", "--into", "c4"])

    assert result.exit_code == 1
    assert "--ff-only" not in flat_output(result.output)


def test_git_merge_warns_when_the_targets_own_branch_diverged(merge_repo, mocker):
    """The relay also fast-forwards the target's `refs/heads/<branch>`.

    A "diverged" outcome there is carried in `PushResult.local_branch` and is
    reported by nothing else: the target keeps a stale branch of that name.
    """
    mocker.patch(
        "jailbee.sync.merge_container_into_container",
        return_value=_result(
            local_branch=LocalBranchUpdate(
                branch="feat/a", status="diverged", old_oid="old1234abcd", new_oid="new7654dcba"
            )
        ),
    )

    result = runner.invoke(app, ["git", "merge", "c1", "--into", "c4"])

    assert result.exit_code == 0, result.output
    flat = flat_output(result.output)
    assert "container's local 'feat/a' (old1234) has diverged" in flat
    assert "jailbee shell c4" in flat
    # The remedy names the ref the objects really landed on — the relay writes
    # `from/<source>/`, never `refs/jailbee/host/`.
    assert "refs/jailbee/from/c1/feat/a" in flat
    assert "refs/jailbee/host/" not in flat


@pytest.mark.parametrize("status", ["up-to-date", "checked-out"])
def test_git_merge_stays_silent_about_a_benign_local_branch(status, merge_repo, mocker):
    mocker.patch(
        "jailbee.sync.merge_container_into_container",
        return_value=_result(
            local_branch=LocalBranchUpdate(
                branch="feat/a", status=status, old_oid=None, new_oid="new7654dcba"
            )
        ),
    )

    result = runner.invoke(app, ["git", "merge", "c1", "--into", "c4"])

    assert result.exit_code == 0, result.output
    assert "local 'feat/a'" not in flat_output(result.output)


# --- argument validation -------------------------------------------------------


def test_git_merge_rejects_branch_override_with_several_sources(merge_repo, mocker):
    """`-b` reads one branch, so it cannot describe several sources."""
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge", "c1", "c2", "--into", "c4", "-b", "feat/x"])

    assert result.exit_code == 2
    assert "single source" in flat_output(result.output)
    called.assert_not_called()


def test_git_merge_requires_into(merge_repo, mocker):
    """Nothing is inferred: without `--into` the command is a usage error."""
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge", "c1"])

    assert result.exit_code == 2
    combined = flat_output((result.output or "") + (result.stderr or ""))
    assert "--into" in combined
    assert "Missing option" in combined
    called.assert_not_called()


def test_git_merge_requires_at_least_one_source(merge_repo, mocker):
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge", "--into", "c4"])

    assert result.exit_code == 2
    called.assert_not_called()
