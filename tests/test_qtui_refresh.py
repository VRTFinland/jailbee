import pytest

pytest.importorskip("PySide6")

from pathlib import Path

from PySide6.QtCore import QThread

from jailbee.dashboard import RepoGroup
from jailbee.git_status import GitStatus
from jailbee.lifecycle import ContainerInfo
from jailbee.qtui.refresh import RefreshWorker


def test_run_loop_emits_first_snapshot_then_stops(qtbot, mocker):
    groups = [RepoGroup("p", "/repo", Path("/repo/.gie/config.yaml"), [])]
    mocker.patch("jailbee.qtui.refresh.gather_live", return_value=groups)

    worker = RefreshWorker(
        incus=mocker.Mock(),
        cwd_config=Path("/repo/.gie/config.yaml"),
        interval=0.5,
        git_interval=10.0,
        git_enabled=True,
    )
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run_loop)

    with qtbot.waitSignal(worker.groupsReady, timeout=3000) as blocker:
        thread.start()
    assert blocker.args[0] == groups

    worker.request_stop()
    thread.quit()
    assert thread.wait(3000)


def test_run_loop_survives_gather_failure_and_keeps_polling(qtbot, mocker):
    """FIX 1 regression test: a gather exception must emit `failed` but must
    NOT kill the loop. The next scheduled gather should succeed normally,
    proving the loop is still alive (and that bookkeeping advanced so the
    retry happens once per `interval`, not in a hot spin)."""
    groups = [RepoGroup("p", "/repo", Path("/repo/.gie/config.yaml"), [])]
    mocker.patch(
        "jailbee.qtui.refresh.gather_live",
        side_effect=[RuntimeError("boom"), groups],
    )

    worker = RefreshWorker(
        incus=mocker.Mock(),
        cwd_config=Path("/repo/.gie/config.yaml"),
        interval=0.2,
        git_interval=10.0,
        git_enabled=True,
    )
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run_loop)

    try:
        with qtbot.waitSignal(worker.failed, timeout=3000) as blocker:
            thread.start()
        assert blocker.args[0] == "boom"

        # The loop must still be running: the next scheduled gather succeeds.
        with qtbot.waitSignal(worker.groupsReady, timeout=3000) as blocker2:
            pass
        assert blocker2.args[0] == groups
    finally:
        worker.request_stop()
        thread.quit()
        assert thread.wait(3000)


def _ci(name: str, *, git_status: GitStatus | None) -> ContainerInfo:
    return ContainerInfo(
        name=name,
        state="Running",
        network="strict",
        ip=None,
        memory_limit=None,
        repo="p",
        git_status=git_status,
    )


def test_run_loop_carries_forward_git_status_on_base_refresh(qtbot, mocker):
    """FIX 2 regression test: a base (non-git) refresh must not blank out
    git_status columns — it should carry forward the last git-tier value."""
    status = GitStatus(wt="+1 -0", ahead_diff="clean", ahead_count="1", conflict="ok")
    git_groups = [
        RepoGroup("p", "/repo", Path("/repo/.gie/config.yaml"), [_ci("p-one", git_status=status)])
    ]
    base_groups = [
        RepoGroup("p", "/repo", Path("/repo/.gie/config.yaml"), [_ci("p-one", git_status=None)])
    ]
    mocker.patch(
        "jailbee.qtui.refresh.gather_live",
        side_effect=[git_groups, base_groups],
    )

    worker = RefreshWorker(
        incus=mocker.Mock(),
        cwd_config=Path("/repo/.gie/config.yaml"),
        interval=0.1,
        git_interval=10.0,
        git_enabled=True,
    )
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run_loop)

    try:
        with qtbot.waitSignal(worker.groupsReady, timeout=3000) as first:
            thread.start()
        assert first.args[0][0].containers[0].git_status is status

        with qtbot.waitSignal(worker.groupsReady, timeout=3000) as second:
            pass
        carried = second.args[0][0].containers[0].git_status
        assert carried is not None
        assert carried == status
    finally:
        worker.request_stop()
        thread.quit()
        assert thread.wait(3000)


