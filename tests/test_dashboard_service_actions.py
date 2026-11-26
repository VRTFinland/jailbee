"""Service requests settle through the public state client, without hanging."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from jailbee.dashboard.runner import ActionRun
from jailbee.dashboard.service_actions import run_actions
from jailbee.state_service import paths
from jailbee.state_service.client import StateClient
from jailbee.state_service.protocol import RunAction
from jailbee.state_service.server import StateServer
from tests.test_state_client import _until
from tests.test_state_server import FakeGatherer, reap_servers, track_server  # noqa: F401


def test_rejected_request_settles_instead_of_waiting_forever(runtime_dir, tmp_path):
    class Child:
        stdout = None

        def __init__(self, sock):
            sock.close()

        def wait(self):
            return 0

        def kill(self):
            pass

    paths.ensure_runtime_dir()
    server = StateServer(FakeGatherer(), version="v1", action_spawn=lambda *a, **kw: Child(kw["prompt_sock"]))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True)
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(None, dashboard_id="origin", version="v1", spawn=lambda: None)
    client.start()
    try:
        client.wait_first_snapshot(3)
        client.run_action(RunAction(55, ["ls"], str(tmp_path), "target", False, "origin", "none", str(tmp_path), "repo"))
        _until(lambda: bool(client.action_entries().get(55) and client.action_entries()[55].finished))

        class Progress:
            def entry_id(self, index):
                return 55

            def output(self, index, chunk):
                pass

            def question(self, index, asking):
                pass

        with ThreadPoolExecutor() as pool:
            pending = pool.submit(run_actions, client, [ActionRun(["ls"], tmp_path)], Progress(), groups=[], transport="none")
            try:
                outcome = pending.result(2)
                assert "duplicate" in outcome.results[0].error
            finally:
                client.close()
    finally:
        client.close()
