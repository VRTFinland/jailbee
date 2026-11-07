"""`jailbee inbox` commands against an in-process fake broker socket."""

import contextlib
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
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    conn.sendall(wire.encode_response(wire.fail(exc.code, exc.detail)))
                return
            self.requests.append(req)
            resp = self.handlers[req.op](req)
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
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
        "list",
        {
            "items": [
                {"id": "m1", "type": "message", "from": "kid", "kind": "note", "body": "hello"}
            ],
            "next": None,
        },
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


def test_read_without_keep_reads_with_keep_then_acks(
    broker: FakeBroker, sockdir: Path, tmp_path: Path
) -> None:
    broker.on("read", {"id": "m1", "type": "message", "body": "hi"})
    broker.on("ack", {"acked": True})
    r = run(sockdir, "read", "m1", tmp=tmp_path)
    assert r.returncode == 0
    assert json.loads(r.stdout)["body"] == "hi"
    assert [(q.op, q.args) for q in broker.requests] == [
        ("read", {"id": "m1", "keep": True}),
        ("ack", {"id": "m1"}),
    ]


def test_read_with_keep_does_not_ack(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    broker.on("read", {"id": "m1", "type": "message", "body": "hi"})
    r = run(sockdir, "read", "m1", "--keep", tmp=tmp_path)
    assert r.returncode == 0
    assert [q.op for q in broker.requests] == ["read"]


def test_read_ack_failure_still_prints_and_exits_0(
    broker: FakeBroker, sockdir: Path, tmp_path: Path
) -> None:
    broker.on("read", {"id": "m1", "type": "message", "body": "hi"})
    broker.on("ack", error=(wire.STORE, "locked"))
    r = run(sockdir, "read", "m1", tmp=tmp_path)
    assert r.returncode == 0
    assert json.loads(r.stdout)["body"] == "hi"
    assert "stays unread" in r.stderr


@pytest.mark.parametrize("failure", [TimeoutError("t"), ConnectionResetError("r")])
def test_read_ack_transport_failure_still_exits_0(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
) -> None:
    from jailbee.inbox import socket_cli
    from jailbee.inbox.transport import BrokerUnreachable

    def call(op: str, args: dict[str, object] | None = None) -> object:
        if op == "ack":
            raise BrokerUnreachable() if isinstance(failure, ConnectionResetError) else failure
        return {"id": "m1", "type": "message", "body": "hi"}

    monkeypatch.setattr(socket_cli, "_call", call)
    assert socket_cli.cmd_read("m1") == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["body"] == "hi"
    assert "stays unread" in out.err


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


@pytest.mark.parametrize("branch", [None, "review-feature"])
@pytest.mark.parametrize("wait", [False, True])
def test_spawn_pr_sends_json_integer_and_exact_manifest(
    broker: FakeBroker, sockdir: Path, tmp_path: Path, branch: str | None, wait: bool
) -> None:
    broker.on("send", send_status("started"))
    args = ["send", "spawn", "--pr", "123", "--text", "Review this PR"]
    if branch is not None:
        args += ["--branch", branch]
    if wait:
        args += ["--wait"]
    result = run(sockdir, *args, tmp=tmp_path)
    assert result.returncode == 0, result.stderr
    ident = result.stdout.splitlines()[0]
    assert len(broker.requests) == 1
    request = broker.requests[0]
    assert request.op == "send"
    assert request.args == {
        "manifest": {
            "version": 2,
            "id": ident,
            "type": "spawn",
            "kind": "pr",
            "pr": 123,
            "branch": branch or "pr-placeholder",
            "prompt_file": f"{ident}.md",
            "rights": {"version": 1, "mounts": [], "loose": False, "egress": []},
        },
        "body": "Review this PR",
        "wait": wait,
    }
    assert type(request.args["manifest"]["pr"]) is int
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
    if state == "refused":
        assert rest == ""
        assert "request refused" in r.stderr
    else:
        assert json.loads(rest)["state"] == state
    assert broker.requests[-1].args["wait"] is True


def test_send_validation_unchanged(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    r = run(sockdir, "send", "message", tmp=tmp_path)
    assert r.returncode == 2
    assert broker.requests == []


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["update", "--child", "kid", "--ref", "abc", "--base", "main"], "mutually exclusive"),
        (["restart", "--child", "kid", "--text", "go", "--resume", "BAD"], "--resume"),
        (["rights", "--child", "kid", "--mount", "repo"], "grants need --for"),
        (["rights", "--for", "1h"], "at least one grant or revoke"),
        (
            ["restart", "--child", "kid", "--text", "go", "--agent-only", "--services", "api"],
            "--agent-only",
        ),
    ],
)
def test_send_invalid_options_never_reach_broker(
    broker: FakeBroker, sockdir: Path, tmp_path: Path, args: list[str], error: str
) -> None:
    broker.on("send", send_status("queued"))
    result = run(sockdir, "send", *args, tmp=tmp_path)
    assert result.returncode == 2
    assert error in result.stderr
    assert broker.requests == []


