"""Action hosting over a real state service connection."""

import asyncio
import os
import threading
import time

import pytest

from jailbee.state_service.client import StateClient
from jailbee.state_service.protocol import RunAction
from jailbee.state_service.server import StateServer
from tests.test_state_client import _until
from tests.test_state_server import FakeGatherer, reap_servers, track_server  # noqa: F401


def test_running_action_keeps_server_alive_until_idle_after_completion(runtime_dir, tmp_path):
    from jailbee.state_service import paths

    started = threading.Event()
    finish = threading.Event()

    class Child:
        stdout = None

        def __init__(self, sock):
            sock.close()
            started.set()

        def wait(self):
            finish.wait(3)
            return 0

        def kill(self):
            finish.set()

    paths.ensure_runtime_dir()
    server = StateServer(
        FakeGatherer(),
        version="v1",
        idle_timeout=0.3,
        action_spawn=lambda *a, **kw: Child(kw["prompt_sock"]),
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
    )
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(
        None, dashboard_id="one", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    client.start()
    client.wait_first_snapshot(3)
    try:
        client.run_action(
            RunAction(42, ["ls"], str(tmp_path), "t", False, "one", "none", str(tmp_path), "repo")
        )
        assert started.wait(2)
        client.close()
        time.sleep(0.6)
        assert thread.is_alive()
        finish.set()
        time.sleep(0.1)
        assert thread.is_alive()
        thread.join(2)
        assert not thread.is_alive()
    finally:
        finish.set()
        client.close()


def test_question_survives_last_disconnect(runtime_dir, tmp_path):
    from jailbee.prompt_channel import CommandEnd, Prompt
    from jailbee.state_service import paths

    answers = []

    class Child:
        stdout = None

        def __init__(self, sock):
            def ask():
                command = CommandEnd(sock)
                answers.append(command.ask(lambda n: Prompt(n, "confirm", "Proceed?")))
                command.close()

            self.thread = threading.Thread(target=ask)
            self.thread.start()

        def wait(self):
            self.thread.join(timeout=3)
            return 0

        def kill(self):
            pass

    paths.ensure_runtime_dir()
    server = StateServer(
        FakeGatherer(),
        version="v1",
        idle_timeout=0.3,
        action_spawn=lambda *a, **kw: Child(kw["prompt_sock"]),
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
    )
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    first = StateClient(None, dashboard_id="one", version="v1", spawn=lambda: None, backoff=(0.05,))
    first.start()
    first.wait_first_snapshot(3)
    first.run_action(
        RunAction(43, ["ls"], str(tmp_path), "t", False, "one", "none", str(tmp_path), "repo")
    )
    _until(lambda: 43 in first.action_questions())
    first.close()
    time.sleep(0.6)
    assert thread.is_alive()
    second = StateClient(
        None, dashboard_id="two", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    second.start()
    try:
        second.wait_first_snapshot(3)
        _until(lambda: 43 in second.action_questions())
        second.answer_action_question(43, second.action_questions()[43].question_id, True)
        _until(lambda: bool(answers))
        assert answers[0].value is True
    finally:
        second.close()


def test_shutdown_kills_verbose_child_and_persists_interrupted(runtime_dir, tmp_path):
    from sqlalchemy import create_engine

    from jailbee.db import _ensure_schema
    from jailbee.db.actions import load
    from jailbee.prompt_channel import CommandEnd, Prompt
    from jailbee.state_service import paths
    from jailbee.state_service.protocol import Shutdown
    from tests.test_state_server import Conn

    children = []

    class Child:
        def __init__(self, sock):
            self.sock = sock
            self.stopped = threading.Event()
            read, self.write = os.pipe()
            self.stdout = os.fdopen(read, "rb")

            def work():
                try:
                    os.write(self.write, b"x" * 4096)
                    command = CommandEnd(sock)
                    command.ask(lambda n: Prompt(n, "confirm", "Proceed?"))
                    command.close()
                except (OSError, EOFError):
                    pass
                finally:
                    os.close(self.write)
                    self.stopped.set()

            self.thread = threading.Thread(target=work)
            self.thread.start()
            children.append(self)

        def wait(self):
            self.thread.join(3)
            return -9

        def kill(self):
            self.sock.shutdown(2)

    paths.ensure_runtime_dir()
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    _ensure_schema(engine)
    server = StateServer(
        FakeGatherer(),
        version="v1",
        engine=engine,
        action_spawn=lambda *a, **kw: Child(kw["prompt_sock"]),
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
    )
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(
        None, dashboard_id="one", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    client.start()
    client.wait_first_snapshot(3)
    try:
        client.run_action(
            RunAction(44, ["ls"], str(tmp_path), "t", False, "one", "none", str(tmp_path), "repo")
        )
        _until(lambda: 44 in client.action_questions())
        conn = Conn(paths.socket_path(), version="v0")
        conn.send(Shutdown())
        thread.join(4)
        assert not thread.is_alive()
        assert children[0].stopped.is_set()
        assert load(engine)[0].state == "interrupted"
        conn.sock.close()
    finally:
        client.close()
        engine.dispose()


def test_service_action_uses_request_environment_not_server_environment(
    runtime_dir, tmp_path, monkeypatch
):
    captured = {}

    class Child:
        stdout = None

        def __init__(self, prompt_sock):
            prompt_sock.close()

        def wait(self):
            return 0

        def kill(self):
            pass

    def spawn(_argv, *, cwd, env, prompt_sock):
        captured.update(env)
        return Child(prompt_sock)

    monkeypatch.setenv("JAILBEE_REMOTE_SESSION", "1")
    monkeypatch.setenv("JAILBEE_REMOTE_TRANSPORT", "ssh")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/service/agent.sock")
    monkeypatch.setenv("DISPLAY", ":service")

    from jailbee.state_service import paths

    paths.ensure_runtime_dir()
    server = StateServer(FakeGatherer(), version="v1", action_spawn=spawn)
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
    )
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(
        None, dashboard_id="dashboard", transport="none", version="v1", spawn=lambda: None
    )
    client.start()
    try:
        client.wait_first_snapshot(3)
        client.run_action(
            RunAction(
                777,
                ["ls"],
                str(tmp_path),
                "target",
                False,
                "dashboard",
                "none",
                str(tmp_path),
                "repo",
                environment={"PATH": "/request/bin", "DISPLAY": ":request"},
                excluded_prefixes=["secret-repo"],
            )
        )
        _until(
            lambda: (
                client.action_entries().get(777, None) is not None
                and client.action_entries()[777].finished
            )
        )
        assert captured["PATH"] == "/request/bin"
        assert captured["DISPLAY"] == ":request"
        assert "SSH_AUTH_SOCK" not in captured
        assert "JAILBEE_REMOTE_SESSION" not in captured
        assert "JAILBEE_REMOTE_TRANSPORT" not in captured
        assert client.action_entries()[777].excluded_prefixes == ["secret-repo"]
    finally:
        client.close()


def test_remote_refusal_precedes_spawn_and_local_is_unaffected(runtime_dir, tmp_path, monkeypatch):
    from jailbee import global_config
    from jailbee.config.models_remote import RemoteCommandPolicy
    from jailbee.state_service import paths

    config = global_config.GlobalConfig()
    config.remote.ssh.commands = RemoteCommandPolicy(mode="disabled")
    monkeypatch.setattr(global_config, "load_global_config", lambda path: (config, []))
    spawned = []

    class Child:
        stdout = None

        def __init__(self, sock):
            sock.close()

        def wait(self):
            return 0

        def kill(self):
            pass

    def spawn(*args, **kwargs):
        spawned.append(True)
        return Child(kwargs["prompt_sock"])

    paths.ensure_runtime_dir()
    server = StateServer(FakeGatherer(), version="v1", action_spawn=spawn)
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
    )
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    remote = StateClient(
        None, dashboard_id="remote", transport="ssh", version="v1", spawn=lambda: None
    )
    local = StateClient(
        None, dashboard_id="local", transport="none", version="v1", spawn=lambda: None
    )
    remote.start()
    local.start()
    try:
        remote.wait_first_snapshot(3)
        local.wait_first_snapshot(3)
        remote.run_action(
            RunAction(
                111,
                ["destroy", "x"],
                str(tmp_path),
                "x",
                False,
                "remote",
                "none",
                str(tmp_path),
                "repo",
            )
        )
        _until(lambda: remote.action_rejection(111) is not None)
        assert not spawned
        assert 111 not in remote.action_entries()
        local.run_action(
            RunAction(
                112,
                ["destroy", "x"],
                str(tmp_path),
                "x",
                False,
                "local",
                "ssh",
                str(tmp_path),
                "repo",
            )
        )
        _until(
            lambda: (
                local.action_entries().get(112) is not None and local.action_entries()[112].finished
            )
        )
        assert spawned == [True]
    finally:
        remote.close()
        local.close()


@pytest.mark.parametrize("answer", [True, None])
def test_remote_cannot_answer_or_abort_local_question(runtime_dir, tmp_path, monkeypatch, answer):
    from jailbee import global_config
    from jailbee.config.models_remote import RemoteCommandPolicy
    from jailbee.prompt_channel import CommandEnd, Prompt
    from jailbee.state_service import paths

    config = global_config.GlobalConfig()
    config.remote.ssh.commands = RemoteCommandPolicy(mode="disabled")
    monkeypatch.setattr(global_config, "load_global_config", lambda path: (config, []))
    from jailbee.remote_ssh import repo_scope

    monkeypatch.setattr(
        repo_scope, "registered_repos", lambda **kwargs: [repo_scope.RepoChoice("repo", tmp_path)]
    )
    answers = []

    class Child:
        stdout = None

        def __init__(self, sock):
            def ask():
                channel = CommandEnd(sock)
                answers.append(channel.ask(lambda n: Prompt(n, "confirm", "Proceed?")))
                channel.close()

            self.thread = threading.Thread(target=ask, daemon=True)
            self.thread.start()

        def wait(self):
            self.thread.join(5)
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
    local = StateClient(
        None, dashboard_id="local", transport="none", version="v1", spawn=lambda: None
    )
    remote = StateClient(
        None, dashboard_id="remote", transport="ssh", version="v1", spawn=lambda: None
    )
    local.start()
    remote.start()
    try:
        local.wait_first_snapshot(3)
        remote.wait_first_snapshot(3)
        local.run_action(
            RunAction(
                113,
                ["destroy", "x"],
                str(tmp_path),
                "x",
                False,
                "local",
                "none",
                str(tmp_path),
                "repo",
            )
        )
        _until(lambda: 113 in remote.action_questions())
        question = remote.action_questions()[113]
        assert not question.can_answer
        assert question.reason == "answer in a local dashboard"
        remote.answer_action_question(113, question.question_id, answer)
        _until(lambda: remote.action_rejection(113) is not None)
        assert not answers
        assert 113 in local.action_questions()
        local.answer_action_question(113, question.question_id, True)
        _until(lambda: bool(answers))
        assert answers[0].value is True
    finally:
        local.close()
        remote.close()


def test_allowed_ssh_action_ignores_service_environment(runtime_dir, tmp_path, monkeypatch):
    from jailbee import global_config
    from jailbee.config.models_remote import RemoteCommandPolicy
    from jailbee.state_service import paths

    config = global_config.GlobalConfig()
    config.remote.ssh.commands = RemoteCommandPolicy(mode="allowlist", allow=["ls"])
    monkeypatch.setattr(global_config, "load_global_config", lambda path: (config, []))
    from jailbee.remote_ssh import repo_scope

    monkeypatch.setattr(
        repo_scope, "registered_repos", lambda **kwargs: [repo_scope.RepoChoice("repo", tmp_path)]
    )
    monkeypatch.setenv("JAILBEE_REMOTE_SESSION", "1")
    monkeypatch.setenv("JAILBEE_REMOTE_TRANSPORT", "ssh")
    monkeypatch.delenv("JAILBEE_REMOTE_EXCLUDED_REPOS", raising=False)
    spawned = []

    class Child:
        stdout = None

        def __init__(self, sock):
            sock.close()

        def wait(self):
            return 0

        def kill(self):
            pass

    def spawn(*args, **kwargs):
        spawned.append(kwargs["env"])
        return Child(kwargs["prompt_sock"])

    paths.ensure_runtime_dir()
    server = StateServer(FakeGatherer(), version="v1", action_spawn=spawn)
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
    )
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(
        None, dashboard_id="remote", transport="ssh", version="v1", spawn=lambda: None
    )
    client.start()
    try:
        client.wait_first_snapshot(3)
        client.run_action(
            RunAction(
                114,
                ["ls"],
                str(tmp_path),
                "x",
                False,
                "remote",
                "none",
                str(tmp_path),
                "repo",
                environment={"PATH": "/bin"},
            )
        )
        _until(lambda: 114 in client.action_entries() and client.action_entries()[114].finished)
        assert client.action_entries()[114].status == 0
        assert len(spawned) == 1
    finally:
        client.close()


