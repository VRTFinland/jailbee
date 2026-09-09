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


# --- argument validation -------------------------------------------------------


def test_git_merge_rejects_branch_override_with_several_sources(merge_repo, mocker):
    """`-b` reads one branch, so it cannot describe several sources."""
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge", "c1", "c2", "--into", "c4", "-b", "feat/x"])

    assert result.exit_code == 2
    assert "single source" in flat_output(result.output)
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
    """Handle over the two patched `jailbee.tui` pickers.

    `sources` / `target` are the patched functions themselves (so call
    arguments can be asserted) and `order` records which prompt ran first —
    the requirement is sources *then* target.

    `target` is `tui.pick_container`, which the command also uses for the
    *source* when `-b` is given: one branch cannot describe several sources,
    so that prompt is single-select. Tests of that path read the same slot.
    """

    def __init__(self, mocker) -> None:
        self._mocker = mocker
        self.order: list[str] = []
        self.source_answer: list[str] | None = []
        self.target_answer: str | None = None
        self.sources = mocker.patch(
            "jailbee.tui.pick_containers_multi", side_effect=self._sources_asked
        )
        self.target = mocker.patch("jailbee.tui.pick_container", side_effect=self._target_asked)

    def _sources_asked(self, _containers, **_kwargs):
        self.order.append("sources")
        return self.source_answer

    def _target_asked(self, _containers, **_kwargs):
        self.order.append("target")
        return self.target_answer

    def offer(self, *containers: ContainerInfo) -> None:
        self._mocker.patch("jailbee.lifecycle.list_containers", return_value=list(containers))

    def answer(self, *, sources: list[str] | None = None, target: str | None = None) -> None:
        self.source_answer = sources
        self.target_answer = target


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


def test_git_merge_with_no_arguments_asks_for_sources_then_the_target(merge_pickers, mocker):
    """A bare `jailbee git merge` prompts for both ends, sources first."""
    p = merge_pickers
    p.offer(_info("c1"), _info("c2"), _info("c4"))
    p.answer(sources=["sampleapp-c1", "sampleapp-c2"], target="sampleapp-c4")
    called = mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge"])

    assert result.exit_code == 0, result.output
    assert p.order == ["sources", "target"]
    # Short names reach `sync`, in the order the checkbox listed them.
    assert [c.args[2] for c in called.call_args_list] == ["c1", "c2"]
    assert all(c.args[3] == "c4" for c in called.call_args_list)


def test_git_merge_source_prompt_says_the_order_it_will_merge_in(merge_pickers, mocker):
    """The checkbox returns rows in *listed* order, not toggle order.

    Merge order decides which source hits a conflict first and stops the run,
    so the prompt has to say which order it is about to use.
    """
    p = merge_pickers
    p.offer(_info("c1"), _info("c4"))
    p.answer(sources=["sampleapp-c1"], target="sampleapp-c4")
    mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    runner.invoke(app, ["git", "merge"])

    assert "listed order" in p.sources.call_args.kwargs["message"]


def test_git_merge_prompts_only_for_the_target_when_sources_are_given(merge_pickers, mocker):
    p = merge_pickers
    p.offer(_info("c1"), _info("c4"))
    p.answer(target="sampleapp-c4")
    called = mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge", "c1"])

    assert result.exit_code == 0, result.output
    p.sources.assert_not_called()
    assert [c.args[2] for c in called.call_args_list] == ["c1"]
    assert called.call_args.args[3] == "c4"


def test_git_merge_prompts_only_for_the_sources_when_into_is_given(merge_pickers, mocker):
    p = merge_pickers
    p.offer(_info("c1"), _info("c4"))
    p.answer(sources=["sampleapp-c1"])
    called = mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge", "--into", "c4"])

    assert result.exit_code == 0, result.output
    p.target.assert_not_called()
    assert called.call_args.args[2] == "c1"
    assert called.call_args.args[3] == "c4"


def test_git_merge_with_a_branch_override_asks_for_exactly_one_source(merge_pickers, mocker):
    """`-b` describes one branch, so a multi-select would offer an illegal answer.

    The single-select picker makes the constraint unreachable instead of
    letting the user tick two rows and fail afterwards.
    """
    p = merge_pickers
    p.offer(_info("c1"), _info("c4"))
    p.answer(target="sampleapp-c1")  # the source picker is `pick_container` here
    called = mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge", "--into", "c4", "-b", "feat/x"])

    assert result.exit_code == 0, result.output
    p.sources.assert_not_called()
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
    p.answer(sources=["sampleapp-c1"], target="sampleapp-c4")
    mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge"])

    assert result.exit_code == 0, result.output
    for picker in (p.sources, p.target):
        offered = [c.name for c in picker.call_args.args[0]]
        assert offered == ["sampleapp-c1", "sampleapp-c4"]


def test_git_merge_still_offers_a_chosen_source_as_the_target(merge_pickers, mocker):
    """Merging a container into itself is deliberately unguarded.

    It means merging branch X into that container's own checked-out branch Y,
    which is coherent — so the target prompt must not hide the rows the source
    prompt just took.
    """
    p = merge_pickers
    p.offer(_info("c1"), _info("c4"))
    p.answer(sources=["sampleapp-c1"], target="sampleapp-c1")
    called = mocker.patch("jailbee.sync.merge_container_into_container", return_value=_result())

    result = runner.invoke(app, ["git", "merge"])

    assert result.exit_code == 0, result.output
    assert "sampleapp-c1" in [c.name for c in p.target.call_args.args[0]]
    assert called.call_args.args[2] == "c1"
    assert called.call_args.args[3] == "c1"


def test_git_merge_cancelled_source_prompt_merges_nothing(merge_pickers, mocker):
    p = merge_pickers
    p.offer(_info("c1"), _info("c4"))
    p.answer(sources=None)
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge"])

    assert result.exit_code == 1
    assert "Aborted" in flat_output((result.output or "") + (result.stderr or ""))
    p.sources.assert_called_once()
    p.target.assert_not_called()
    called.assert_not_called()


def test_git_merge_empty_source_selection_merges_nothing(merge_pickers, mocker):
    """Enter with nothing ticked is not an error, and not a merge either."""
    p = merge_pickers
    p.offer(_info("c1"), _info("c4"))
    p.answer(sources=[])
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge"])

    assert result.exit_code == 0, result.output
    assert "Nothing selected" in flat_output(result.output)
    p.target.assert_not_called()
    called.assert_not_called()


def test_git_merge_cancelled_target_prompt_merges_nothing(merge_pickers, mocker):
    """Cancelling the second prompt must not merge the sources anywhere."""
    p = merge_pickers
    p.offer(_info("c1"), _info("c4"))
    p.answer(sources=["sampleapp-c1"], target=None)
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge"])

    assert result.exit_code == 1
    assert "Aborted" in flat_output((result.output or "") + (result.stderr or ""))
    assert p.order == ["sources", "target"]
    called.assert_not_called()


def test_git_merge_without_eligible_containers_says_so(merge_pickers, mocker):
    """Nothing to offer is reported, not rendered as an empty picker."""
    p = merge_pickers
    p.offer(_info("shared", mode="mount"), _info("cold", state="Stopped"))
    called = mocker.patch("jailbee.sync.merge_container_into_container")

    result = runner.invoke(app, ["git", "merge"])

    assert result.exit_code == 1
    p.sources.assert_not_called()
    p.target.assert_not_called()
    called.assert_not_called()
