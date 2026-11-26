"""Channel lifetime through the action runner's socket/spawn boundary."""

import fcntl
import socket
import threading

import pytest

from jailbee.dashboard.runner import run_action
from jailbee.prompt_channel import CommandEnd, Prompt


@pytest.mark.parametrize("failure", [None, OSError, ValueError])
def test_channel_watch_disconnects_on_high_fd_or_socket_error(mocker, tmp_path, failure):
    disconnected = threading.Event()
    handler_ready = threading.Event()
    pair = socket.socketpair

    def high_pair():
        ui, child = pair()
        fd = fcntl.fcntl(ui.fileno(), fcntl.F_DUPFD, 1100)
        ui.close()
        return socket.socket(fileno=fd), child

    mocker.patch("jailbee.dashboard.runner.socket.socketpair", side_effect=high_pair)
    recv = socket.socket.recv

    def faulty_recv(sock, size, flags=0):
        if flags == socket.MSG_PEEK and failure is not None:
            raise failure("watch failed")
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