@pytest.mark.parametrize(
    ("args", "expected", "body"),
    [
        (
            ["update", "--child", "kid", "--ref", "abc", "--merge"],
            {"version": 1, "type": "update", "child": "kid", "ref": "abc", "merge": True},
            None,
        ),
        (
            ["update", "--child", "kid", "--base", "main"],
            {"version": 1, "type": "update", "child": "kid", "base": "main"},
            None,
        ),
        (
            [
                "rights",
                "--child",
                "kid",
                "--loose",
                "--mount",
                "repo",
                "--egress",
                "example.com",
                "--for",
                "1h",
            ],
            {
                "version": 1,
                "type": "rights",
                "child": "kid",
                "grant": {
                    "loose": True,
                    "mounts": ["repo"],
                    "egress": ["example.com"],
                    "for": "1h",
                },
            },
            None,
        ),
        (
            [
                "spawn",
                "--branch",
                "feature",
                "--new",
                "--text",
                "go",
                "--services",
                " api, worker ",
            ],
            {
                "version": 2,
                "type": "spawn",
                "kind": "new",
                "branch": "feature",
                "rights": {"version": 1, "mounts": [], "loose": False, "egress": []},
                "services": ["api", "worker"],
            },
            "go",
        ),
        (
            ["spawn", "--branch", "feature", "--text", "go", "--services", ""],
            {
                "version": 2,
                "type": "spawn",
                "kind": "fork",
                "branch": "feature",
                "rights": {"version": 1, "mounts": [], "loose": False, "egress": []},
                "services": [],
            },
            "go",
        ),
        (
            [
                "restart",
                "--child",
                "kid",
                "--text",
                "go",
                "--resume",
                "last",
                "--services",
                "api,worker",
            ],
            {
                "version": 1,
                "type": "restart",
                "child": "kid",
                "resume": "last",
                "services": ["api", "worker"],
            },
            "go",
        ),
        (
            [
                "restart",
                "--child",
                "kid",
                "--text",
                "go",
                "--resume",
                "12345678-1234-1234-1234-123456789abc",
                "--services",
                "",
            ],
            {
                "version": 1,
                "type": "restart",
                "child": "kid",
                "resume": "12345678-1234-1234-1234-123456789abc",
                "services": [],
            },
            "go",
        ),
    ],
)
def test_send_cli_manifest_fields(
    broker: FakeBroker,
    sockdir: Path,
    tmp_path: Path,
    args: list[str],
    expected: dict[str, object],
    body: str | None,
) -> None:
    broker.on("send", send_status("queued"))
    result = run(sockdir, "send", *args, tmp=tmp_path)
    assert result.returncode == 0, result.stderr
    assert len(broker.requests) == 1
    request = broker.requests[0]
    assert request.op == "send"
    ident = result.stdout.strip()
    if body is not None:
        expected = {**expected, "prompt_file": f"{ident}.md"}
    assert request.args == {"manifest": {**expected, "id": ident}, "body": body, "wait": False}


@pytest.mark.parametrize("option", ["--quiet-after", "--digest"])
@pytest.mark.parametrize("value", ["0", "0s", "garbage", "-1", "1d"])
def test_wait_invalid_durations_never_reach_broker(
    broker: FakeBroker, sockdir: Path, tmp_path: Path, option: str, value: str
) -> None:
    broker.on("next", {"kind": "timeout"})
    result = run(sockdir, "wait", f"{option}={value}", tmp=tmp_path)
    assert result.returncode == 2
    assert broker.requests == []


@pytest.mark.parametrize(
    ("option", "key"), [("--quiet-after", "quiet_after"), ("--digest", "digest")]
)
@pytest.mark.parametrize(("value", "seconds"), [("2s", 2), ("3m", 180), ("1h", 3600)])
def test_wait_duration_conversion(
    broker: FakeBroker,
    sockdir: Path,
    tmp_path: Path,
    option: str,
    key: str,
    value: str,
    seconds: int,
) -> None:
    broker.on("next", {"kind": "timeout"})
    result = run(sockdir, "wait", option, value, "--timeout", "0", tmp=tmp_path)
    assert result.returncode == 1
    assert len(broker.requests) == 1
    assert broker.requests[0].op == "next"
    assert broker.requests[0].args[key] == seconds


def test_stale_socket_reports_unreachable(sockdir: Path, tmp_path: Path) -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(sockdir / "broker.sock"))
    sock.close()
    result = run(sockdir, "ls", tmp=tmp_path)
    assert result.returncode == 4
    assert "the host broker is not reachable" in result.stderr


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


