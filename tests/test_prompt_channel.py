"""The prompt channel, driven through the public prompting functions.

A test peer (`UiEnd` on a thread) sits on the other end of a socketpair; the
command side finds its end through `JAILBEE_PROMPT_FD` exactly as in
production.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import typer

from jailbee import prompt_channel as pc
from jailbee import prompting
from jailbee.prompting import (
    Cancelled,
    MissingValue,
    Option,
    PromptContext,
    ask_confirm,
    ask_secret,
    ask_text,
    choose_many,
    choose_one,
)

JOIN = 5.0


class Peer:
    """A UI on a thread: records prompts, answers with `handler`."""

    def __init__(self, sock: socket.socket, handler: Callable[[pc.Prompt], Any]) -> None:
        self.prompts: list[pc.Prompt] = []
        self._ui = pc.UiEnd(sock)

        def run() -> None:
            self._ui.serve(self._record(handler))

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def _record(self, handler: Callable[[pc.Prompt], Any]) -> Callable[[pc.Prompt], Any]:
        def inner(p: pc.Prompt) -> Any:
            self.prompts.append(p)
            return handler(p)

        return inner

    def join(self) -> None:
        self._thread.join(JOIN)
        assert not self._thread.is_alive(), "peer hung"


@pytest.fixture
def channel(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[Callable[[pc.Prompt], Any]], Peer]]:
    """Open a socketpair, export one end's fd, return a peer factory."""
    cmd_sock, ui_sock = socket.socketpair()
    monkeypatch.setenv("JAILBEE_PROMPT_FD", str(cmd_sock.fileno()))
    peers: list[Peer] = []

    def make(handler: Callable[[pc.Prompt], Any]) -> Peer:
        peer = Peer(ui_sock, handler)
        peers.append(peer)
        return peer

    yield make
    # Close the command side first: the peer's serve loop ends on EOF.
    prompting.drop_channel()
    cmd_sock.close()
    for peer in peers:
        peer.join()
    ui_sock.close()


def test_confirm_answer_and_wire_fields(channel: Any) -> None:
    peer = channel(lambda p: True)
    ctx = PromptContext(repo="r", container="c")
    assert ask_confirm("Go?", default=False, context=ctx) is True
    (p,) = peer.prompts
    assert (p.kind, p.question, p.default, p.destructive) == ("confirm", "Go?", False, False)
    assert p.context == pc.WireContext("r", "c")


def test_confirm_false_is_an_answer_not_cancel(channel: Any) -> None:
    channel(lambda p: False)
    assert ask_confirm("Go?") is False


def test_destructive_flag_travels_and_default_is_no(channel: Any) -> None:
    peer = channel(lambda p: True)
    ask_confirm("Delete?", default=True, destructive=True)
    assert peer.prompts[0].destructive is True
    assert peer.prompts[0].default is False


def test_choose_one_maps_id_back_to_arbitrary_object(channel: Any) -> None:
    a, b = object(), {"x": 1}
    peer = channel(lambda p: p.options[1].id)
    got = choose_one(
        "thing",
        [Option(a, "A", "alpha", "first"), Option(b, "B", "beta")],
    )
    assert got is b
    p = peer.prompts[0]
    assert p.kind == "choose_one"
    assert [(o.title, o.label, o.description) for o in p.options] == [
        ("A", "alpha", "first"),
        ("B", "beta", ""),
    ]


def test_choose_one_sends_the_default_as_the_option_id(channel: Any) -> None:
    peer = channel(lambda p: p.options[0].id)
    choose_one("thing", [Option("a", "A", "a"), Option("b", "B", "b")], default="b")
    assert peer.prompts[0].default == peer.prompts[0].options[1].id


def test_choose_one_without_a_default_sends_none(channel: Any) -> None:
    peer = channel(lambda p: p.options[0].id)
    choose_one("thing", [Option("a", "A", "a"), Option("b", "B", "b")])
    assert peer.prompts[0].default is None


def test_choose_one_destructive_asks_even_single_option(channel: Any) -> None:
    peer = channel(lambda p: p.options[0].id)
    assert choose_one("thing", [Option(7, "t", "l")], destructive=True) == 7
    assert peer.prompts[0].destructive is True


def test_choose_one_single_option_never_reaches_channel(channel: Any, capsys: Any) -> None:
    peer = channel(lambda p: pytest.fail("asked"))
    assert choose_one("thing", [Option(7, "t", "l")]) == 7
    assert peer.prompts == []


def test_choose_many_returns_values_in_answer_order_and_empty_is_valid(channel: Any) -> None:
    answers = iter([["2", "0"], []])
    channel(lambda p: next(answers))
    opts = [Option(i, str(i), str(i)) for i in range(3)]
    assert choose_many("things", opts) == [2, 0]
    assert choose_many("things", opts) == []


