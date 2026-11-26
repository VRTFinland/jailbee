"""Channel lifetime through the action runner's socket/spawn boundary."""

import fcntl
import resource
import socket
import threading

import pytest

from jailbee.dashboard.runner import run_action
from jailbee.prompt_channel import CommandEnd, Prompt


def test_channel_watch_disconnects_on_high_fd(mocker, tmp_path):
    if resource.getrlimit(resource.RLIMIT_NOFILE)[0] <= 1100:
        pytest.skip("high-fd regression needs RLIMIT_NOFILE > 1100")
    disconnected = threading.Event()
    handler_ready = threading.Event()
    pair = socket.socketpair

    def high_pair():
        ui, child = pair()
        fd = fcntl.fcntl(ui.fileno(), fcntl.F_DUPFD, 1100)
        ui.close()
        return socket.socket(fileno=fd), child

    mocker.patch("jailbee.dashboard.runner.socket.socketpair", side_effect=high_pair)

    class Child:
        stdout = None

        def __init__(self, sock):
            self.thread = threading.Thread(target=self.run, args=(sock,))
            self.thread.start()

        def run(self, sock):
            end = CommandEnd(sock)
            try:
                end._ch.handshake()
                end._ch.send(Prompt(1, "text", "Waiting?").to_json())
                assert handler_ready.wait(5)
            finally:
                end.close()

        def wait(self):
            self.thread.join(5)
            assert not self.thread.is_alive()
            return 1

        def kill(self):
            handler_ready.set()

    def handler(prompt):
        handler_ready.set()
        disconnected.wait(1)
        return None

    result = run_action(
        ["start", "alpha"],
        cwd=tmp_path,
        transport="none",
        policy=None,
        on_prompt=handler,
        on_output=lambda text: None,
        on_disconnect=disconnected.set,
        spawn=lambda argv, **kwargs: Child(kwargs["prompt_sock"]),
    )
    assert disconnected.is_set()
    assert result.cancelled
    assert result.status == 1


@pytest.mark.parametrize("stage", ["register", "select", "peek"])
@pytest.mark.parametrize("error", [OSError, ValueError])
def test_watch_error_disconnects_a_still_open_channel(mocker, tmp_path, stage, error):
    disconnected = threading.Event()
    release_child = threading.Event()
    child_open = threading.Event()
    from jailbee.dashboard import runner

    selector_type = runner.selectors.DefaultSelector

    class FaultySelector(selector_type):
        def register(self, *args, **kwargs):
            if stage == "register":
                raise error("register failed")
            return super().register(*args, **kwargs)

        def select(self, timeout=None):
            if stage == "select":
                raise error("select failed")
            # The injected peek failure must happen with a live, idle peer.
            return [(None, runner.selectors.EVENT_READ)]

    mocker.patch.object(runner.selectors, "DefaultSelector", FaultySelector)
    recv = socket.socket.recv

    def faulty_recv(sock, size, flags=0):
        if flags == socket.MSG_PEEK:
            raise error("peek failed")
        return recv(sock, size, flags)

    mocker.patch.object(socket.socket, "recv", faulty_recv)

    class Child:
        stdout = None

        def __init__(self, sock):
            self.thread = threading.Thread(target=self.run, args=(sock,))
            self.thread.start()

        def run(self, sock):
            end = CommandEnd(sock)
            try:
                end._ch.handshake()
                child_open.set()
                end._ch.send(Prompt(1, "text", "Waiting?").to_json())
                release_child.wait(5)
            finally:
                child_open.clear()
                end.close()

        def wait(self):
            self.thread.join(5)
            assert not self.thread.is_alive()
            return 1

        def kill(self):
            release_child.set()

    def handler(prompt):
        try:
            assert disconnected.wait(1), "watch failure left a live command awaiting forever"
            assert child_open.is_set()
        finally:
            release_child.set()
        return None

    result = run_action(
        ["start", "alpha"],
        cwd=tmp_path,
        transport="none",
        policy=None,
        on_prompt=handler,
        on_output=lambda text: None,
        on_disconnect=disconnected.set,
        spawn=lambda argv, **kwargs: Child(kwargs["prompt_sock"]),
    )
    assert result.cancelled
    assert result.status == 1


def test_answer_does_not_wait_forever_for_disconnect_callback(tmp_path):
    callback_entered = threading.Event()
    release_callback = threading.Event()
    completed = threading.Event()
    results = []

    class Child:
        stdout = None

        def __init__(self, sock):
            self.thread = threading.Thread(target=self.run, args=(sock,))
            self.thread.start()

        def run(self, sock):
            end = CommandEnd(sock)
            end._ch.handshake()
            end._ch.send(Prompt(1, "text", "Waiting?").to_json())
            end.close()

        def wait(self):
            self.thread.join(5)
            return 1

        def kill(self):
            pass

    def disconnect():
        callback_entered.set()
        release_callback.wait(5)

    def handler(prompt):
        assert callback_entered.wait(5)
        return None

    def run():
        try:
            results.append(
                run_action(
                    ["start", "alpha"],
                    cwd=tmp_path,
                    transport="none",
                    policy=None,
                    on_prompt=handler,
                    on_output=lambda text: None,
                    on_disconnect=disconnect,
                    spawn=lambda argv, **kwargs: Child(kwargs["prompt_sock"]),
                )
            )
        finally:
            completed.set()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert completed.wait(1), "answer waited on a dead UI's disconnect callback"
        assert results[0].cancelled
    finally:
        release_callback.set()
        worker.join(5)