def test_global_restricted_remote_sees_orphan_question_read_only(
    runtime_dir, tmp_path, monkeypatch
):
    from jailbee import global_config
    from jailbee.config.models_remote import RemoteCommandPolicy
    from jailbee.prompt_channel import CommandEnd, Prompt
    from jailbee.state_service import paths

    config = global_config.GlobalConfig()
    config.remote.ssh.commands = RemoteCommandPolicy(mode="full")
    config.remote.ssh.restrict_host = True
    monkeypatch.setattr(global_config, "load_global_config", lambda path: (config, []))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    answers = []

    class Child:
        stdout = None

        def __init__(self, sock):
            def ask():
                channel = CommandEnd(sock)
                answers.append(channel.ask(lambda n: Prompt(n, "confirm", "Proceed?")))
                channel.close()

            self.thread = threading.Thread(target=ask, daemon=True)
            self.thread.start()

        def wait(self):
            self.thread.join(5)
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
    local = StateClient(
        None, dashboard_id="local", transport="none", version="v1", spawn=lambda: None
    )
    remote = StateClient(
        None, dashboard_id="remote", transport="ssh", version="v1", spawn=lambda: None
    )
    local.start()
    remote.start()
    try:
        local.wait_first_snapshot(3)
        remote.wait_first_snapshot(3)
        local.run_action(
            RunAction(
                118,
                ["orphan", "destroy", "x"],
                str(tmp_path),
                "x",
                False,
                "local",
                "none",
                str(tmp_path),
                "",
            )
        )
        _until(lambda: 118 in remote.action_questions())
        question = remote.action_questions()[118]
        assert not question.can_answer
        assert question.reason == "answer in a local dashboard"
        remote.answer_action_question(118, question.question_id, None)
        _until(lambda: remote.action_rejection(118) is not None)
        assert not answers
        local.answer_action_question(118, question.question_id, True)
        _until(lambda: bool(answers))
    finally:
        local.close()
        remote.close()


