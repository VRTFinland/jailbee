"""CLI tests for `jailbee git merge` — merge one container into another.

The summary is the requirement: a multi-source run that stops halfway is only
usable if the user can see the boundary, so every exit path prints what landed,
what stopped it, what was not attempted, and how to resume.

Assertions go through `flat_output` because Rich wraps at 80 columns off a
terminal, and a wrapped line is still the right line.
"""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.lifecycle import ContainerInfo
from jailbee.submodules import GitlinkResolution
from jailbee.sync import (
    ConflictReport,
    LocalBranchUpdate,
    MergeConflictError,
    MergeInContainerResult,
    PushResult,
    SubmoduleMove,
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
    submodule_moves: tuple[SubmoduleMove, ...] = (),
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
        submodule_moves=submodule_moves,
    )


def _move(path: str = "deps/libfoo") -> SubmoduleMove:
    return SubmoduleMove(
        path=path,
        old_sha="a" * 40,
        new_sha="b" * 40,
        status="modified",
        commits=3,
        ins=12,
        dels=5,
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
    called = mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge", "c1", "c2", "--into", "c4"])

    assert result.exit_code == 0, result.output
    assert [c.args[2] for c in called.call_args_list] == ["c1", "c2"]
    assert all(c.args[3] == "c4" for c in called.call_args_list)
    # cfg and incus are threaded through.
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


def test_git_merge_prints_the_submodule_moves_of_each_source(merge_repo, mocker):
    """A gitlink that moved must not be buried in the superproject's diff.

    `jailbee git pull` prints this block; without it the cross-container merge
    is the one path that moves submodule pointers silently. Each source gets
    its own block, since each is a separate merge commit in the target.
    """

    def side_effect(cfg, incus, source, target, *, branch=None, plain=False):
        return _result(source=source, submodule_moves=(_move(f"deps/{source}-sub"),))

    mocker.patch("jailbee.sync.merge_container_into_container", side_effect=side_effect)

    result = runner.invoke(app, ["git", "merge", "c1", "c2", "--into", "c4"])

    assert result.exit_code == 0, result.output
    flat = flat_output(result.output)
    assert flat.count("── Submodules") == 2
    assert "deps/c1-sub" in flat
    assert "deps/c2-sub" in flat
    assert "(3 commits, +12 -5)" in flat


def test_git_merge_prints_no_submodule_block_when_nothing_moved(merge_repo, mocker):
    """An empty report is not printed as an empty block."""
    mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge", "c1", "--into", "c4"])

    assert result.exit_code == 0, result.output
    assert "── Submodules" not in flat_output(result.output)


# --- --plain must not claim a merge --------------------------------------------


def test_git_merge_plain_reports_a_transport_not_a_merge(merge_repo, mocker):
    """`--plain` runs no merge at all, so the summary must not say "merged"."""
    called = mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

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

    called = mocker.patch("jailbee.sync.merge_container_into_container", side_effect=side_effect)

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
    assert "resolve the conflict, git add, git commit" in flat
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
    flat = flat_output(result.output)
    assert "jailbee git merge c1 --into c4 -b feat/x" in flat
    # Nothing followed the failing source, so there is no "not attempted" line
    # to print — an empty one would be noise, not information.
    assert "not attempted" not in flat


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


def test_git_merge_resolves_every_source_before_merging_any(merge_repo, mocker):
    """An unresolvable source name must fail before anything lands in the target.

    `_resolve_existing` exits the process itself (`error` + `typer.Exit(1)`),
    so resolving inside the merge loop would abandon a half-applied
    multi-source run — killed after `c1` had already landed and *past* the
    summary, so the user is told nothing about what landed, what stopped it or
    how to resume. Resolution is all-or-nothing instead, which is stronger than
    printing a summary: with nothing attempted, no summary is owed.
    """
    _cfg, incus = merge_repo

    def resolve(_cfg, name):
        if name == "c2typo":
            raise typer.Exit(1)  # what `_resolve_existing` does for an unknown name
        return (incus, f"sampleapp-{name}")

    mocker.patch("jailbee.cli._resolve_existing", side_effect=resolve)
    called = mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge", "c1", "c2typo", "c3", "--into", "c4"])

    assert result.exit_code == 1
    called.assert_not_called()


# --- several targets -----------------------------------------------------------


def test_git_merge_merges_every_source_into_every_target(merge_repo, mocker):
    """`--into` is repeatable: every target takes every source, target by target."""
    called = mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge", "c1", "c2", "--into", "t1", "--into", "t2"])

    assert result.exit_code == 0, result.output
    assert [(c.args[2], c.args[3]) for c in called.call_args_list] == [
        ("c1", "t1"),
        ("c2", "t1"),
        ("c1", "t2"),
        ("c2", "t2"),
    ]
    flat = flat_output(result.output)
    assert "merged into t1: c1, c2" in flat
    assert "merged into t2: c1, c2" in flat


def test_git_merge_carries_on_to_the_next_target_after_a_failure(merge_repo, mocker):
    """A target that stops must not take the targets after it down with it.

    Within one target the sources still stop at the first failure — merging on
    top of a conflicted tree is not safe — but the next target has its own
    working tree, untouched by that conflict, and is attempted in full.
    """

    def side_effect(cfg, incus, source, target, *, branch=None, plain=False):
        if (source, target) == ("c1", "t1"):
            raise MergeConflictError("conflicts", report=_conflict_report())
        return _result(source=source)

    called = mocker.patch("jailbee.sync.merge_container_into_container", side_effect=side_effect)

    result = runner.invoke(app, ["git", "merge", "c1", "c2", "--into", "t1", "--into", "t2"])

    assert result.exit_code == 1
    assert [(c.args[2], c.args[3]) for c in called.call_args_list] == [
        ("c1", "t1"),
        ("c1", "t2"),
        ("c2", "t2"),
    ]
    flat = flat_output(result.output)
    assert "merged into t1: nothing" in flat
    assert "not attempted: c2" in flat
    assert "merged into t2: c1, c2" in flat


def test_git_merge_rolls_up_what_each_target_ended_with(merge_repo, mocker):
    """With several targets the per-target blocks scroll away; the roll-up does not.

    It is the only place the user can read the state of every target at once:
    which ones are complete, which one stopped, on what, and what it never
    attempted.
    """

    def side_effect(cfg, incus, source, target, *, branch=None, plain=False):
        if (source, target) == ("c2", "t1"):
            raise SyncError("Container 'c2' is not running.")
        return _result(source=source)

    mocker.patch("jailbee.sync.merge_container_into_container", side_effect=side_effect)

    result = runner.invoke(app, ["git", "merge", "c1", "c2", "c3", "--into", "t1", "--into", "t2"])

    assert result.exit_code == 1
    flat = flat_output(result.output)
    assert "Summary: 1 of 2 targets complete" in flat
    # `flat_output` collapses runs of whitespace, so the column padding is not
    # what is asserted here — the words are.
    assert (
        "t1 stopped merged c1 — stopped at c2: Container 'c2' is not running. "
        "(c3 not attempted)" in flat
    )
    assert "t2 ok merged c1, c2, c3" in flat


def test_git_merge_roll_up_reports_a_plain_run_as_a_transport(merge_repo, mocker):
    """`--plain` merges nothing, so the roll-up must not claim a merge either."""
    mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge", "c1", "--into", "t1", "--into", "t2", "--plain"])

    assert result.exit_code == 0, result.output
    flat = flat_output(result.output)
    assert "t1 ok transported c1" in flat
    assert "merged c1" not in flat


def test_git_merge_into_a_single_target_prints_no_roll_up(merge_repo, mocker):
    """One target is already summarised by its own block; a roll-up would repeat it."""
    mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge", "c1", "c2", "--into", "c4"])

    assert result.exit_code == 0, result.output
    assert "targets complete" not in flat_output(result.output)


def test_git_merge_resolves_every_target_before_merging_any(merge_repo, mocker):
    """An unresolvable target name must fail before anything lands anywhere.

    Same all-or-nothing rule as the sources: `_resolve_existing` exits the
    process itself, and doing that from inside the loop would kill the run
    after an earlier target had already taken every source — past its summary,
    so the half-applied run would be reported to nobody.
    """
    _cfg, incus = merge_repo

    def resolve(_cfg, name):
        if name == "t2typo":
            raise typer.Exit(1)  # what `_resolve_existing` does for an unknown name
        return (incus, f"sampleapp-{name}")

    mocker.patch("jailbee.cli._resolve_existing", side_effect=resolve)
    called = mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge", "c1", "--into", "t1", "--into", "t2typo"])

    assert result.exit_code == 1
    called.assert_not_called()


def test_git_merge_branch_override_still_allows_several_targets(merge_repo, mocker):
    """`-b` constrains the sources to one; it says nothing about the targets."""
    called = mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(
        app, ["git", "merge", "c1", "--into", "t1", "--into", "t2", "-b", "feat/x"]
    )

    assert result.exit_code == 0, result.output
    assert [c.args[3] for c in called.call_args_list] == ["t1", "t2"]
    assert all(c.kwargs["branch"] == "feat/x" for c in called.call_args_list)


# --- argument validation -------------------------------------------------------


def test_git_merge_rejects_branch_override_with_several_sources(merge_repo, mocker):
    """`-b` reads one branch, so it cannot describe several sources."""
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge", "c1", "c2", "--into", "c4", "-b", "feat/x"])

    assert result.exit_code == 2
    assert "single source" in flat_output(result.output)
    called.assert_not_called()


def test_git_merge_refuses_a_container_named_at_both_ends(merge_repo, mocker):
    """A container cannot be its own merge source.

    The interactive path hides the rows the other end has taken, so this shape
    can only be typed — and it is refused whole, before anything is merged,
    rather than merging the legal pairs and leaving the user to work out which
    of them ran.
    """
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge", "c1", "c2", "--into", "c1", "--into", "t2"])

    assert result.exit_code == 2
    combined = flat_output((result.output or "") + (result.stderr or ""))
    assert "c1" in combined
    assert "into itself" in combined
    called.assert_not_called()


def test_git_merge_self_merge_guard_compares_resolved_names(merge_repo, mocker):
    """`c1` and `sampleapp-c1` are one container, so naming both ends is refused.

    The guard runs on the resolved short names rather than on what was typed;
    comparing the raw arguments would let the same container through under its
    other spelling.
    """
    _cfg, incus = merge_repo
    # The fixture's resolver prefixes unconditionally; the real one resolves
    # both spellings of one container to the same full name, which is the
    # behaviour the guard leans on.
    mocker.patch(
        "jailbee.cli._resolve_existing",
        side_effect=lambda _cfg, name: (
            incus,
            name if name.startswith("sampleapp-") else f"sampleapp-{name}",
        ),
    )
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge", "c1", "--into", "sampleapp-c1"])

    assert result.exit_code == 2
    assert "into itself" in flat_output((result.output or "") + (result.stderr or ""))
    called.assert_not_called()


def test_git_merge_off_a_tty_requires_the_target_explicitly(merge_repo, mocker):
    """Off a TTY nothing can be prompted for, so the omission is an error.

    A script must be told what is missing rather than made to hang for a
    choice it cannot make.
    """
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge", "c1"])

    assert result.exit_code == 1
    combined = flat_output((result.output or "") + (result.stderr or ""))
    assert "--into <target>" in combined
    assert "TTY" in combined
    called.assert_not_called()


def test_git_merge_off_a_tty_requires_the_sources_explicitly(merge_repo, mocker):
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge", "--into", "c4"])

    assert result.exit_code == 1
    combined = flat_output((result.output or "") + (result.stderr or ""))
    assert "<source>" in combined
    assert "TTY" in combined
    called.assert_not_called()


def test_git_merge_off_a_tty_names_both_missing_ends(merge_repo, mocker):
    """A bare `jailbee git merge` off a TTY names both halves, not just one."""
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge"])

    assert result.exit_code == 1
    combined = flat_output((result.output or "") + (result.stderr or ""))
    assert "<source>" in combined
    assert "--into <target>" in combined
    called.assert_not_called()


# --- interactive selection -----------------------------------------------------


def _info(short: str, *, mode: str = "clone", state: str = "Running") -> ContainerInfo:
    """One picker candidate, as `lifecycle.list_containers` returns it.

    A real `ContainerInfo` rather than a mock: the command filters candidates
    on `.mode` and `.state`, and a MagicMock answers both with something
    truthy, so every row would survive a filter that does not exist yet.
    """
    return ContainerInfo(
        name=f"sampleapp-{short}",
        state=state,
        network=None,
        ip=None,
        memory_limit=None,
        repo="sampleapp",
        mode=mode,
    )


class _Pickers:
    """Handle over the patched `jailbee.tui` pickers.

    Both ends are checkboxes, so both run through the *same*
    `pick_containers_multi`; they are told apart by their message — "FROM" or
    "INTO" — which is also what distinguishes them for the user. `order`
    records which prompt ran first (the requirement is sources, then targets),
    `offered` what each was shown, `messages` what each asked.

    The third end name, "single", is `tui.pick_container` — reached only by
    `-b`, since one branch cannot describe several sources, so that prompt
    alone is single-select.
    """

    def __init__(self, mocker) -> None:
        self._mocker = mocker
        self.order: list[str] = []
        self.offered: dict[str, list[str]] = {}
        self.messages: dict[str, str] = {}
        # What each prompt answers, set per test: a list is a tick, `None` a
        # cancel, `[]` an empty selection.
        self.source_answer: list[str] | None = []
        self.target_answer: list[str] | None = []
        self.single_answer: str | None = None
        mocker.patch("jailbee.tui.pick_containers_multi", side_effect=self._multi_asked)
        mocker.patch("jailbee.tui.pick_container", side_effect=self._single_asked)

    def _multi_asked(self, containers, *, message: str):
        end = "sources" if "FROM" in message else "targets"
        self._record(end, containers, message)
        return self.source_answer if end == "sources" else self.target_answer

    def _single_asked(self, containers, *, message: str):
        self._record("single", containers, message)
        return self.single_answer

    def _record(self, end: str, containers, message: str) -> None:
        self.order.append(end)
        self.offered[end] = [c.name for c in containers]
        self.messages[end] = message

    def offer(self, *containers: ContainerInfo) -> None:
        self._mocker.patch("jailbee.lifecycle.list_containers", return_value=list(containers))


@pytest.fixture
def merge_pickers(merge_repo, mocker):
    """Wire the interactive path: a TTY, a stubbed listing and both pickers.

    `list_containers` and `_stdin_is_interactive` are patched on
    `jailbee.lifecycle`, where the command imports them from lazily.
    """
    _cfg, incus = merge_repo
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    return _Pickers(mocker)


def test_git_merge_with_no_arguments_asks_for_sources_then_the_targets(merge_pickers, mocker):
    """A bare `jailbee git merge` prompts for both ends, sources first."""
    p = merge_pickers
    p.offer(_info("c1"), _info("c2"), _info("c4"))
    p.source_answer = ["sampleapp-c1", "sampleapp-c2"]
    p.target_answer = ["sampleapp-c4"]
    called = mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge"])

    assert result.exit_code == 0, result.output
    assert p.order == ["sources", "targets"]
    # Short names reach `sync`, in the order the checkbox listed them.
    assert [c.args[2] for c in called.call_args_list] == ["c1", "c2"]
    assert all(c.args[3] == "c4" for c in called.call_args_list)


def test_git_merge_target_prompt_is_a_checkbox_too(merge_pickers, mocker):
    """Several targets can be ticked in one pass, and each takes every source."""
    p = merge_pickers
    p.offer(_info("c1"), _info("t1"), _info("t2"))
    p.source_answer = ["sampleapp-c1"]
    p.target_answer = ["sampleapp-t1", "sampleapp-t2"]
    called = mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge"])

    assert result.exit_code == 0, result.output
    assert [(c.args[2], c.args[3]) for c in called.call_args_list] == [("c1", "t1"), ("c1", "t2")]


def test_git_merge_source_prompt_says_the_order_it_will_merge_in(merge_pickers, mocker):
    """The checkbox returns rows in *listed* order, not toggle order.

    Merge order decides which source hits a conflict first and stops the run,
    so the prompt has to say which order it is about to use.
    """
    p = merge_pickers
    p.offer(_info("c1"), _info("c4"))
    p.source_answer = ["sampleapp-c1"]
    p.target_answer = ["sampleapp-c4"]
    mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    runner.invoke(app, ["git", "merge"])

    assert "listed order" in p.messages["sources"]


def test_git_merge_prompts_only_for_the_targets_when_sources_are_given(merge_pickers, mocker):
    p = merge_pickers
    p.offer(_info("c1"), _info("c4"))
    p.target_answer = ["sampleapp-c4"]
    called = mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge", "c1"])

    assert result.exit_code == 0, result.output
    assert p.order == ["targets"]
    assert [c.args[2] for c in called.call_args_list] == ["c1"]
    assert called.call_args.args[3] == "c4"


def test_git_merge_prompts_only_for_the_sources_when_into_is_given(merge_pickers, mocker):
    p = merge_pickers
    p.offer(_info("c1"), _info("c4"))
    p.source_answer = ["sampleapp-c1"]
    called = mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge", "--into", "c4"])

    assert result.exit_code == 0, result.output
    assert p.order == ["sources"]
    assert called.call_args.args[2] == "c1"
    assert called.call_args.args[3] == "c4"


def test_git_merge_with_a_branch_override_asks_for_exactly_one_source(merge_pickers, mocker):
    """`-b` describes one branch, so a multi-select would offer an illegal answer.

    The single-select picker makes the constraint unreachable instead of
    letting the user tick two rows and fail afterwards.
    """
    p = merge_pickers
    p.offer(_info("c1"), _info("c4"))
    p.single_answer = "sampleapp-c1"
    called = mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge", "--into", "c4", "-b", "feat/x"])

    assert result.exit_code == 0, result.output
    assert p.order == ["single"]
    assert called.call_args.args[2] == "c1"
    assert called.call_args.kwargs["branch"] == "feat/x"


def test_git_merge_offers_only_running_clone_mode_containers(merge_pickers, mocker):
    """A mount-mode or stopped row can only error, so it is not offered.

    `sync.assert_container_publishable` refuses both at the source end and
    `merge_container_into_container`'s own preflight refuses both at the
    target end.
    """
    p = merge_pickers
    p.offer(
        _info("c1"),
        _info("shared", mode="mount"),
        _info("cold", state="Stopped"),
        _info("c4"),
    )
    p.source_answer = ["sampleapp-c1"]
    p.target_answer = ["sampleapp-c4"]
    mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge"])

    assert result.exit_code == 0, result.output
    assert p.offered["sources"] == ["sampleapp-c1", "sampleapp-c4"]


def test_git_merge_target_prompt_hides_the_chosen_sources(merge_pickers, mocker):
    """A container cannot merge into itself, so its row is not offered as a target.

    Hiding the row is how the rule is enforced interactively: the choice that
    the command would refuse is never presented.
    """
    p = merge_pickers
    p.offer(_info("c1"), _info("c2"), _info("c4"))
    p.source_answer = ["sampleapp-c1", "sampleapp-c2"]
    p.target_answer = ["sampleapp-c4"]
    mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge"])

    assert result.exit_code == 0, result.output
    assert p.offered["targets"] == ["sampleapp-c4"]


def test_git_merge_source_prompt_hides_the_targets_given_on_the_command_line(merge_pickers, mocker):
    """The same rule from the other side: a named target cannot also be a source."""
    p = merge_pickers
    p.offer(_info("c1"), _info("c4"))
    p.source_answer = ["sampleapp-c1"]
    mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge", "--into", "c4"])

    assert result.exit_code == 0, result.output
    assert p.offered["sources"] == ["sampleapp-c1"]


def test_git_merge_says_so_when_the_sources_leave_no_target(merge_pickers, mocker):
    """Ticking every eligible container as a source leaves nothing to merge into."""
    p = merge_pickers
    p.offer(_info("c1"), _info("c2"))
    p.source_answer = ["sampleapp-c1", "sampleapp-c2"]
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge"])

    assert result.exit_code == 1
    combined = flat_output((result.output or "") + (result.stderr or ""))
    assert "no eligible container left to merge into" in combined
    assert "targets" not in p.order
    called.assert_not_called()


def test_git_merge_says_so_when_the_targets_leave_no_source(merge_pickers, mocker):
    """Naming every eligible container with `--into` leaves nothing to merge from."""
    p = merge_pickers
    p.offer(_info("c1"), _info("c4"))
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge", "--into", "c1", "--into", "c4"])

    assert result.exit_code == 1
    combined = flat_output((result.output or "") + (result.stderr or ""))
    assert "no eligible container left to merge from" in combined
    assert p.order == []
    called.assert_not_called()


def test_git_merge_cancelled_source_prompt_merges_nothing(merge_pickers, mocker):
    p = merge_pickers
    p.offer(_info("c1"), _info("c4"))
    p.source_answer = None
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge"])

    assert result.exit_code == 1
    assert "Aborted" in flat_output((result.output or "") + (result.stderr or ""))
    assert p.order == ["sources"]
    called.assert_not_called()


def test_git_merge_empty_source_selection_merges_nothing(merge_pickers, mocker):
    """Enter with nothing ticked is not an error, and not a merge either."""
    p = merge_pickers
    p.offer(_info("c1"), _info("c4"))
    p.source_answer = []
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge"])

    assert result.exit_code == 0, result.output
    assert "Nothing selected" in flat_output(result.output)
    assert p.order == ["sources"]
    called.assert_not_called()


def test_git_merge_cancelled_target_prompt_merges_nothing(merge_pickers, mocker):
    """Cancelling the second prompt must not merge the sources anywhere."""
    p = merge_pickers
    p.offer(_info("c1"), _info("c4"))
    p.source_answer = ["sampleapp-c1"]
    p.target_answer = None
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge"])

    assert result.exit_code == 1
    assert "Aborted" in flat_output((result.output or "") + (result.stderr or ""))
    assert p.order == ["sources", "targets"]
    called.assert_not_called()


def test_git_merge_empty_target_selection_merges_nothing(merge_pickers, mocker):
    """Ticking no target is a decision not to merge, not an error."""
    p = merge_pickers
    p.offer(_info("c1"), _info("c4"))
    p.source_answer = ["sampleapp-c1"]
    p.target_answer = []
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge"])

    assert result.exit_code == 0, result.output
    assert "Nothing selected" in flat_output(result.output)
    called.assert_not_called()


def test_git_merge_without_eligible_containers_says_so(merge_pickers, mocker):
    """Nothing to offer is reported, not rendered as an empty picker."""
    p = merge_pickers
    p.offer(_info("shared", mode="mount"), _info("cold", state="Stopped"))
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge"])

    assert result.exit_code == 1
    assert p.order == []
    called.assert_not_called()