def test_choose_many_single_option_is_still_asked(channel: Any) -> None:
    peer = channel(lambda p: ["0"])
    assert choose_many("things", [Option("only", "o", "o")]) == ["only"]
    assert len(peer.prompts) == 1


def test_text_answer_default_completions_and_validation_loop(channel: Any) -> None:
    answers = iter(["bad", "good"])
    peer = channel(lambda p: next(answers))
    got = ask_text(
        "name",
        validate=lambda t: None if t == "good" else "nope",
        default="d",
        completions=["a", "b"],
        clear_shortcut=True,
    )
    assert got == "good"
    assert len(peer.prompts) == 2
    p = peer.prompts[0]
    assert (p.kind, p.default, p.completions, p.clear_shortcut) == ("text", "d", ("a", "b"), True)
    assert peer.prompts[0].id != peer.prompts[1].id


def test_secret_answer(channel: Any) -> None:
    peer = channel(lambda p: "hunter2")
    assert ask_secret("token") == "hunter2"
    assert peer.prompts[0].kind == "secret"


@pytest.mark.parametrize(
    "call, raised",
    [
        # A confirmation has always been typer's: its cancel is typer's Abort.
        (lambda: ask_confirm("q"), typer.Abort),
        (lambda: choose_one("n", [Option(1, "a", "a"), Option(2, "b", "b")]), Cancelled),
        (lambda: choose_many("n", [Option(1, "a", "a")]), Cancelled),
        (lambda: ask_text("n", validate=lambda t: None), Cancelled),
        (lambda: ask_secret("n"), Cancelled),
    ],
    ids=["confirm", "choose_one", "choose_many", "text", "secret"],
)
def test_cancel_has_the_cancel_exit_for_every_kind(
    channel: Any, call: Callable[[], object], raised: type[Exception]
) -> None:
    channel(lambda p: None)
    with pytest.raises(raised):
        call()