@pytest.mark.parametrize(
    "args",
    [
        ("send", "stop"),
        ("send", "rights", "--loose", "--for", "nonsense"),
        ("send", "rights", "--loose", "--for", "0s"),
        ("send", "rights", "--loose", "--for", "25h"),
        ("send", "spawn", "--branch", "kid", "--text", ""),
        ("send", "message", "--text", "hello", "--timeout", "bad"),
        ("send", "message", "--text", "hello", "--wait", "--timeout", "bad"),
        ("wait", "--timeout", "bad"),
        ("wait", "--wake-on", "nonsense"),
        ("wait", "--quiet-after", "bad"),
        ("wait", "--digest", "bad"),
    ],
)
def test_invalid_arguments_without_socket(
    sockdir: Path, tmp_path: Path, args: tuple[str, ...]
) -> None:
    result = run(sockdir, *args, tmp=tmp_path)
    assert result.returncode == 2, result.stderr


def test_runners_without_socket_lists_local_profiles(
    sockdir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from jailbee.inbox import cli, socket_cli

    config = tmp_path / "litellm.json"
    config.write_text(json.dumps({"profiles": {"local": {"tiers": {"sonnet": "model"}}}}))
    monkeypatch.setenv("JAILBEE_LITELLM_CONFIG", str(config))
    monkeypatch.setenv("JAILBEE_ORCHESTRATION_SOCKET", str(sockdir / "broker.sock"))
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/bin/claude")
    assert socket_cli.cmd_runners(as_json=True) == 0
    entries = json.loads(capsys.readouterr().out)
    assert {"agent": "claude-jb", "profile": "local"} in [e["runner"] for e in entries]


@pytest.mark.parametrize(
    "args",
    [
        ("send", "stop", "--child", "kid"),
        ("wait", "--timeout", "0"),
        ("send", "rights", "--loose", "--for", "24h"),
        ("send", "rights", "--loose", "--for", "1440"),
        ("send", "rights", "--loose", "--for", "never"),
    ],
)
def test_valid_broker_commands_without_socket(
    sockdir: Path, tmp_path: Path, args: tuple[str, ...]
) -> None:
    assert run(sockdir, *args, tmp=tmp_path).returncode == 4


def test_unreachable_exit_4(sockdir: Path, tmp_path: Path) -> None:
    # The directory exists (the device mount point) but nothing listens.
    r = run(sockdir, "ls", tmp=tmp_path)
    assert r.returncode == 4
    assert "the host broker socket is missing" in r.stderr


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
    assert r.stdout.startswith("stop-")  # the id was printed before the call


def test_send_prints_the_id_before_the_broker_answers(
    broker: FakeBroker, sockdir: Path, tmp_path: Path
) -> None:
    broker.on("send", error=(wire.REFUSED, "disk full"))
    r = run(sockdir, "send", "stop", "--child", "kid", tmp=tmp_path)
    assert r.returncode == 1
    assert r.stdout.startswith("stop-")
    assert "disk full" in r.stderr
    assert len(broker.requests) == 1  # an error response is final


@pytest.mark.parametrize("wait", [False, True])
def test_send_duplicate_is_exit_0_with_a_note(
    broker: FakeBroker, sockdir: Path, tmp_path: Path, wait: bool
) -> None:
    detail = "request x was already handled; its status is in the inbox or was already reported"
    broker.on("send", error=(wire.DUPLICATE, detail))
    extra = ["--wait"] if wait else []
    r = run(sockdir, "send", "stop", "--child", "kid", *extra, tmp=tmp_path)
    assert r.returncode == 0
    assert r.stdout.strip().startswith("stop-") and len(r.stdout.split()) == 1
    assert "already handled" in r.stderr


@pytest.mark.parametrize("state,code", [("done", 0), ("failed", 1)])
def test_publish_until_done_json_cli(broker, sockdir, tmp_path, state, code):
    final = {
        "id": "h-final",
        "type": "status",
        "state": state,
        "results": [{"index": 0, "kind": "create", "issue": 42, "url": "https://example.test/42"}],
    }
    request_ids = []

    def send_request(request):
        request_ids.append(request.args["manifest"]["id"])
        return wire.ok(send_status("started"))

    def next_status(request):
        assert request.args["ref"] == request_ids[0]
        assert 0 < request.args["timeout"] <= 10
        return wire.ok({"kind": "status", "item": final})

    broker.handlers["send"] = send_request
    broker.handlers["next"] = next_status
    broker.on("ack", {"acked": True})
    result = run(
        sockdir,
        "send",
        "publish",
        "--issue",
        "a.json",
        "--wait",
        "--until",
        "done",
        "--timeout",
        "10s",
        "--json",
        tmp=tmp_path,
    )
    assert result.returncode == code, result.stderr
    assert json.loads(result.stdout) == final
    assert [r.op for r in broker.requests] == ["send", "next", "ack"]


def test_publish_until_without_wait_is_usage_error(broker, sockdir, tmp_path):
    result = run(sockdir, "send", "publish", "--issue", "a.json", "--until", "done", tmp=tmp_path)
    assert result.returncode == 2
    assert "--until needs publish --wait" in result.stderr
    assert broker.requests == []


def test_call_timeout_is_two_minutes() -> None:
    from jailbee.inbox import socket_cli

    assert socket_cli.CALL_TIMEOUT == 120.0


def test_send_without_wait_uses_the_request_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from jailbee.inbox import socket_cli
    from jailbee.inbox.send import SendRequest

    seen: list[float | None] = []

    def fake(_factory: object, _args: object, *, timeout: float | None) -> dict[str, object]:
        seen.append(timeout)
        return send_status("done")

    monkeypatch.setattr(socket_cli, "call_send", fake)
    for request, expected in (
        (SendRequest(type="stop", child="kid"), 1800),
        (SendRequest(type="stop", child="kid", wait=True, timeout="5m"), 300),
    ):
        assert socket_cli.cmd_send(request) == 0
        assert seen[-1] == expected


def test_send_timeout_message_and_exit_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from jailbee.inbox import socket_cli
    from jailbee.inbox.send import SendRequest

    def fake(*_a: object, **_k: object) -> object:
        raise TimeoutError

    monkeypatch.setattr(socket_cli, "call_send", fake)
    assert socket_cli.cmd_send(SendRequest(type="stop", child="kid")) == 3
    err = capsys.readouterr().err
    assert (
        "jailbee inbox: no reply from the host broker yet; the request (id printed above) "
        "may or may not have run. Inspect `jailbee inbox ls` and `jailbee inbox status` "
        "before resending with a new id"
    ) in err


def test_timeout_from_a_socket_command_is_exit_4(
    broker: FakeBroker, sockdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import typer

    from jailbee.inbox import container_cmd, socket_cli

    def boom() -> int:
        raise TimeoutError("timed out")

    monkeypatch.setenv("JAILBEE_ORCHESTRATION_SOCKET", str(sockdir / "broker.sock"))
    monkeypatch.setenv("JAILBEE_ORCHESTRATION_DIR", str(tmp_path / "nofiles"))
    monkeypatch.setattr(socket_cli, "cmd_ls", boom)
    with pytest.raises(typer.Exit) as exc:
        container_cmd.ls()
    assert exc.value.exit_code == 4


def test_store_error_is_exit_4_with_detail(
    broker: FakeBroker, sockdir: Path, tmp_path: Path
) -> None:
    broker.on("list", error=(wire.STORE, "inbox migration pending; retry shortly"))
    r = run(sockdir, "ls", tmp=tmp_path)
    assert r.returncode == 4
    assert "the host broker is not reachable" in r.stderr
    assert "inbox migration pending; retry shortly" in r.stderr
    broker.on("read", error=(wire.STORE, "inbox migration pending; retry shortly"))
    r = run(sockdir, "read", "m1", tmp=tmp_path)
    assert r.returncode == 4
    assert "inbox migration pending" in r.stderr
    broker.on("children", error=(wire.STORE, "inbox migration pending; retry shortly"))
    r = run(sockdir, "children", tmp=tmp_path)
    assert r.returncode == 4


# -- paged inbox listing -----------------------------------------------------


def _paging_list(items: list[dict[str, object]], size: int) -> Handler:
    """A broker that pages `list` by seq (cursor = index)."""

    def handle(req: wire.Request) -> wire.Response:
        start = int(req.args["after"] or 0)
        end = start + size
        return wire.ok({"items": items[start:end], "next": end if end < len(items) else None})

    return handle


def test_ls_pages_through_a_long_inbox(broker: FakeBroker, sockdir: Path, tmp_path: Path) -> None:
    items: list[dict[str, object]] = [
        {"id": f"m{n}", "type": "message", "from": "kid", "kind": "note", "body": "x"}
        for n in range(7)
    ]
    broker.handlers["list"] = _paging_list(items, 3)
    r = run(sockdir, "ls", tmp=tmp_path)
    assert r.returncode == 0
    assert [line.split()[0] for line in r.stdout.splitlines()] == [f"m{n}" for n in range(7)]


@pytest.mark.parametrize("wait", [False, True])
def test_send_refusal_always_prints_detail_not_status(
    broker: FakeBroker, sockdir: Path, tmp_path: Path, wait: bool
) -> None:
    broker.on("send", {**send_status("refused"), "detail": "operation not granted"})
    r = run(sockdir, "send", "stop", "--child", "kid", *(["--wait"] if wait else []), tmp=tmp_path)
    assert r.returncode == 1
    assert len(r.stdout.splitlines()) == 1 and r.stdout.startswith("stop-")
    assert "operation not granted" in r.stderr