def test_unrestricted_remote_orphan_runs_from_home(runtime_dir, tmp_path, monkeypatch):
    from jailbee import global_config
    from jailbee.config.models_remote import RemoteCommandPolicy
    from jailbee.state_service import paths

    config = global_config.GlobalConfig()
    config.remote.ssh.commands = RemoteCommandPolicy(mode="allowlist", allow=["orphan destroy"])
    config.remote.ssh.restrict_host = False
    monkeypatch.setattr(global_config, "load_global_config", lambda path: (config, []))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    spawned = []

    class Child:
        stdout = None

        def __init__(self, sock):
            sock.close()

        def wait(self):
            return 0

        def kill(self):
            pass

    def spawn(*args, **kwargs):
        spawned.append(kwargs["cwd"])
        return Child(kwargs["prompt_sock"])

    paths.ensure_runtime_dir()
    server = StateServer(FakeGatherer(), version="v1", action_spawn=spawn)
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
    )
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(
        None, dashboard_id="remote", transport="ssh", version="v1", spawn=lambda: None
    )
    client.start()
    try:
        client.wait_first_snapshot(3)
        client.run_action(
            RunAction(
                117,
                ["orphan", "destroy", "x"],
                str(tmp_path),
                "x",
                False,
                "remote",
                "none",
                str(tmp_path),
                "arbitrary",
                environment={"PATH": "/bin"},
            )
        )
        _until(
            lambda: (
                client.action_rejection(117) is not None
                or (117 in client.action_entries() and client.action_entries()[117].finished)
            )
        )
        assert client.action_rejection(117) is None
        assert spawned == [tmp_path]
    finally:
        client.close()


def test_remote_orphan_policy_is_checked_before_repo_scope(runtime_dir, tmp_path, monkeypatch):
    from jailbee import global_config
    from jailbee.config.models_remote import RemoteCommandPolicy
    from jailbee.remote_ssh import repo_scope
    from jailbee.state_service import paths

    config = global_config.GlobalConfig()
    config.remote.ssh.commands = RemoteCommandPolicy(mode="allowlist", allow=["orphan destroy"])
    config.remote.ssh.restrict_host = True
    monkeypatch.setattr(global_config, "load_global_config", lambda path: (config, []))
    monkeypatch.setattr(
        repo_scope, "registered_repos", lambda **kwargs: [repo_scope.RepoChoice("repo", tmp_path)]
    )
    spawned = []
    paths.ensure_runtime_dir()
    server = StateServer(
        FakeGatherer(), version="v1", action_spawn=lambda *a, **kw: spawned.append(True)
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
    )
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(
        None, dashboard_id="remote", transport="ssh", version="v1", spawn=lambda: None
    )
    client.start()
    try:
        client.wait_first_snapshot(3)
        client.run_action(
            RunAction(
                116,
                ["orphan", "destroy", "x"],
                str(tmp_path),
                "x",
                False,
                "remote",
                "none",
                str(tmp_path),
                "repo",
            )
        )
        _until(lambda: client.action_rejection(116) is not None)
        assert not spawned
        assert 116 not in client.action_entries()
    finally:
        client.close()


