"""Service requests settle through the public state client, without hanging."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from jailbee.dashboard.model import RepoGroup
from jailbee.dashboard.runner import OUTPUT_DROPPED_MARKER, OUTPUT_KEEP_CHARS, ActionRun
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
    server = StateServer(
        FakeGatherer(), version="v1", action_spawn=lambda *a, **kw: Child(kw["prompt_sock"])
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
    )
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(None, dashboard_id="origin", version="v1", spawn=lambda: None)
    client.start()
    try:
        client.wait_first_snapshot(3)
        client.run_action(
            RunAction(
                55, ["ls"], str(tmp_path), "target", False, "origin", "none", str(tmp_path), "repo"
            )
        )
        _until(
            lambda: bool(client.action_entries().get(55) and client.action_entries()[55].finished)
        )

        class Progress:
            def entry_id(self, index):
                return 55

            def output(self, index, chunk):
                pass

            def question(self, index, asking):
                pass

        with ThreadPoolExecutor() as pool:
            pending = pool.submit(
                run_actions,
                client,
                [ActionRun(["ls"], tmp_path)],
                Progress(),
                groups=[],
                transport="none",
            )
            try:
                outcome = pending.result(2)
                assert "duplicate" in outcome.results[0].error
            finally:
                client.close()
    finally:
        client.close()


def test_capped_service_tail_replaces_local_output_instead_of_appending_old_text(tmp_path):
    from jailbee.state_service.protocol import ActionEntry

    first = "x" * OUTPUT_KEEP_CHARS
    final = OUTPUT_DROPPED_MARKER + "x" * (OUTPUT_KEEP_CHARS - 5) + "final"

    class Client:
        dashboard_id = "origin"

        def __init__(self):
            self.sent = False
            self.reads = 0

        def run_action(self, request):
            self.sent = True

        def action_connection(self):
            return 1

        def status(self):
            return None

        def action_rejection(self, entry_id):
            return None

        def action_entries(self):
            if not self.sent:
                return {}
            self.reads += 1
            entry = ActionEntry(
                91,
                ["ls"],
                str(tmp_path),
                "repo",
                False,
                "origin",
                "none",
                str(tmp_path),
                "repo",
                output=first,
            )
            if self.reads >= 2:
                entry = replace(entry, output=final, finished=True, status=0)
            return {91: entry}

    class Progress:
        text = ""

        def entry_id(self, index):
            return 91

        def output(self, index, chunk):
            self.text += chunk

        def replace_output(self, index, text):
            self.text = text

        def question(self, index, asking):
            pass

    progress = Progress()
    outcome = run_actions(
        Client(),
        [ActionRun(["ls"], tmp_path)],
        progress,
        groups=[RepoGroup("repo", str(tmp_path), None, [])],
        transport="none",
    )
    assert progress.text == final
    assert outcome.outputs == [final]
    assert outcome.output == final


def test_batch_resumes_after_disconnect_without_resubmitting_accepted_action(tmp_path, monkeypatch):
    from jailbee.state_service.protocol import ActionEntry

    monkeypatch.setattr("jailbee.dashboard.service_actions.POLL_SECONDS", 0)

    class Client:
        dashboard_id = "origin"

        def __init__(self):
            self.entries = {}
            self.sent = []
            self.polls = 0

        def action_connection(self):
            self.polls += 1
            return None if 3 <= self.polls <= 5 else 1 if self.polls < 3 else 2

        def status(self):
            return None

        def action_rejection(self, entry_id):
            return None

        def action_entries(self):
            return self.entries.copy()

        def run_action(self, request):
            self.sent.append(request.entry_id)
            self.entries[request.entry_id] = ActionEntry(
                request.entry_id,
                request.argv,
                request.cwd,
                request.target,
                False,
                request.origin,
                request.transport,
                request.repo_root,
                request.prefix,
                finished=True,
                status=0,
                output="ok",
            )
            if len(self.sent) == 1:
                raise BrokenPipeError("ambiguous send")

    class Progress:
        def entry_id(self, index):
            return index + 1

        def output(self, index, chunk):
            pass

        def question(self, index, asking):
            pass

    client = Client()
    outcome = run_actions(
        client, [ActionRun(["ls"], tmp_path)] * 2, Progress(), groups=[], transport="none"
    )
    assert client.sent == [1, 2]
    assert [result.status for result in outcome.results] == [0, 0]


def test_disconnect_timeout_returns_partial_output(tmp_path, monkeypatch):
    monkeypatch.setattr("jailbee.dashboard.service_actions.RECONNECT_TIMEOUT_SECONDS", 0)

    class Client:
        dashboard_id = "origin"

        def action_connection(self):
            return None

        def action_entries(self):
            return {}

        def action_rejection(self, entry_id):
            return None

        def status(self):
            return "offline"

        def run_action(self, request):
            raise AssertionError("must not send while disconnected")

    outcome = run_actions(Client(), [ActionRun(["ls"], tmp_path)], groups=[], transport="none")
    assert outcome.results[0].error == "offline"
    assert outcome.outputs == [""]