def test_eof_is_cancel(channel: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd_sock, ui_sock = socket.socketpair()
    monkeypatch.setenv("JAILBEE_PROMPT_FD", str(cmd_sock.fileno()))
    ui_sock.close()
    try:
        with pytest.raises(typer.Abort):
            ask_confirm("q")
    finally:
        prompting.drop_channel()
        cmd_sock.close()


def test_ui_dying_mid_question_cancels(channel: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd_sock, ui_sock = socket.socketpair()
    monkeypatch.setenv("JAILBEE_PROMPT_FD", str(cmd_sock.fileno()))

    def die() -> None:
        ui = pc.UiEnd(ui_sock)
        assert ui.next_prompt() is not None
        ui.close()

    t = threading.Thread(target=die, daemon=True)
    t.start()
    try:
        with pytest.raises(typer.Abort):
            ask_confirm("q")
    finally:
        t.join(JOIN)
        prompting.drop_channel()
        cmd_sock.close()


def test_noninteractive_wins_over_channel(channel: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    peer = channel(lambda p: True)
    monkeypatch.setenv("JAILBEE_NONINTERACTIVE", "1")
    assert prompting.is_interactive() is False
    with pytest.raises(MissingValue):
        ask_confirm("q", alternative="--yes")
    assert peer.prompts == []


def test_is_interactive_true_with_channel_even_without_tty(
    channel: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert prompting.is_interactive() is True


@pytest.mark.parametrize("value", ["abc", "999", "1", "-4", ""])
def test_invalid_fd_falls_back_as_if_absent(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("JAILBEE_PROMPT_FD", value)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert prompting.is_interactive() is False
    with pytest.raises(MissingValue):
        ask_confirm("q")


def test_non_socket_fd_is_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    with open(tmp_path / "f", "w") as fh:
        monkeypatch.setenv("JAILBEE_PROMPT_FD", str(fh.fileno()))
        monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
        assert prompting.is_interactive() is False


def test_inherited_fd_stays_open_after_use(channel: Any) -> None:
    import os

    channel(lambda p: True)
    fd = int(os.environ["JAILBEE_PROMPT_FD"])
    ask_confirm("q")
    os.fstat(fd)  # raises if closed


def test_secret_never_in_logs_on_either_side(
    channel: Any, caplog: pytest.LogCaptureFixture
) -> None:
    channel(lambda p: "s3cr3t-value" if p.kind == "secret" else True)
    with caplog.at_level(logging.DEBUG, logger="jailbee.prompt_channel"):
        assert ask_secret("token") == "s3cr3t-value"
        ask_confirm("visible question")
    text = caplog.text
    assert "s3cr3t-value" not in text
    assert "<redacted>" in text
    assert "visible question" in text  # logging is on, so the check above means something


def test_version_mismatch_fails_clearly_on_command_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd_sock, ui_sock = socket.socketpair()
    monkeypatch.setenv("JAILBEE_PROMPT_FD", str(cmd_sock.fileno()))

    def old_ui() -> None:
        ui_sock.sendall(json.dumps({"type": "hello", "version": 99, "role": "ui"}).encode() + b"\n")
        ui_sock.makefile("rb").readline()

    t = threading.Thread(target=old_ui, daemon=True)
    t.start()
    try:
        with pytest.raises(MissingValue) as exc:
            ask_confirm("q")
        assert "version mismatch" in exc.value.message
        assert "99" in exc.value.message
    finally:
        ui_sock.close()
        t.join(JOIN)
        prompting.drop_channel()
        cmd_sock.close()


def test_version_mismatch_raises_on_ui_side() -> None:
    cmd_sock, ui_sock = socket.socketpair()
    cmd_sock.sendall(
        json.dumps({"type": "hello", "version": 0, "role": "command"}).encode() + b"\n"
    )
    ui = pc.UiEnd(ui_sock)
    with pytest.raises(pc.ProtocolVersionError):
        ui.next_prompt()
    ui.close()
    cmd_sock.close()


def test_reserved_events_are_skipped(channel: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd_sock, ui_sock = socket.socketpair()
    monkeypatch.setenv("JAILBEE_PROMPT_FD", str(cmd_sock.fileno()))

    def ui() -> None:
        end = pc.UiEnd(ui_sock)
        p = end.next_prompt()
        assert p is not None
        for extra in ({"type": "progress", "n": 1}, {"type": "output", "text": "x"}):
            ui_sock.sendall(json.dumps(extra).encode() + b"\n")
        end.answer(p, True)

    t = threading.Thread(target=ui, daemon=True)
    t.start()
    try:
        assert ask_confirm("q") is True
    finally:
        t.join(JOIN)
        prompting.drop_channel()
        cmd_sock.close()
        ui_sock.close()


def test_nonsense_answer_is_a_failure_not_a_wrong_value(channel: Any) -> None:
    channel(lambda p: "no-such-id")
    with pytest.raises(MissingValue):
        choose_one("n", [Option(1, "a", "a"), Option(2, "b", "b")])


def test_choose_one_sends_the_explicit_question(channel: Any) -> None:
    peer = channel(lambda p: p.options[0].id)
    choose_one(
        "pending PR description",
        [Option("a", "A", "alpha"), Option("b", "B", "beta")],
        question="More than one PR description is pending. Which one?",
    )
    assert peer.prompts[0].question == "More than one PR description is pending. Which one?"


# ---- adoption: the descriptor variable is consumed once ----------------------------


def test_adoption_consumes_the_variable_and_later_asks_still_use_the_channel(
    channel: Any,
) -> None:
    import os

    peer = channel(lambda p: True)
    assert os.environ.get(pc.PROMPT_FD_ENV)
    prompting.adopt_channel()
    assert pc.PROMPT_FD_ENV not in os.environ  # a grandchild cannot inherit the name
    assert prompting.is_interactive() is True
    assert ask_confirm("q") is True
    assert len(peer.prompts) == 1


def test_adoption_makes_the_descriptor_non_inheritable(channel: Any) -> None:
    import os

    channel(lambda p: True)
    fd = int(os.environ[pc.PROMPT_FD_ENV])
    prompting.adopt_channel()
    assert os.get_inheritable(fd) is False
    os.fstat(fd)  # still open: the inherited end is never closed


def test_a_grandchild_does_not_see_the_prompt_variable(channel: Any) -> None:
    import subprocess
    import sys

    channel(lambda p: True)
    prompting.adopt_channel()
    out = subprocess.run(
        [sys.executable, "-c", "import os;print(os.environ.get('JAILBEE_PROMPT_FD'))"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip() == "None"


# ---- wire details the dashboard shows ----------------------------------------------


def test_confirm_detail_travels(channel: Any) -> None:
    peer = channel(lambda p: True)
    ask_confirm("Destroy?", destructive=True, detail="⚠ 3 unpushed commits")
    assert peer.prompts[0].detail == "⚠ 3 unpushed commits"


def test_choose_many_defaults_travel_as_option_ids(channel: Any) -> None:
    peer = channel(lambda p: ["1"])
    opts = [Option("a", "A", "a"), Option("b", "B", "b"), Option("c", "C", "c")]
    choose_many("things", opts, default=["a", "c"])
    assert peer.prompts[0].default == ("0", "2")


def test_choose_one_question_is_the_terminals_header_by_default(channel: Any) -> None:
    peer = channel(lambda p: p.options[0].id)
    choose_one("login", [Option("a", "A", "a"), Option("b", "B", "b")])
    choose_one("login", [Option("a", "A", "a"), Option("b", "B", "b")], question="Which?")
    assert [p.question for p in peer.prompts] == ["Select login:", "Which?"]


def test_a_refused_text_answer_is_explained_on_the_next_prompt(channel: Any) -> None:
    answers = iter(["bad", "good"])
    peer = channel(lambda p: next(answers))
    ask_text("name", validate=lambda t: None if t == "good" else "not a name")
    assert [p.error for p in peer.prompts] == [None, "not a name"]