def test_remote_cannot_spoof_registered_repo_scope(runtime_dir, tmp_path, monkeypatch):
    from jailbee import global_config
    from jailbee.config.models_remote import RemoteCommandPolicy
    from jailbee.remote_ssh import repo_scope
    from jailbee.state_service import paths

    public = tmp_path / "public"
    private = tmp_path / "private"
    public.mkdir()
    private.mkdir()
    config = global_config.GlobalConfig()
    config.remote.ssh.commands = RemoteCommandPolicy(mode="allowlist", allow=["ls"])
    config.remote.ssh.excluded_repos = ["private"]
    monkeypatch.setattr(global_config, "load_global_config", lambda path: (config, []))
    monkeypatch.setattr(
        repo_scope,
        "registered_repos",
        lambda **kwargs: [
            repo_scope.RepoChoice("public", public),
            repo_scope.RepoChoice("private", private),
        ],
    )
    spawned = []
    paths.ensure_runtime_dir()
    server = StateServer(
        FakeGatherer(), version="v1", action_spawn=lambda *a, **kw: spawned.append(True)
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
    )
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(
        None, dashboard_id="remote", transport="ssh", version="v1", spawn=lambda: None
    )
    client.start()
    try:
        client.wait_first_snapshot(3)
        client.run_action(
            RunAction(
                115,
                ["ls"],
                str(private),
                "x",
                False,
                "remote",
                "none",
                str(public),
                "public",
                environment={"PATH": "/bin"},
            )
        )
        _until(lambda: client.action_rejection(115) is not None)
        assert not spawned
        assert 115 not in client.action_entries()
    finally:
        client.close()


def test_batch_continues_after_origin_dashboard_closes(runtime_dir, tmp_path):
    from jailbee.state_service import paths
    from jailbee.state_service.protocol import RunActionBatch

    started = threading.Event()
    release = threading.Event()
    second = threading.Event()

    class Child:
        stdout = None

        def __init__(self, sock, first):
            sock.close()
            self.first = first
            (started if first else second).set()

        def wait(self):
            if self.first:
                assert release.wait(10)
            return 0

        def kill(self):
            release.set()

    paths.ensure_runtime_dir()
    server = StateServer(
        FakeGatherer(), version="v1",
        action_spawn=lambda argv, **kw: Child(kw["prompt_sock"], "first" in argv),
    )
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True)
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(None, dashboard_id="one", version="v1", spawn=lambda: None, backoff=(0.05,))
    client.start()
    try:
        client.wait_first_snapshot(3)
        requests = [
            RunAction(i, [name], str(tmp_path), "x", False, "one", "none", str(tmp_path), "repo")
            for i, name in [(101, "first"), (102, "second")]
        ]
        client.run_action_batch(RunActionBatch(requests))
        assert started.wait(10)
        _until(lambda: set(client.action_entries()) >= {101, 102})
        assert not second.is_set()
        client.close()
        release.set()
        assert second.wait(10)
    finally:
        release.set()
        client.close()


def test_batch_rejects_duplicate_ids_without_starting_any_action(runtime_dir, tmp_path):
    from jailbee.state_service import paths
    from jailbee.state_service.protocol import RunActionBatch

    spawned = []
    paths.ensure_runtime_dir()
    server = StateServer(FakeGatherer(), version="v1", action_spawn=lambda *a, **kw: spawned.append(1))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True)
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(None, dashboard_id="one", version="v1", spawn=lambda: None, backoff=(0.05,))
    client.start()
    try:
        client.wait_first_snapshot(3)
        request = RunAction(101, ["ls"], str(tmp_path), "x", False, "one", "none", str(tmp_path), "repo")
        client.run_action_batch(RunActionBatch([request, request]))
        _until(lambda: client.action_rejection(101) is not None)
        assert not spawned
        assert not client.action_entries()
    finally:
        client.close()


