"""Host-only PR identity locks; no container execution or HOME writes."""

import errno
import fcntl
import os
import time
from contextlib import contextmanager
from multiprocessing import get_context

import pytest

from jailbee.outbox.io import PrManagement
from jailbee.outbox.models import OutboxExecutionError
from jailbee.outbox_io import ContainerIdentity


def _hold_lock(kind, root, identity, pipe, release_after):
    manager = PrManagement(root)
    key = identity
    with manager.lock(key):
        pipe.send("held")
        if pipe.poll(release_after):
            pipe.recv()
    pipe.close()


@contextmanager
def held_by_process(kind, root, identity, *, release_after=3):
    """Use an independent file description; auto-release bounds a broken test."""
    context = get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_hold_lock, args=(kind, root, identity, child, release_after))
    process.start()
    child.close()
    try:
        assert parent.poll(10), "holder did not acquire the lock"
        assert parent.recv() == "held"
        yield parent
    finally:
        if process.is_alive():
            try:
                parent.send("release")
            except BrokenPipeError:
                pass  # The auto-release may have closed the pipe before process exit.
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(10)
        parent.close()
        assert process.exitcode == 0


def lock_case(kind, tmp_path):
    identity = ContainerIdentity("repo-feature", "2026-09-30T12:00:00Z")
    root = tmp_path / kind
    manager = PrManagement(root)
    key = identity
    error = OutboxExecutionError
    return manager, key, error, root, identity


@pytest.mark.parametrize("kind", ["pr"])
def test_finite_lock_wait_times_out_closes_fd_and_can_be_reused(kind, tmp_path, mocker):
    manager, key, error, root, identity = lock_case(kind, tmp_path)
    opened = mocker.spy(os, "open")
    with held_by_process(kind, root, identity):
        started = time.monotonic()
        with pytest.raises(error, match=r"(?i)timed out.*refresh.*retry"):
            with manager.lock(key, timeout=0.1):
                pytest.fail("busy lock entered")
        assert 0.08 <= time.monotonic() - started < 0.8
        descriptor = opened.spy_return
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF
        if kind == "pr":
            assert manager.identity is None
        # A leaked held-path entry would incorrectly allow this second attempt.
        with pytest.raises(error):
            with manager.lock(key, timeout=0):
                pytest.fail("timed-out lock recorded as held")
    with manager.lock(key, timeout=0.1):
        pass


@pytest.mark.parametrize("kind", ["pr"])
def test_default_lock_still_waits_for_other_process(kind, tmp_path):
    manager, key, _, root, identity = lock_case(kind, tmp_path)
    with held_by_process(kind, root, identity, release_after=0.25):
        started = time.monotonic()
        with manager.lock(key):
            assert time.monotonic() - started >= 0.15


@pytest.mark.parametrize("kind", ["pr"])
def test_finite_lock_acquires_when_holder_releases_before_deadline(kind, tmp_path):
    manager, key, _, root, identity = lock_case(kind, tmp_path)
    with held_by_process(kind, root, identity, release_after=0.2):
        started = time.monotonic()
        with manager.lock(key, timeout=1):
            assert 0.1 <= time.monotonic() - started < 0.8


@pytest.mark.parametrize("kind", ["pr"])
def test_finite_lock_reentry_keeps_outer_lock_without_extra_acquisition(kind, tmp_path, mocker):
    manager, key, error, root, _ = lock_case(kind, tmp_path)
    with manager.lock(key, timeout=0):
        flock = mocker.spy(fcntl, "flock")
        with manager.lock(key, timeout=0):
            pass
        flock.assert_not_called()
        other = PrManagement(root)
        with pytest.raises(error):
            with other.lock(key, timeout=0):
                pytest.fail("inner context released outer lock")
    with other.lock(key, timeout=0):
        pass


@pytest.mark.parametrize("kind", ["pr"])
def test_lock_oserror_closes_fd_and_keeps_state_reusable(kind, tmp_path, mocker):
    manager, key, error, _, _ = lock_case(kind, tmp_path)
    opened = mocker.spy(os, "open")
    real_flock = fcntl.flock
    flock = mocker.patch.object(fcntl, "flock", side_effect=OSError(errno.EIO, "I/O failure"))
    with pytest.raises(error, match="could not lock"):
        with manager.lock(key, timeout=0.1):
            pytest.fail("failed acquisition entered")
    with pytest.raises(OSError) as closed:
        os.fstat(opened.spy_return)
    assert closed.value.errno == errno.EBADF
    flock.side_effect = real_flock
    with manager.lock(key, timeout=0):
        pass


@pytest.mark.parametrize("kind", ["pr"])
@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf"), float("-inf")])
def test_lock_refuses_nonfinite_or_negative_timeout(kind, timeout, tmp_path):
    manager, key, error, _, _ = lock_case(kind, tmp_path)
    with pytest.raises(error, match="finite non-negative"):
        with manager.lock(key, timeout=timeout):
            pytest.fail("invalid deadline accepted")
    with manager.lock(key, timeout=0):
        pass


def _probe(identity, state, pipe):
    import os

    from jailbee.outbox.io import PrManagement

    os.environ["XDG_STATE_HOME"] = str(state)
    pipe.send("waiting")
    with PrManagement().lock(identity):
        pipe.send("acquired")
    pipe.close()


def test_pr_management_serializes_identity_and_reenters(tmp_path, monkeypatch):
    from jailbee.outbox.io import PrManagement

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    context = get_context("spawn")
    identity = ContainerIdentity("repo-feature", "2026-09-30T12:00:00Z")
    replacement = ContainerIdentity(identity.full_name, "2026-10-01T12:00:00Z")
    manager = PrManagement()
    processes = []
    pipes = []
    try:
        with manager.lock(identity):
            with manager.lock(identity):
                for candidate in (identity, replacement):
                    parent, child = context.Pipe()
                    process = context.Process(target=_probe, args=(candidate, tmp_path, child))
                    process.start()
                    child.close()
                    processes.append(process)
                    pipes.append(parent)
                    assert parent.poll(10), "worker did not reach the lock"
                    assert parent.recv() == "waiting"
                assert not pipes[0].poll(0.3), "same identity must remain blocked"
                assert pipes[1].poll(10), "replacement identity must not be blocked"
                assert pipes[1].recv() == "acquired"
            assert not pipes[0].poll(0.3), "inner release must not release the outer lock"
        assert pipes[0].poll(10)
        assert pipes[0].recv() == "acquired"
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(10)
        for pipe in pipes:
            pipe.close()