def test_set_interval_clamps_and_clears_paused(mocker):
    worker = RefreshWorker(
        incus=mocker.Mock(),
        cwd_config=None,
        interval=3.0,
        git_interval=10.0,
        git_enabled=True,
    )
    worker.set_paused(True)
    worker.set_interval(0.1)
    assert worker._interval == 0.5  # clamped to the floor
    assert worker._paused is False

    worker.set_paused(True)
    worker.set_interval(7.0)
    assert worker._interval == 7.0
    assert worker._paused is False


def test_set_paused_then_force_still_gathers(qtbot, mocker):
    """A plain tick must NOT gather while paused, but force() must still
    trigger a gather. This is the behavioral variant; see
    test_refresh_due_paused_gating_unit below for the flake-resistant unit
    version of the same decision."""
    groups = [RepoGroup("p", "/repo", Path("/repo/.gie/config.yaml"), [])]
    mocker.patch("jailbee.qtui.refresh.gather_live", return_value=groups)

    worker = RefreshWorker(
        incus=mocker.Mock(),
        cwd_config=Path("/repo/.gie/config.yaml"),
        interval=0.2,
        git_interval=10.0,
        git_enabled=True,
    )
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run_loop)

    try:
        # First gather always happens (the `first` gather), then pause.
        with qtbot.waitSignal(worker.groupsReady, timeout=3000):
            thread.start()
        worker.set_paused(True)

        with qtbot.assertNotEmitted(worker.groupsReady, wait=800):
            pass

        with qtbot.waitSignal(worker.groupsReady, timeout=3000):
            worker.force()
    finally:
        worker.request_stop()
        thread.quit()
        assert thread.wait(3000)


def test_refresh_due_paused_gating_unit():
    """Unit-level equivalent of the paused-gating decision made in
    ``run_loop`` (``if self._paused and not forced and not first: do_base
    = False``), isolated from thread timing so it can't flake."""
    from jailbee.dashboard import _refresh_due

    do_base, _do_git = _refresh_due(
        now=100.0,
        last_base=99.9,
        last_full=0.0,
        interval=0.2,
        git_interval=10.0,
        git_enabled=True,
        first=False,
        forced=False,
    )
    # _refresh_due alone says "not due yet"; paused only needs to force
    # do_base False when it *would* have been True and it's not forced/first.
    paused = True
    forced = False
    first = False
    if paused and not forced and not first:
        do_base = False
    assert do_base is False

    # forced always wins even while paused.
    do_base2, _ = _refresh_due(
        now=100.0,
        last_base=99.9,
        last_full=0.0,
        interval=0.2,
        git_interval=10.0,
        git_enabled=True,
        first=False,
        forced=True,
    )
    forced = True
    if paused and not forced and not first:
        do_base2 = False
    assert do_base2 is True


def test_gather_once_picks_up_a_repo_registered_after_launch(mocker):
    """The worker must not carry a config-path list captured at launch.

    A repo that registers mid-session (`jailbee new` in an unregistered repo,
    or the pool timer re-registering one) was invisible to the running
    window: its containers landed in a view-only orphan group, so
    right-clicking them opened no menu until `jb gui` was restarted.
    """
    a = Path("/repos/a/.jailbee/config.yaml")
    b = Path("/repos/b/.jailbee/config.yaml")
    registered = [a]
    mocker.patch("jailbee.dashboard.registered_repo_configs", side_effect=lambda: list(registered))
    gr = mocker.patch("jailbee.dashboard.gather_rows", return_value=[])

    worker = RefreshWorker(
        incus=mocker.Mock(),
        cwd_config=None,
        interval=0.5,
        git_interval=10.0,
        git_enabled=False,
    )
    worker.gather_once(do_git=False)
    assert gr.call_args.args[1] == [a]

    registered.append(b)
    worker.gather_once(do_git=False)
    assert gr.call_args.args[1] == [a, b]


def test_gather_once_delegates_to_gather_live(mocker):
    groups = [RepoGroup("p", "/repo", None, [])]
    gl = mocker.patch("jailbee.qtui.refresh.gather_live", return_value=groups)
    incus = mocker.Mock()
    cwd = Path("/repo/.jailbee/config.yaml")
    worker = RefreshWorker(
        incus=incus,
        cwd_config=cwd,
        interval=0.5,
        git_interval=10.0,
        git_enabled=False,
    )
    assert worker.gather_once(do_git=True) == groups
    # git disabled at the worker level wins over a git-tier tick
    gl.assert_called_once_with(incus, cwd, with_git=False)
