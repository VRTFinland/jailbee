"""Final action-host review regressions through the real service socket."""

import os
import threading

import pytest

from jailbee.state_service import paths
from jailbee.state_service.client import StateClient
from jailbee.state_service.protocol import RunAction
from tests.test_state_client import _until
from tests.test_state_server import FakeGatherer, Running, reap_servers, track_server  # noqa: F401


def test_busy_repo_does_not_evict_another_repos_finished_history(runtime_dir, tmp_path):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import create_engine

    from jailbee.db import _ensure_schema
    from jailbee.db.actions import save
    from jailbee.state_service.protocol import ActionEntry

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    _ensure_schema(engine)
    now = datetime.now(UTC)
    for entry_id in range(1, 104):
        root = str(tmp_path / ("quiet" if entry_id == 1 else "busy"))
        save(
            engine,
            ActionEntry(
                entry_id,
                ["ls"],
                root,
                "t",
                False,
                "local",
                "none",
                root,
                "quiet" if entry_id == 1 else "busy",
                finished=True,
                state="done",
                finished_at=now - timedelta(seconds=104 - entry_id),
            ),
        )
    paths.ensure_runtime_dir()
    running = Running(FakeGatherer(), paths.socket_path(), engine=engine)
    client = StateClient(None, dashboard_id="local", version="v1", spawn=lambda: None)
    client.start()
    try:
        client.wait_first_snapshot(3)
        entries = client.action_entries()
        assert 1 in entries
        assert len(entries) == 101
        assert 2 not in entries
        assert 3 not in entries
        assert 103 in entries
    finally:
        client.close()
        running.server._stopping = True
        running.thread.join(5)
        engine.dispose()


def test_batch_remnant_is_interrupted_before_and_after_reconnect(runtime_dir, tmp_path):
    from jailbee.state_service.protocol import RunActionBatch

    def broken_spawn(*args, **kwargs):
        raise OSError("cannot start command")

    paths.ensure_runtime_dir()
    Running(FakeGatherer(), paths.socket_path(), action_spawn=broken_spawn)
    client = StateClient(None, dashboard_id="local", version="v1", spawn=lambda: None)
    client.start()
    later = StateClient(None, dashboard_id="later", version="v1", spawn=lambda: None)
    try:
        client.wait_first_snapshot(3)
        client.run_action_batch(
            RunActionBatch(
                [
                    RunAction(
                        i, ["ls"], str(tmp_path), "t", False, "local", "none", str(tmp_path), "repo"
                    )
                    for i in (1, 2)
                ]
            )
        )
        _until(
            lambda: (
                client.action_entries().get(2) is not None and client.action_entries()[2].finished
            )
        )
        assert client.action_entries()[2].state == "interrupted"
        later.start()
        later.wait_first_snapshot(3)
        assert later.action_entries()[2].state == "interrupted"
    finally:
        client.close()
        later.close()


@pytest.mark.parametrize("allowed", [False, True])
def test_remote_dismiss_requires_current_command_policy(
    runtime_dir, tmp_path, monkeypatch, allowed
):
    from jailbee import global_config
    from jailbee.config.models_remote import RemoteCommandPolicy
    from jailbee.remote_ssh import repo_scope

    config = global_config.GlobalConfig()
    config.remote.ssh.commands = RemoteCommandPolicy(mode="disabled")
    monkeypatch.setattr(global_config, "load_global_config", lambda path: (config, []))
    monkeypatch.setattr(
        repo_scope, "registered_repos", lambda **kw: [repo_scope.RepoChoice("repo", tmp_path)]
    )

    class Child:
        stdout = None

        def __init__(self, sock):
            sock.close()

        def wait(self):
            return 0

        def kill(self):
            pass

    paths.ensure_runtime_dir()
    Running(
        FakeGatherer(), paths.socket_path(), action_spawn=lambda *a, **kw: Child(kw["prompt_sock"])
    )
    local = StateClient(None, dashboard_id="local", version="v1", spawn=lambda: None)
    remote = StateClient(
        None, dashboard_id="remote", transport="ssh", version="v1", spawn=lambda: None
    )
    local.start()
    remote.start()
    try:
        local.wait_first_snapshot(3)
        remote.wait_first_snapshot(3)
        local.run_action(
            RunAction(1, ["ls"], str(tmp_path), "t", False, "local", "none", str(tmp_path), "repo")
        )
        _until(
            lambda: (
                remote.action_entries().get(1) is not None and remote.action_entries()[1].finished
            )
        )
        if allowed:
            config.remote.ssh.commands = RemoteCommandPolicy(mode="full")
        remote.dismiss_action(1)
        if allowed:
            _until(lambda: 1 not in local.action_entries())
        else:
            _until(lambda: remote.action_rejection(1) is not None)
            assert 1 in local.action_entries()
            assert 1 in remote.action_entries()
            local.dismiss_action(1)
            _until(lambda: 1 not in remote.action_entries())
    finally:
        local.close()
        remote.close()


@pytest.mark.parametrize("invalidate", ["refresh", "ttl"])
def test_remote_output_burst_reuses_scope_until_invalidation(
    runtime_dir, tmp_path, monkeypatch, invalidate
):
    from jailbee import global_config
    from jailbee.remote_ssh import repo_scope

    loads = []
    queries = []
    config = global_config.GlobalConfig()

    def load(_path):
        loads.append(True)
        return config, []

    def repos(**_kwargs):
        queries.append(True)
        return [repo_scope.RepoChoice("repo", tmp_path)]

    monkeypatch.setattr(global_config, "load_global_config", load)
    monkeypatch.setattr(repo_scope, "registered_repos", repos)
    children = []

    class Child:
        def __init__(self, sock):
            sock.close()
            read_fd, self.write_fd = os.pipe()
            self.stdout = os.fdopen(read_fd, "rb")
            self.release = threading.Event()
            children.append(self)

        def wait(self):
            self.release.wait(5)
            return 0

        def kill(self):
            self.release.set()

    paths.ensure_runtime_dir()
    clock = [0.0]
    Running(
        FakeGatherer(),
        paths.socket_path(),
        action_spawn=lambda *a, **kw: Child(kw["prompt_sock"]),
        clock=lambda: clock[0],
    )
    local = StateClient(None, dashboard_id="local", version="v1", spawn=lambda: None)
    remote = StateClient(
        None, dashboard_id="remote", transport="ssh", version="v1", spawn=lambda: None
    )
    local.start()
    remote.start()
    try:
        local.wait_first_snapshot(3)
        remote.wait_first_snapshot(3)
        local.run_action(
            RunAction(1, ["ls"], str(tmp_path), "t", False, "local", "none", str(tmp_path), "repo")
        )
        _until(lambda: 1 in remote.action_entries() and bool(children))
        before = len(loads), len(queries)
        for index in range(10):
            text = f"chunk {index}\n"
            os.write(children[0].write_fd, text.encode())
            _until(lambda text=text: text in remote.action_entries()[1].output)
        assert (len(loads), len(queries)) == before

        config.remote.ssh.excluded_repos = ["repo"]
        if invalidate == "refresh":
            seq = remote.latest().seq
            remote.refresh()
            _until(lambda: remote.latest().seq > seq)
        else:
            clock[0] = 6.0
        os.write(children[0].write_fd, b"private output\n")
        _until(lambda: "private output" in local.action_entries()[1].output)
        assert "private output" not in remote.action_entries()[1].output
        assert len(queries) > before[1]
    finally:
        if children:
            os.close(children[0].write_fd)
            children[0].release.set()
        local.close()
        remote.close()
