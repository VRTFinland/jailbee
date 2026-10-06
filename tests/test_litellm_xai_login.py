"""The waiter must not bypass state checks or leave terminal/socket resources open."""

import fcntl
import importlib
import os
import pty
import socket
import struct
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


def helper():
    return importlib.import_module("jailbee.provision.litellm.xai_login")


class Server(HTTPServer):
    expected_state = "session-state"


def server():
    return Server(("127.0.0.1", 0), BaseHTTPRequestHandler)


def test_manual_code_is_bound_to_this_session_and_server_closed():
    s = server()
    read, write = os.pipe()
    os.write(write, b"bare-code\n")
    try:
        result = helper().wait_for_callback(s, read, timeout=0.1)
        assert result == {"code": "bare-code", "state": "session-state"}
        assert s.fileno() == -1
    finally:
        os.close(read)
        os.close(write)
        s.server_close()


@pytest.mark.parametrize("query", ["code=c&state=wrong", "code=c&state=session-state&code=d"])
def test_invalid_callback_url_never_becomes_a_code(query):
    s = server()
    read, write = os.pipe()
    os.write(write, f"http://127.0.0.1:{s.server_port}/callback?{query}\n".encode())
    try:
        with pytest.raises(helper().LoginError):
            helper().wait_for_callback(s, read, timeout=0.1)
        assert s.fileno() == -1
    finally:
        os.close(read)
        os.close(write)
        s.server_close()


def test_callback_beats_eof_and_preserves_state():
    s = server()
    client = socket.create_connection(s.server_address)
    client.sendall(b"GET /callback?code=c&state=session-state HTTP/1.1\r\nHost: localhost\r\n\r\n")
    read, write = os.pipe()
    os.close(write)
    try:
        assert helper().wait_for_callback(s, read, timeout=0.2) == {
            "code": "c",
            "state": "session-state",
        }
    finally:
        client.close()
        os.close(read)
        s.server_close()


def test_partial_http_cannot_extend_timeout_and_terminal_is_restored():
    s = server()
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    client = socket.create_connection(s.server_address)
    client.sendall(b"GET /callback")
    try:
        with pytest.raises(helper().LoginError, match="Timed out"):
            helper().wait_for_callback(s, slave, timeout=0.02)
        assert termios.tcgetattr(slave) == original
        assert s.fileno() == -1
    finally:
        client.close()
        os.close(master)
        os.close(slave)
        s.server_close()


def test_eof_without_callback_cancels():
    s = server()
    read, write = os.pipe()
    os.close(write)
    try:
        with pytest.raises(helper().LoginError, match="cancelled"):
            helper().wait_for_callback(s, read, timeout=0.1)
        assert s.fileno() == -1
    finally:
        os.close(read)
        s.server_close()


def test_interrupt_restores_terminal_and_closes_server(monkeypatch):
    s = server()
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)

    def interrupt(*args):
        assert not termios.tcgetattr(slave)[3] & termios.ECHO
        raise KeyboardInterrupt

    monkeypatch.setattr(helper().selectors.DefaultSelector, "select", interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            helper().wait_for_callback(s, slave)
        assert termios.tcgetattr(slave) == original
        assert s.fileno() == -1
    finally:
        os.close(master)
        os.close(slave)
        s.server_close()


def test_main_sanitizes_upstream_errors(capsys):
    class Auth:
        def login(self, **kwargs):
            raise ValueError("SECRET-CODE SECRET-TOKEN")

    assert helper().main(Auth) == 1
    captured = capsys.readouterr()
    assert "SECRET" not in captured.out + captured.err
    assert "failed" in captured.err


def test_waiter_supports_high_numbered_descriptors():
    s = server()
    read, write = os.pipe()
    high = fcntl.fcntl(read, fcntl.F_DUPFD, 1100)
    os.write(write, b"high-code\n")
    try:
        assert helper().wait_for_callback(s, high, timeout=0.1)["code"] == "high-code"
    finally:
        os.close(high)
        os.close(read)
        os.close(write)
        s.server_close()


@pytest.mark.parametrize("cancel", [False, True])
def test_main_hides_input_before_vendor_login_and_restores_it(monkeypatch, capsys, cancel):
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)

    class Input:
        def fileno(self):
            return slave

    class Auth:
        def login(self, **kwargs):
            assert kwargs == {"no_browser": True}
            assert not termios.tcgetattr(slave)[3] & termios.ECHO
            if cancel:
                raise KeyboardInterrupt

    monkeypatch.setattr(helper().sys, "stdin", Input())
    try:
        assert helper().main(Auth) == (130 if cancel else 0)
        assert termios.tcgetattr(slave) == original
        assert ("cancelled" if cancel else "Logged in") in str(capsys.readouterr())
    finally:
        os.close(master)
        os.close(slave)


def test_valid_pasted_callback_url():
    s = server()
    read, write = os.pipe()
    os.write(
        write,
        f"http://127.0.0.1:{s.server_port}/callback?code=url-code&state=session-state\n".encode(),
    )
    try:
        assert helper().wait_for_callback(s, read, timeout=0.1)["code"] == "url-code"
    finally:
        os.close(read)
        os.close(write)
        s.server_close()


def test_oversize_paste_rejected():
    s = server()
    read, write = os.pipe()

    def feed():
        try:
            os.write(write, b"a" * 9000 + b"\n")
        except BrokenPipeError:
            pass

    worker = threading.Thread(target=feed)
    worker.start()
    try:
        with pytest.raises(helper().LoginError, match="too long"):
            helper().wait_for_callback(s, read, timeout=1)
    finally:
        os.close(read)
        worker.join(1)
        os.close(write)
        s.server_close()


def test_fragmented_callback_survives_a_reset_connection():
    s = server()
    read, write = os.pipe()

    def feed():
        bad = socket.create_connection(s.server_address)
        bad.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        bad.sendall(b"GET /callback")
        bad.close()
        time.sleep(0.02)
        with socket.create_connection(s.server_address) as good:
            good.sendall(b"GET /callback?code=fragmented&state=session-state HTTP/1.1\r\n")
            time.sleep(0.02)
            good.sendall(b"Host: localhost\r\n\r\n")

    worker = threading.Thread(target=feed)
    worker.start()
    try:
        assert helper().wait_for_callback(s, read, timeout=1)["code"] == "fragmented"
    finally:
        worker.join(1)
        os.close(read)
        os.close(write)
        s.server_close()


def test_denied_callback_fails_immediately_without_error_description():
    s = server()
    read, write = os.pipe()
    client = socket.create_connection(s.server_address)
    client.sendall(
        b"GET /callback?error=access_denied&state=session-state&error_description=SECRET "
        b"HTTP/1.1\r\nHost: localhost\r\n\r\n"
    )
    try:
        with pytest.raises(helper().LoginError, match="authorization failed") as error:
            helper().wait_for_callback(s, read, timeout=0.1)
        assert "SECRET" not in str(error.value)
    finally:
        client.close()
        os.close(read)
        os.close(write)
        s.server_close()