def test_action_streams_output_and_result(runtime_dir, tmp_path):
    class Child:
        def __init__(self, prompt_sock):
            prompt_sock.close()
            read, write = os.pipe()
            os.write(write, b"hello\n")
            os.close(write)
            self.stdout = os.fdopen(read, "rb")

        def wait(self):
            return 7

        def kill(self):
            pass

    from jailbee.state_service import paths

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
    client = StateClient(
        None, dashboard_id="dashboard", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    client.start()
    try:
        client.wait_first_snapshot(3)
        client.run_action(
            RunAction(
                123,
                ["ls"],
                str(tmp_path),
                "target",
                False,
                "dashboard",
                "none",
                str(tmp_path),
                "repo",
            )
        )
        _until(
            lambda: bool(client.action_entries().get(123) and client.action_entries()[123].finished)
        )
        assert client.action_entries()[123].status == 7
        assert client.action_entries()[123].output == "hello\n"
    finally:
        client.close()


def test_action_events_are_broadcast_to_same_scope_clients(runtime_dir, tmp_path):
    class Child:
        stdout = None

        def __init__(self, sock):
            sock.close()

        def wait(self):
            return 0

        def kill(self):
            pass

    from jailbee.state_service import paths

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

    def connect(identity):
        client = StateClient(
            tmp_path, dashboard_id=identity, version="v1", spawn=lambda: None, backoff=(0.05,)
        )
        client.start()
        client.wait_first_snapshot(3)
        return client

    first, second = connect("one"), connect("two")
    try:
        first.run_action(
            RunAction(
                123,
                ["ls"],
                str(tmp_path),
                "target",
                False,
                "one",
                "none",
                str(tmp_path),
                "repo",
            )
        )
        _until(lambda: 123 in first.action_entries() and 123 in second.action_entries())
        _until(
            lambda: first.action_entries()[123].finished and second.action_entries()[123].finished
        )
        assert first.action_entries()[123].output == second.action_entries()[123].output
    finally:
        first.close()
        second.close()


def test_late_client_gets_finished_action_and_can_dismiss_for_all(runtime_dir, tmp_path):
    class Child:
        stdout = None

        def __init__(self, sock):
            sock.close()

        def wait(self):
            return 0

        def kill(self):
            pass

    from jailbee.state_service import paths

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

    def connect(identity):
        client = StateClient(
            tmp_path, dashboard_id=identity, version="v1", spawn=lambda: None, backoff=(0.05,)
        )
        client.start()
        client.wait_first_snapshot(3)
        return client

    first = connect("one")
    first.run_action(
        RunAction(321, ["ls"], str(tmp_path), "target", False, "one", "none", str(tmp_path), "repo")
    )
    _until(
        lambda: first.action_entries().get(321) is not None and first.action_entries()[321].finished
    )
    late = connect("late")
    try:
        _until(lambda: 321 in late.action_entries() and late.action_entries()[321].finished)
        assert late.action_entries()[321].output == first.action_entries()[321].output
        late.dismiss_action(321)
        _until(lambda: 321 not in first.action_entries() and 321 not in late.action_entries())
    finally:
        first.close()
        late.close()


def test_wire_action_messages_round_trip():
    from jailbee.state_service.protocol import (
        ActionEntry,
        ActionFinish,
        ActionOutput,
        ActionRejected,
        decode,
        encode,
    )

    messages = [
        RunAction(1, ["ls"], "/repo", "a", True, "dashboard", "none", "/repo", "r"),
        ActionEntry(1, ["ls"], "/repo", "a", True, "dashboard", "none", "/repo", "r"),
        ActionOutput(1, "text"),
        ActionFinish(1, 0),
        ActionRejected(1, "duplicate"),
    ]
    for message in messages:
        assert decode(encode(message)) == message


@pytest.mark.parametrize("value", [True, None])
def test_question_is_answered_by_first_dashboard_over_real_channel(runtime_dir, tmp_path, value):
    from jailbee.prompt_channel import Cancel, CommandEnd, Prompt
    from jailbee.state_service import paths

    answers = []

    class Child:
        stdout = None

        def __init__(self, sock):
            def ask():
                command = CommandEnd(sock)
                answers.append(command.ask(lambda n: Prompt(n, "confirm", "Proceed?")))
                command.close()

            self.thread = threading.Thread(target=ask)
            self.thread.start()

        def wait(self):
            self.thread.join(timeout=3)
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
    client = StateClient(
        None, dashboard_id="dashboard", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    client.start()
    second = StateClient(
        None, dashboard_id="second", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    second.start()
    outsider = StateClient(
        None,
        dashboard_id="outside",
        excluded_prefixes=("repo",),
        version="v1",
        spawn=lambda: None,
        backoff=(0.05,),
    )
    outsider.start()
    try:
        client.wait_first_snapshot(3)
        second.wait_first_snapshot(3)
        outsider.wait_first_snapshot(3)
        client.run_action(
            RunAction(
                17,
                ["ls"],
                str(tmp_path),
                "target",
                False,
                "dashboard",
                "none",
                str(tmp_path),
                "repo",
            )
        )
        _until(lambda: 17 in client.action_questions() and 17 in second.action_questions())
        question = second.action_questions()[17]
        assert client.action_entries()[17].state == "awaiting"
        assert question.prompt.question == "Proceed?"
        late = StateClient(
            None, dashboard_id="late", version="v1", spawn=lambda: None, backoff=(0.05,)
        )
        late.start()
        try:
            late.wait_first_snapshot(3)
            _until(lambda: 17 in late.action_questions())
            assert late.action_questions()[17] == question
        finally:
            late.close()
        assert 17 not in outsider.action_questions()
        outsider.answer_action_question(17, question.question_id, False)
        second.answer_action_question(17, question.question_id + 1, False)
        second.answer_action_question(17, question.question_id, value)
        second.answer_action_question(17, question.question_id, False)
        _until(lambda: bool(answers))
        if value is None:
            assert isinstance(answers[0], Cancel)
        else:
            assert answers[0].value is True
        _until(lambda: 17 not in client.action_questions() and 17 not in second.action_questions())
    finally:
        client.close()
        second.close()
        outsider.close()


def test_prompt_channel_eof_clears_question(runtime_dir, tmp_path):
    from jailbee.prompt_channel import CommandEnd, Prompt
    from jailbee.state_service import paths

    release = threading.Event()

    class Child:
        stdout = None

        def __init__(self, sock):
            self.sock = sock

            def ask():
                command = CommandEnd(sock)
                try:
                    command.ask(lambda n: Prompt(n, "text", "Name?"))
                except (OSError, EOFError, ValueError):
                    pass

            self.thread = threading.Thread(target=ask, daemon=True)
            self.thread.start()

            def close_channel():
                release.wait(5)
                sock.shutdown(2)
                sock.close()

            threading.Thread(target=close_channel, daemon=True).start()

        def wait(self):
            self.thread.join(timeout=3)
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
    client = StateClient(
        None, dashboard_id="origin", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    client.start()
    try:
        client.wait_first_snapshot(3)
        client.run_action(
            RunAction(
                19, ["ls"], str(tmp_path), "target", False, "origin", "none", str(tmp_path), "repo"
            )
        )
        _until(lambda: 19 in client.action_questions())
        release.set()
        _until(lambda: 19 not in client.action_questions())
        _until(lambda: client.action_entries()[19].finished)
        assert client.action_entries()[19].cancelled
    finally:
        release.set()
        client.close()


def test_reconnect_replays_entries_in_repository_scope_and_rejects_duplicate(runtime_dir, tmp_path):
    from jailbee.state_service import paths

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

    def connect(identity, cwd_root=None):
        client = StateClient(
            cwd_root, dashboard_id=identity, version="v1", spawn=lambda: None, backoff=(0.05,)
        )
        client.start()
        client.wait_first_snapshot(3)
        return client

    first = connect("one")
    request = RunAction(
        55, ["ls"], str(tmp_path), "target", False, "one", "none", str(tmp_path), "repo"
    )
    first.run_action(request)
    _until(
        lambda: first.action_entries().get(55) is not None and first.action_entries()[55].finished
    )
    first.close()
    other = connect("other", tmp_path / "another-repo")
    again = connect("one")
    try:
        _until(lambda: 55 in again.action_entries())
        assert 55 not in other.action_entries()
        assert again.action_entries()[55].finished
        again.run_action(request)
        _until(lambda: again.action_rejection(55) is not None)
        assert again.action_entries()[55].status == 0
    finally:
        other.close()
        again.close()


def test_large_escaped_output_is_bounded_and_replayed(runtime_dir, tmp_path):
    from jailbee.dashboard.runner import OUTPUT_DROPPED_MARKER, OUTPUT_KEEP_CHARS
    from jailbee.state_service import paths

    text = ('\x01\n\\"' * (OUTPUT_KEEP_CHARS // 2)) + "THE END"
    output_file = tmp_path / "output"
    output_file.write_bytes(text.encode())

    class Child:
        def __init__(self, sock):
            sock.close()
            self.stdout = output_file.open("rb")

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

    def connect():
        client = StateClient(
            None, dashboard_id="large", version="v1", spawn=lambda: None, backoff=(0.05,)
        )
        client.start()
        client.wait_first_snapshot(3)
        return client

    client = connect()
    try:
        client.run_action(
            RunAction(
                99,
                ["ls", "\x01" * 12000],
                str(tmp_path),
                "target",
                False,
                "large",
                "none",
                str(tmp_path),
                "repo",
            )
        )
        _until(
            lambda: (
                client.action_entries().get(99) is not None and client.action_entries()[99].finished
            )
        )
        expected = OUTPUT_DROPPED_MARKER + text[-OUTPUT_KEEP_CHARS:]
        assert client.action_entries()[99].output == expected
    finally:
        client.close()
    again = connect()
    try:
        _until(lambda: 99 in again.action_entries())
        assert again.action_entries()[99].output == expected
    finally:
        again.close()


def test_slow_action_reader_is_dropped_without_blocking_other_client(
    runtime_dir, tmp_path, monkeypatch
):
    import socket

    from jailbee.state_service import paths
    from jailbee.state_service import server as server_module
    from jailbee.state_service.protocol import PROTOCOL, Hello, encode

    monkeypatch.setattr(server_module, "MAX_ACTION_BUFFER_BYTES", 32768, raising=False)
    source = tmp_path / "large-output"
    source.write_bytes(b"a" * (8 * 1024 * 1024))

    class Child:
        def __init__(self, sock):
            sock.close()
            self.stdout = source.open("rb")

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
    slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    slow.settimeout(5)
    slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    slow.connect(str(paths.socket_path()))
    slow.sendall(encode(Hello(PROTOCOL, "v1", None, "slow")))
    reader = slow.makefile("rb")
    assert reader.readline()
    healthy = StateClient(
        None, dashboard_id="healthy", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    healthy.start()
    try:
        healthy.wait_first_snapshot(3)
        slow.sendall(
            encode(
                RunAction(
                    441,
                    ["ls"],
                    str(tmp_path),
                    "target",
                    False,
                    "slow",
                    "none",
                    str(tmp_path),
                    "repo",
                )
            )
        )
        # Wait until output has saturated the receiver without consuming it.
        _until(lambda: server._actions.get(441) is not None and server._actions[441].finished)
        healthy.refresh()
        assert healthy.latest() is not None
        total = 0
        while chunk := reader.read(65536):
            total += len(chunk)
            assert total < 2 * 1024 * 1024
    finally:
        healthy.close()
        reader.close()
        slow.close()


def test_finished_history_is_bounded_and_evictions_reach_connected_clients(runtime_dir, tmp_path):
    from sqlalchemy import create_engine

    from jailbee.db import _ensure_schema
    from jailbee.state_service import paths

    class Child:
        stdout = None

        def __init__(self, sock):
            sock.close()

        def wait(self):
            return 0

        def kill(self):
            pass

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    _ensure_schema(engine)
    paths.ensure_runtime_dir()
    server = StateServer(
        FakeGatherer(),
        version="v1",
        engine=engine,
        action_spawn=lambda *a, **kw: Child(kw["prompt_sock"]),
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
    )
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(
        None, dashboard_id="dashboard", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    client.start()
    try:
        client.wait_first_snapshot(3)
        for entry_id in range(1, 103):
            client.run_action(
                RunAction(
                    entry_id,
                    ["true"],
                    str(tmp_path),
                    "target",
                    False,
                    "dashboard",
                    "none",
                    str(tmp_path),
                    "repo",
                )
            )
            _until(
                lambda entry_id=entry_id: (
                    client.action_entries().get(entry_id, None) is not None
                    and client.action_entries()[entry_id].finished
                )
            )
        _until(lambda: len(client.action_entries()) == 100)
        assert 1 not in client.action_entries()
        assert 101 in client.action_entries()
    finally:
        client.close()
        server._stopping = True
        thread.join(timeout=5)

    paths.socket_path().unlink(missing_ok=True)
    restarted = StateServer(FakeGatherer(), version="v1", engine=engine)
    thread = threading.Thread(
        target=lambda: asyncio.run(restarted.serve(paths.socket_path())), daemon=True
    )
    thread.start()
    track_server(restarted, thread)
    _until(paths.socket_path().exists)
    recovered = StateClient(
        None, dashboard_id="recovered", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    recovered.start()
    try:
        recovered.wait_first_snapshot(3)
        assert len(recovered.action_entries()) == 100
        assert 1 not in recovered.action_entries()
        assert 102 in recovered.action_entries()
    finally:
        recovered.close()
        restarted._stopping = True
        thread.join(timeout=5)


def test_finished_action_survives_server_restart(runtime_dir, tmp_path):
    from sqlalchemy import create_engine

    from jailbee.db import _ensure_schema
    from jailbee.state_service import paths

    class Child:
        stdout = None

        def __init__(self, sock):
            sock.close()

        def wait(self):
            return 7

        def kill(self):
            pass

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    _ensure_schema(engine)
    paths.ensure_runtime_dir()

    def start_server():
        server = StateServer(
            FakeGatherer(),
            version="v1",
            engine=engine,
            action_spawn=lambda *a, **kw: Child(kw["prompt_sock"]),
        )
        thread = threading.Thread(
            target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
        )
        thread.start()
        track_server(server, thread)
        _until(paths.socket_path().exists)
        return server, thread

    def start_client():
        client = StateClient(
            None, dashboard_id="dashboard", version="v1", spawn=lambda: None, backoff=(0.05,)
        )
        client.start()
        client.wait_first_snapshot(3)
        return client

    server, thread = start_server()
    client = start_client()
    client.run_action(
        RunAction(
            123, ["ls"], str(tmp_path), "target", True, "dashboard", "none", str(tmp_path), "repo"
        )
    )
    _until(lambda: 123 in client.action_entries() and client.action_entries()[123].finished)
    client.close()
    server._stopping = True
    thread.join(timeout=5)
    assert not thread.is_alive()
    paths.socket_path().unlink(missing_ok=True)
    server, thread = start_server()
    client = start_client()
    try:
        _until(lambda: 123 in client.action_entries())
        assert client.action_entries()[123].status == 7
        client.dismiss_action(123)
        _until(lambda: 123 not in client.action_entries())
    finally:
        client.close()
    server._stopping = True
    thread.join(timeout=5)
    assert not thread.is_alive()
    paths.socket_path().unlink(missing_ok=True)
    start_server()
    client = start_client()
    try:
        assert 123 not in client.action_entries()
    finally:
        client.close()


def test_action_does_not_starve_gatherer(runtime_dir, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from jailbee.state_service import paths

    started = threading.Event()
    release = threading.Event()
    gathered_after_start = threading.Event()

    class Gatherer(FakeGatherer):
        def tick(self, **kwargs):
            if started.is_set():
                gathered_after_start.set()
            return super().tick(**kwargs)

    class Child:
        stdout = None

        def __init__(self, sock):
            sock.close()

        def wait(self):
            started.set()
            release.wait(5)
            return 0

        def kill(self):
            release.set()

    paths.ensure_runtime_dir()
    server = StateServer(
        Gatherer(), version="v1", action_spawn=lambda *a, **kw: Child(kw["prompt_sock"])
    )

    def serve():
        async def run():
            asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
            await server.serve(paths.socket_path())

        asyncio.run(run())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(
        None, dashboard_id="dashboard", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    client.start()
    try:
        client.wait_first_snapshot(3)
        client.run_action(
            RunAction(
                123,
                ["ls"],
                str(tmp_path),
                "target",
                False,
                "dashboard",
                "none",
                str(tmp_path),
                "repo",
            )
        )
        assert started.wait(3)
        client.refresh()
        assert gathered_after_start.wait(2)
    finally:
        release.set()
        client.close()


def test_slow_persistence_does_not_block_action_finish(runtime_dir, tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from jailbee.db import _ensure_schema, actions
    from jailbee.state_service import paths

    saving = threading.Event()
    release = threading.Event()
    finish_child = threading.Event()
    original_save = actions.save

    def slow_save(engine, entry):
        saving.set()
        release.wait(5)
        original_save(engine, entry)

    class Child:
        stdout = None

        def __init__(self, sock):
            sock.close()

        def wait(self):
            finish_child.wait(5)
            return 0

        def kill(self):
            finish_child.set()

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    _ensure_schema(engine)
    monkeypatch.setattr(actions, "save", slow_save)
    paths.ensure_runtime_dir()
    server = StateServer(
        FakeGatherer(),
        version="v1",
        engine=engine,
        action_spawn=lambda *a, **kw: Child(kw["prompt_sock"]),
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
    )
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(
        None, dashboard_id="dashboard", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    client.start()
    try:
        client.wait_first_snapshot(3)
        client.run_action(
            RunAction(
                123,
                ["ls"],
                str(tmp_path),
                "target",
                False,
                "dashboard",
                "none",
                str(tmp_path),
                "repo",
            )
        )
        assert saving.wait(3)
        finish_child.set()
        _until(lambda: 123 in client.action_entries() and client.action_entries()[123].finished)
    finally:
        release.set()
        finish_child.set()
        client.close()


def test_save_failure_does_not_prevent_action_completion(runtime_dir, tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from jailbee.db import _ensure_schema, actions
    from jailbee.state_service import paths

    class Child:
        stdout = None

        def __init__(self, sock):
            sock.close()

        def wait(self):
            return 0

        def kill(self):
            pass

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    _ensure_schema(engine)

    def fail_save(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(actions, "save", fail_save)
    paths.ensure_runtime_dir()
    server = StateServer(
        FakeGatherer(),
        version="v1",
        engine=engine,
        action_spawn=lambda *a, **kw: Child(kw["prompt_sock"]),
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
    )
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(
        None, dashboard_id="dashboard", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    client.start()
    try:
        client.wait_first_snapshot(3)
        client.run_action(
            RunAction(
                123,
                ["ls"],
                str(tmp_path),
                "target",
                False,
                "dashboard",
                "none",
                str(tmp_path),
                "repo",
            )
        )
        _until(lambda: 123 in client.action_entries() and client.action_entries()[123].finished)
        assert client.action_entries()[123].status == 0
    finally:
        client.close()


def test_corrupt_persisted_action_does_not_prevent_service_start(tmp_path, caplog):
    from sqlalchemy import create_engine
    from sqlmodel import Session

    from jailbee.db import _ensure_schema
    from jailbee.db.models import ActionEntryRow

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    _ensure_schema(engine)
    with Session(engine) as session:
        session.add(ActionEntryRow(entry_id=123, state="done", payload="not json"))
        session.commit()

    StateServer(FakeGatherer(), version="v1", engine=engine)
    assert "123" in caplog.text


def test_running_action_is_interrupted_after_restart(runtime_dir, tmp_path):
    from sqlalchemy import create_engine

    from jailbee.db import _ensure_schema
    from jailbee.state_service import paths

    release = threading.Event()

    class Child:
        stdout = None

        def __init__(self, sock):
            sock.close()

        def wait(self):
            release.wait(5)
            return 0

        def kill(self):
            release.set()

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    _ensure_schema(engine)
    paths.ensure_runtime_dir()
    server = StateServer(
        FakeGatherer(),
        version="v1",
        engine=engine,
        action_spawn=lambda *a, **kw: Child(kw["prompt_sock"]),
    )
    stopped = threading.Event()
    leaked_tasks = []

    def run_server():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.serve(paths.socket_path()))
            stopped.set()
            release.wait(5)
            loop.run_until_complete(asyncio.gather(*server._action_tasks))
            leaked_tasks.extend(task for task in asyncio.all_tasks(loop) if not task.done())
        finally:
            loop.close()

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(
        None, dashboard_id="dashboard", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    client.start()
    try:
        client.wait_first_snapshot(3)
        client.run_action(
            RunAction(
                345,
                ["ls"],
                str(tmp_path),
                "target",
                False,
                "dashboard",
                "none",
                str(tmp_path),
                "repo",
            )
        )
        _until(lambda: 345 in client.action_entries())
        client.close()
        server._stopping = True
        assert stopped.wait(5), "original server did not stop"
        paths.socket_path().unlink(missing_ok=True)
        replacement = StateServer(FakeGatherer(), version="v1", engine=engine)
        replacement_thread = threading.Thread(
            target=lambda: asyncio.run(replacement.serve(paths.socket_path())), daemon=True
        )
        replacement_thread.start()
        track_server(replacement, replacement_thread)
        _until(paths.socket_path().exists)
        client = StateClient(
            None, dashboard_id="dashboard", version="v1", spawn=lambda: None, backoff=(0.05,)
        )
        client.start()
        client.wait_first_snapshot(3)
        _until(lambda: 345 in client.action_entries())
        entry = client.action_entries()[345]
        assert entry.state == "interrupted"
        assert entry.finished
    finally:
        release.set()
        client.close()
        thread.join(5)
        assert not thread.is_alive()
        assert not leaked_tasks


def test_connected_client_sees_finished_action_expire_without_new_commands(
    runtime_dir, tmp_path, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import create_engine

    from jailbee.db import _ensure_schema
    from jailbee.db.actions import save
    from jailbee.state_service import paths
    from jailbee.state_service import server as server_module
    from jailbee.state_service.protocol import ActionEntry

    now = datetime.now(UTC)
    current = [now]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0]

    monkeypatch.setattr(server_module, "datetime", Clock)
    monkeypatch.setattr(server_module, "ACTION_PRUNE_INTERVAL_SECONDS", 0.1, raising=False)
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    _ensure_schema(engine)
    save(
        engine,
        ActionEntry(
            123,
            ["ls"],
            str(tmp_path),
            "target",
            False,
            "dashboard",
            "none",
            str(tmp_path),
            "repo",
            finished=True,
            state="done",
            finished_at=now,
        ),
    )
    paths.ensure_runtime_dir()
    server = StateServer(FakeGatherer(), version="v1", engine=engine)
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
    )
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(
        None, dashboard_id="dashboard", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    client.start()
    try:
        client.wait_first_snapshot(3)
        _until(lambda: 123 in client.action_entries())
        current[0] = now + timedelta(days=8)
        _until(lambda: 123 not in client.action_entries())
    finally:
        client.close()


def test_naive_persisted_timestamp_does_not_prevent_service_start(runtime_dir, tmp_path):
    from datetime import UTC, datetime

    from sqlalchemy import create_engine

    from jailbee.db import _ensure_schema
    from jailbee.db.actions import save
    from jailbee.state_service import paths
    from jailbee.state_service.protocol import ActionEntry

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    _ensure_schema(engine)
    for entry_id, timestamp in [(123, datetime(2026, 1, 1)), (124, datetime.now(UTC))]:
        save(
            engine,
            ActionEntry(
                entry_id,
                ["ls"],
                str(tmp_path),
                "target",
                False,
                "dashboard",
                "none",
                str(tmp_path),
                "repo",
                finished=True,
                state="done",
                finished_at=timestamp,
            ),
        )
    paths.ensure_runtime_dir()
    server = StateServer(FakeGatherer(), version="v1", engine=engine)
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
    )
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(
        None, dashboard_id="dashboard", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    client.start()
    try:
        client.wait_first_snapshot(3)
        _until(lambda: 124 in client.action_entries())
        assert 123 not in client.action_entries()
    finally:
        client.close()
