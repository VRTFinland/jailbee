"""The publication lock is shared by independent host processes and keeps its inode."""

import multiprocessing

from jailbee.container_locks import locked
from jailbee.paths import xdg_data_home


def _hold(full, acquired, release):
    with locked(full):
        acquired.set()
        assert release.wait(10)


def test_independent_process_holds_stable_container_lock():
    import fcntl

    ctx = multiprocessing.get_context("spawn")
    acquired, release = ctx.Event(), ctx.Event()
    worker = ctx.Process(target=_hold, args=("proj-process", acquired, release))
    worker.start()
    try:
        assert acquired.wait(10)
        path = xdg_data_home() / "jailbee/container-locks/proj-process.lock"
        inode = path.stat().st_ino
        with path.open("a") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise AssertionError("a separate host process did not exclude this caller")
        release.set()
        worker.join(10)
        assert worker.exitcode == 0
        with locked("proj-process"):
            assert path.stat().st_ino == inode
        with locked("proj-process"):
            assert path.stat().st_ino == inode
    finally:
        release.set()
        worker.join(10)
        if worker.is_alive():
            worker.terminate()
            worker.join(10)
