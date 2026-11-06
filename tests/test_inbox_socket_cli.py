"""`jailbee inbox` commands against an in-process fake broker socket."""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from jailbee.orchestration import wire

Handler = Callable[[wire.Request], wire.Response]


class FakeBroker:
    def __init__(self, path: Path) -> None:
        self.requests: list[wire.Request] = []
        self.handlers: dict[str, Handler] = {}
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(path))
        self._srv.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rb") as reader:
            line = wire.read_line(reader)
            if line is None:
                return
            try:
                req = wire.decode_request(line)
            except wire.WireError as exc:
                conn.sendall(wire.encode_response(wire.fail(exc.code, exc.detail)))
                return
            self.requests.append(req)
            resp = self.handlers[req.op](req)
            conn.sendall(wire.encode_response(resp))

    def on(self, op: str, result: object = None, *, error: tuple[str, str] | None = None) -> None:
        self.handlers[op] = (
            (lambda _r: wire.fail(*error)) if error else (lambda _r: wire.ok(result))
        )

    def close(self) -> None:
        self._srv.close()


@pytest.fixture
def sockdir() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def broker(sockdir: Path) -> Iterator[FakeBroker]:
    b = FakeBroker(sockdir / "broker.sock")
    yield b
    b.close()


def run(
    sockdir: Path, *args: str, tmp: Path, input_: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "JAILBEE_ORCHESTRATION_SOCKET": str(sockdir / "broker.sock"),
        "JAILBEE_ORCHESTRATION_DIR": str(tmp / "nofiles"),
        "JAILBEE_LITELLM_CONFIG": str(tmp / "none.json"),
        "JAILBEE_NO_UPDATE_CHECK": "1",
    }
    return subprocess.run(
        [sys.executable, "-m", "jailbee.inbox", *args],
        env=env,
        input=input_,
        capture_output=True,
        text=True,
        timeout=60,
    )


def child(name: str = "kid") -> dict[str, object]:
    return {"name": name, "running": True, "session": "s1", "done": False}


