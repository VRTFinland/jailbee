"""The dashboard action runner: refusal before spawn, prompt relay, audit."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from jailbee.config.models_remote import RemoteCommandPolicy, RemotePolicy
from jailbee.dashboard import runner
from jailbee.prompt_channel import CommandEnd, Prompt


def _policy(*allow: str) -> RemotePolicy:
    if not allow:
        return RemotePolicy()
    return RemotePolicy(commands=RemoteCommandPolicy(mode="allowlist", allow=list(allow)))


class FakeChild:
    """A command on a thread: asks `questions` through the channel, prints, exits."""

    def __init__(self, sock: socket.socket, questions: list[Prompt | None], exit_code: int) -> None:
        read_fd, write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb")
        self.answers: list[Any] = []
        self.killed = False
        self._code = exit_code
        self._thread = threading.Thread(target=self._run, args=(sock, write_fd, questions))
        self._thread.start()

    def _run(self, sock: socket.socket, write_fd: int, questions: list[Prompt | None]) -> None:
        end = CommandEnd(sock)
        out = os.fdopen(write_fd, "wb")
        try:
            out.write("alku ä\n".encode())
            out.flush()
            for q in questions:
                assert q is not None
                self.answers.append(end.ask(lambda i, q=q: Prompt(**{**q.__dict__, "id": i})))
            out.write(f"vastaukset={len(self.answers)}\n".encode())
        finally:
            out.close()
            end.close()

    def wait(self) -> int:
        self._thread.join(5)
        return self._code

    def kill(self) -> None:
        self.killed = True


class Spawns:
    def __init__(self, questions: list[Prompt | None] | None = None, exit_code: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.child: FakeChild | None = None
        self._questions = questions or []
        self._exit = exit_code

    def __call__(self, argv: Any, *, cwd: Path, env: Any, prompt_sock: socket.socket) -> FakeChild:
        self.calls.append({"argv": list(argv), "env": dict(env), "cwd": cwd})
        self.child = FakeChild(prompt_sock, self._questions, self._exit)
        return self.child


def _confirm(text: str = "Go?") -> Prompt:
    return Prompt(id=0, kind="confirm", question=text, default=False)


def _run(
    argv: list[str],
    spawn: Spawns,
    tmp_path: Path,
    *,
    transport: Any = "web",
    policy: RemotePolicy | None = None,
    on_prompt: Any = lambda p: True,
    audit: list[dict[str, Any]] | None = None,
    client: str | None = "dev-1",
) -> tuple[runner.RunResult, list[str]]:
    out: list[str] = []
    records = audit if audit is not None else []
    result = runner.run_action(
        argv,
        transport=transport,
        policy=policy,
        cwd=tmp_path,
        on_prompt=on_prompt,
        on_output=out.append,
        client=client,
        spawn=spawn,
        audit=records.append,
    )
    return result, out


def test_refusal_starts_no_child_and_is_audited(tmp_path: Path) -> None:
    spawn, audit = Spawns(), []
    result, out = _run(["destroy", "x"], spawn, tmp_path, policy=_policy("ls"), audit=audit)
    assert result.refused and result.status is None
    assert spawn.calls == [] and out == []
    assert [(r["transport"], r["client"], r["command"], r["outcome"]) for r in audit] == [
        ("web", "dev-1", "destroy", "refused")
    ]


def test_missing_policy_refuses(tmp_path: Path) -> None:
    spawn = Spawns()
    result, _ = _run(["ls"], spawn, tmp_path, policy=None)
    assert result.refused and spawn.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["shell", "a"],
        ["tmux", "a"],
        ["apps", "run", "gimp", "a"],
        ["ide", "a"],
        ["orphan", "tmux", "a"],
        ["orphan", "shell", "a"],
    ],
)
def test_attach_style_is_never_spawned(argv: list[str], tmp_path: Path) -> None:
    spawn = Spawns()
    assert runner.is_attach_style(argv)
    result, _ = _run(argv, spawn, tmp_path, transport="none")
    assert result.refused and spawn.calls == []


def test_ordinary_actions_are_not_attach_style() -> None:
    assert not runner.is_attach_style(["ls", "--all"])
    assert not runner.is_attach_style(["git", "pull", "x"])


def test_prompt_relay_output_and_exit_status(tmp_path: Path) -> None:
    spawn, audit = Spawns([_confirm("Sure?"), _confirm("Really?")], exit_code=3), []
    seen: list[str] = []

    def handler(p: Prompt) -> Any:
        seen.append(p.question)
        return p.question == "Sure?"

    result, out = _run(
        ["ls"], spawn, tmp_path, policy=_policy("ls"), on_prompt=handler, audit=audit
    )
    assert result.status == 3 and not result.ok
    assert seen == ["Sure?", "Really?"]
    assert [a.__dict__["value"] for a in spawn.child.answers] == [True, False]  # type: ignore[union-attr]  # test: child exists after spawn
    assert "".join(out) == "alku ä\nvastaukset=2\n"
    assert audit[0]["outcome"] == "exit:3"


def test_handler_cancel_reaches_the_child_as_cancel(tmp_path: Path) -> None:
    spawn = Spawns([_confirm()])
    result, _ = _run(["ls"], spawn, tmp_path, policy=_policy("ls"), on_prompt=lambda p: None)
    assert result.status == 0
    assert type(spawn.child.answers[0]).__name__ == "Cancel"  # type: ignore[union-attr]  # test: child exists after spawn


def test_handler_exception_kills_child_and_is_audited(tmp_path: Path) -> None:
    spawn, audit = Spawns([_confirm()]), []

    def boom(p: Prompt) -> Any:
        raise RuntimeError("ui died")

    with pytest.raises(RuntimeError):
        _run(["ls"], spawn, tmp_path, policy=_policy("ls"), on_prompt=boom, audit=audit)
    assert spawn.child.killed  # type: ignore[union-attr]  # test: child exists after spawn
    assert audit[0]["outcome"] == "error"


def test_spawn_failure_is_reported(tmp_path: Path) -> None:
    def failing(argv: Any, **kw: Any) -> Any:
        kw["prompt_sock"].close()
        raise FileNotFoundError("nope")

    out: list[str] = []
    result = runner.run_action(
        ["ls"],
        transport="none",
        policy=None,
        cwd=tmp_path,
        on_prompt=lambda p: True,
        on_output=out.append,
        spawn=failing,
    )
    assert result.error == "nope" and result.status is None


def test_local_transport_is_not_audited_and_gets_address_flags(tmp_path: Path) -> None:
    spawn, audit = Spawns(), []
    _run(["ls"], spawn, tmp_path, transport="none", audit=audit)
    assert audit == []
    out: list[str] = []
    runner.run_action(
        ["ls"],
        transport="none",
        policy=None,
        cwd=tmp_path,
        on_prompt=lambda p: True,
        on_output=out.append,
        address_flags=["--config", "/r/c.yaml"],
        spawn=spawn,
    )
    assert spawn.calls[-1]["argv"][-3:] == ["ls", "--config", "/r/c.yaml"]


def test_remote_child_carries_transport_markers(tmp_path: Path) -> None:
    spawn = Spawns()
    _run(["ls"], spawn, tmp_path, policy=_policy("ls"))
    env = spawn.calls[0]["env"]
    assert env["JAILBEE_REMOTE_TRANSPORT"] == "web"
    assert env["JAILBEE_REMOTE_SESSION"] == "1"
    assert env["JAILBEE_REMOTE_RESTRICTED"] == "1"


def test_default_audit_is_jsonl_without_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    out: list[str] = []
    runner.run_action(
        ["destroy", "secret-container", "--force"],
        transport="web",
        policy=_policy("ls"),
        cwd=tmp_path,
        on_prompt=lambda p: True,
        on_output=out.append,
        client="phone",
        spawn=Spawns(),
    )
    path = tmp_path / "state" / "jailbee" / "audit" / "remote-actions.jsonl"
    text = path.read_text()
    (rec,) = [json.loads(line) for line in text.splitlines()]
    assert rec["client"] == "phone" and rec["outcome"] == "refused"
    assert "secret-container" not in text and "--force" not in text
    assert path.stat().st_mode & 0o777 == 0o600


def test_real_child_asks_through_the_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real subprocess: JAILBEE_PROMPT_FD reaches it and ask_confirm is relayed."""
    code = (
        "from jailbee.prompting import ask_confirm\n"
        "print('answer', ask_confirm('Proceed?', default=False))\n"
        "raise SystemExit(7)\n"
    )
    real = runner.popen_spawn
    monkeypatch.setattr(runner, "cli_argv", lambda argv: [sys.executable, "-c", code])
    questions: list[str] = []

    def handler(p: Prompt) -> Any:
        questions.append(p.question)
        return True

    out: list[str] = []
    result = runner.run_action(
        ["ls"],
        transport="none",
        policy=None,
        cwd=tmp_path,
        on_prompt=handler,
        on_output=out.append,
        spawn=real,
    )
    assert questions == ["Proceed?"]
    assert result.status == 7
    assert "answer True" in "".join(out)
