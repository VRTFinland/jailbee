"""Actions stream to every subscriber in scope, including a mid-run subscriber."""

import asyncio
import os
import threading

from jailbee.state_service import paths
from jailbee.state_service.client import StateClient
from jailbee.state_service.protocol import RunAction
from jailbee.state_service.server import StateServer
from tests.test_state_client import _until
from tests.test_state_server import FakeGatherer, reap_servers, track_server  # noqa: F401


def test_live_stream_is_scoped_and_mid_run_client_gets_output_so_far(runtime_dir, tmp_path):
    ready = threading.Event()
    release = threading.Event()
    children = []

    class Child:
        def __init__(self, sock):
            read_fd, self.write_fd = os.pipe()
            self.stdout = os.fdopen(read_fd, "rb")
            self.sock = sock
            self.thread = threading.Thread(target=self.work)
            self.thread.start()

        def work(self):
            os.write(self.write_fd, b"first live line\n")
            ready.set()
            release.wait(5)
            os.write(self.write_fd, b"last live line\n")
            os.close(self.write_fd)
            self.sock.close()

        def wait(self):
            self.thread.join(5)
            return 0

        def kill(self):
            release.set()

    def spawn(*args, **kwargs):
        child = Child(kwargs["prompt_sock"])
        children.append(child)
        return child

    paths.ensure_runtime_dir()
    server = StateServer(FakeGatherer(), version="v1", action_spawn=spawn)
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
    )
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    clients = []

    def connect(identity, root=None, **kwargs):
        client = StateClient(
            root, dashboard_id=identity, version="v1", spawn=lambda: None, **kwargs
        )
        client.start()
        clients.append(client)
        client.wait_first_snapshot(3)
        return client

    repo = tmp_path / "repo"
    repo.mkdir()
    other_repo = tmp_path / "other"
    other_repo.mkdir()
    first = connect("origin", repo)
    second = connect("follower", repo)
    unrelated = connect("other", other_repo)
    excluded = connect("excluded", excluded_prefixes=["repo"])
    try:
        first.run_action(
            RunAction(71, ["ls"], str(repo), "repo-a", True, "origin", "none", str(repo), "repo")
        )
        assert ready.wait(3)
        _until(
            lambda: (
                second.action_entries().get(71) is not None
                and "first live" in second.action_entries()[71].output
            )
        )
        assert not second.action_entries()[71].finished
        late = connect("late", repo)
        _until(lambda: 71 in late.action_entries())
        assert late.action_entries()[71].output == "first live line\n"
        assert not late.action_entries()[71].finished
        assert 71 not in unrelated.action_entries()
        assert 71 not in excluded.action_entries()
        release.set()
        _until(
            lambda: all(
                c.action_entries().get(71) and c.action_entries()[71].finished
                for c in [first, second, late]
            )
        )
        for client in [first, second, late]:
            assert client.action_entries()[71].output == "first live line\nlast live line\n"
        second.dismiss_action(71)
        _until(lambda: all(71 not in c.action_entries() for c in [first, second, late]))
        assert not unrelated.action_entries()
        assert not excluded.action_entries()
    finally:
        release.set()
        for client in clients:
            client.close()