def test_ls_prints_ls_lines(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on(
        "list", [{"id": "m1", "type": "message", "from": "kid", "kind": "note", "body": "hello"}]
    )
    r = run(sockdir, "ls", tmp=tmp_path)
    assert r.returncode == 0
    assert r.stdout.split()[:4] == ["m1", "message", "kid", "note"]
    assert "hello" in r.stdout
    assert not (tmp_path / "nofiles").exists()


def test_read_prints_pretty_and_passes_keep(
    broker: FakeBroker, sockdir: Path, tmp_path: Path
) -> None:
    broker.on("read", {"id": "m1", "type": "message", "body": "hi"})
    r = run(sockdir, "read", "m1", "--keep", tmp=tmp_path)
    assert r.returncode == 0
    assert json.loads(r.stdout)["body"] == "hi"
    assert broker.requests[-1].args == {"id": "m1", "keep": True}


def test_read_not_found(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on("read", error=(wire.NOT_FOUND, "no unread item"))
    r = run(sockdir, "read", "zz", tmp=tmp_path)
    assert r.returncode == 1
    assert "no unread message with id zz" in r.stderr


def test_wait_message(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on("next", {"kind": "message", "id": "m7"})
    r = run(
        sockdir, "wait", "--wake-on", "done,note", "--quiet-after", "2m", "--digest", "1h",
        "--timeout", "10", tmp=tmp_path,
    )  # fmt: skip
    assert r.returncode == 0
    assert r.stdout == "m7\n"
    args = broker.requests[-1].args
    assert args["wake_on"] == ["done", "note"]
    assert args["quiet_after"] == 120
    assert args["digest"] == 3600
    assert 0 < args["timeout"] <= 10


def test_wait_path_is_obsolete_once(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on("next", {"kind": "message", "id": "m7"})
    r = run(sockdir, "wait", "--path", tmp=tmp_path)
    assert r.stdout == "m7\n"
    assert r.stderr.count("--path is obsolete with the broker socket; printed the message id") == 1


def test_wait_quiet_child(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on("next", {"kind": "quiet-child", "child": "kid"})
    r = run(sockdir, "wait", tmp=tmp_path)
    assert (r.returncode, r.stdout) == (0, "quiet-child kid\n")


def test_wait_digest_renders_status(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on(
        "next",
        {
            "kind": "digest",
            "children": [child()],
            "last_from_child": {"kid": time.time() - 30},
            "observed_at": time.time(),
        },
    )
    r = run(sockdir, "wait", "--digest", "1m", tmp=tmp_path)
    lines = r.stdout.splitlines()
    assert r.returncode == 0
    assert lines[0] == "digest"
    assert lines[1].startswith("NAME")
    assert lines[2].startswith("kid")


def test_wait_timeout(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on("next", {"kind": "timeout"})
    r = run(sockdir, "wait", "--timeout", "0", tmp=tmp_path)
    assert (r.returncode, r.stdout) == (1, "")
    assert broker.requests[-1].args["timeout"] == 0


def test_wait_busy(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on("next", error=(wire.BUSY, "another process is already waiting in this container"))
    r = run(sockdir, "wait", tmp=tmp_path)
    assert r.returncode == 2
    assert "another process is already waiting in this container" in r.stderr


def test_wait_bad_request(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on("next", error=(wire.BAD_REQUEST, "wake_on must be a list"))
    r = run(sockdir, "wait", tmp=tmp_path)
    assert r.returncode == 2
    assert "wake_on must be a list" in r.stderr


def send_status(state: str) -> dict[str, object]:
    return {"version": 1, "type": "status", "ref": "x", "state": state}


def test_send_without_wait(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on("send", send_status("delivered"))
    r = run(sockdir, "send", "message", "--text", "hello", tmp=tmp_path)
    assert r.returncode == 0
    ident = r.stdout.strip()
    args = broker.requests[-1].args
    assert args["wait"] is False
    assert args["body"] == "hello"
    assert args["manifest"]["id"] == ident
    assert args["manifest"]["type"] == "message"
    assert args["manifest"]["body_file"] == f"{ident}.md"
    assert not (tmp_path / "nofiles").exists()


def test_send_without_body(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on("send", send_status("queued"))
    r = run(sockdir, "send", "stop", "--child", "kid", tmp=tmp_path)
    assert r.returncode == 0
    assert broker.requests[-1].args["body"] is None
    assert broker.requests[-1].args["manifest"]["child"] == "kid"


@pytest.mark.parametrize(("state", "code"), [("done", 0), ("queued", 0), ("refused", 1)])
def test_send_wait_status(
    broker: FakeBroker, sockdir: Path, tmp_path: Path, state: str, code: int
) -> None:
    broker.on("send", send_status(state))
    r = run(sockdir, "send", "stop", "--child", "kid", "--wait", tmp=tmp_path)
    assert r.returncode == code
    ident, _, rest = r.stdout.partition("\n")
    assert ident.startswith("stop-")
    assert json.loads(rest)["state"] == state
    assert broker.requests[-1].args["wait"] is True


def test_send_validation_unchanged(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    r = run(sockdir, "send", "message", tmp=tmp_path)
    assert r.returncode == 2
    assert broker.requests == []


def test_children(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on(
        "children", {"observed_at": time.time(), "children": [child()], "last_from_child": {}}
    )
    r = run(sockdir, "children", tmp=tmp_path)
    assert (r.returncode, r.stdout) == (0, "kid\ttrue\ts1\tfalse\n")
    assert r.stderr == ""
    r = run(sockdir, "children", "--json", tmp=tmp_path)
    assert json.loads(r.stdout) == [child()]


def test_children_stale_warning(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on(
        "children",
        {"observed_at": time.time() - 1000, "children": [child()], "last_from_child": {}},
    )
    r = run(sockdir, "children", tmp=tmp_path)
    assert r.returncode == 0
    assert "stale" in r.stderr


def test_children_not_orchestrated(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on("children", error=(wire.NOT_ORCHESTRATED, "no"))
    r = run(sockdir, "children", tmp=tmp_path)
    assert r.returncode == 1
    assert "no children view here (not an orchestrator)" in r.stderr


def test_status_table_and_json(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    result = {
        "observed_at": time.time(),
        "children": [child()],
        "last_from_child": {"kid": 12.0},
    }
    broker.on("status", result)
    r = run(sockdir, "status", tmp=tmp_path)
    assert r.returncode == 0
    assert r.stdout.splitlines()[1].startswith("kid")
    r = run(sockdir, "status", "--child", "kid", "--json", tmp=tmp_path)
    assert json.loads(r.stdout)[0]["last_from_child_at"] == 12.0
    assert broker.requests[-1].args == {"child": "kid"}


def test_status_unknown_child(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on("status", error=(wire.NOT_FOUND, "unknown child 'zz'"))
    r = run(sockdir, "status", "--child", "zz", tmp=tmp_path)
    assert r.returncode == 1
    assert "unknown child 'zz'" in r.stderr


GRANT = {
    "operations": ["spawn"],
    "max_running_children": 3,
    "grantable": {"mounts": [], "loose": False, "egress": []},
    "publish": {},
    "default_runner": {"agent": "claude-jb", "profile": "p", "model": "opus"},
    "default_runner_source": "grant",
}


def test_grant(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on("grant", GRANT)
    r = run(sockdir, "grant", tmp=tmp_path)
    assert r.returncode == 0
    assert "max_running_children: 3" in r.stdout
    assert "default runner: claude-jb -P p opus" in r.stdout


def test_grant_not_orchestrated(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on("grant", error=(wire.NOT_ORCHESTRATED, "no"))
    r = run(sockdir, "grant", tmp=tmp_path)
    assert r.returncode == 1
    assert "no orchestration grant here yet" in r.stderr


def test_runners_default_from_grant(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on("grant", GRANT)
    r = run(sockdir, "runners", "--json", tmp=tmp_path)
    assert r.returncode == 0
    assert json.loads(r.stdout)[0]["runner"] == {
        "agent": "claude-jb",
        "profile": "p",
        "model": "opus",
    }


def test_runners_without_grant(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on("grant", error=(wire.NOT_ORCHESTRATED, "no"))
    r = run(sockdir, "runners", "--json", tmp=tmp_path)
    assert r.returncode == 0
    assert json.loads(r.stdout)[0]["runner"] is None


def test_unreachable_exit_4(sockdir: Path, tmp_path: Path) -> None:
    # The directory exists (the device mount point) but nothing listens.
    r = run(sockdir, "ls", tmp=tmp_path)
    assert r.returncode == 4
    assert "the host broker is not reachable" in r.stderr


def test_version_mismatch_exit_4(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on("list", error=(wire.VERSION_ERROR, "v"))
    r = run(sockdir, "ls", tmp=tmp_path)
    assert r.returncode == 4
    assert "container and broker versions differ" in r.stderr


def test_send_wait_no_reply_exit_3(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    def hang(_r: wire.Request) -> wire.Response:
        time.sleep(4)
        return wire.ok(send_status("done"))

    broker.handlers["send"] = hang
    r = run(sockdir, "send", "stop", "--child", "kid", "--wait", "--timeout", "1", tmp=tmp_path)
    assert r.returncode == 3
    assert "no reply from the host broker" in r.stderr
