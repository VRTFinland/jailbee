"""The prompt channel wire protocol, shared by the command and the UI end.

A command run under a UI inherits one end of a socketpair (its fd number in
`JAILBEE_PROMPT_FD`); every question it asks travels over it as one JSON
object per line, and the command waits for the answer. Stdlib only.

Wire format (every object has a ``type``)::

    {"type": "hello", "version": 1, "role": "command" | "ui"}
    {"type": "prompt", "id": 7, "kind": "confirm" | "choose_one" | "choose_many"
                                       | "text" | "secret",
     "question": str, "options": [{"id": "0", "title": str, "label": str,
                                   "description": str}, ...],
     "default": bool | str | null, "destructive": bool,
     "context": {"repo": str | null, "container": str | null} | null,
     "completions": [str] | null, "clear_shortcut": bool}
    {"type": "answer", "id": 7, "value": ...}   # confirm: bool; choose_one: option
                                                # id; choose_many: [option id];
                                                # text/secret: str
    {"type": "cancel", "id": 7}
    {"type": "progress", ...} / {"type": "output", ...}   # reserved, not emitted

Each side sends its hello first and then reads the peer's; a differing
``version`` raises `ProtocolVersionError` on both ends. EOF means cancel.
There are no timeouts: a question stays open until answered, cancelled or the
peer goes away. Secret answers are never logged (debug lines redact them).
Unknown message types are skipped, so later versions may add events.

The command side uses `CommandEnd`; the UI side uses `UiEnd`.
"""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO, Any

PROTOCOL_VERSION = 1

KINDS = ("confirm", "choose_one", "choose_many", "text", "secret")
# Reserved for later versions: defined, never emitted by this one.
EVENT_TYPES = ("progress", "output")

log = logging.getLogger("jailbee.prompt_channel")

REDACTED = "<redacted>"


class ProtocolError(Exception):
    """The peer sent something that is not the protocol."""


class ProtocolVersionError(ProtocolError):
    """The peer speaks a different protocol version."""


@dataclass(frozen=True)
class WireOption:
    id: str
    title: str
    label: str
    description: str = ""


@dataclass(frozen=True)
class WireContext:
    repo: str | None = None
    container: str | None = None


@dataclass(frozen=True)
class Prompt:
    id: int
    kind: str
    question: str
    options: tuple[WireOption, ...] = ()
    default: bool | str | None = None
    destructive: bool = False
    context: WireContext | None = None
    completions: tuple[str, ...] | None = None
    clear_shortcut: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "type": "prompt",
            "id": self.id,
            "kind": self.kind,
            "question": self.question,
            "options": [
                {"id": o.id, "title": o.title, "label": o.label, "description": o.description}
                for o in self.options
            ],
            "default": self.default,
            "destructive": self.destructive,
            "context": (
                None
                if self.context is None
                else {"repo": self.context.repo, "container": self.context.container}
            ),
            "completions": None if self.completions is None else list(self.completions),
            "clear_shortcut": self.clear_shortcut,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Prompt:
        try:
            kind = data["kind"]
            if kind not in KINDS:
                raise ProtocolError(f"unknown prompt kind {kind!r}")
            ctx = data.get("context")
            comps = data.get("completions")
            return cls(
                id=int(data["id"]),
                kind=kind,
                question=str(data["question"]),
                options=tuple(
                    WireOption(
                        str(o["id"]),
                        str(o.get("title", "")),
                        str(o["label"]),
                        str(o.get("description", "")),
                    )
                    for o in data.get("options") or ()
                ),
                default=data.get("default"),
                destructive=bool(data.get("destructive", False)),
                context=None if ctx is None else WireContext(ctx.get("repo"), ctx.get("container")),
                completions=None if comps is None else tuple(str(c) for c in comps),
                clear_shortcut=bool(data.get("clear_shortcut", False)),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ProtocolError(f"malformed prompt message: {exc!r}") from exc


@dataclass(frozen=True)
class Answer:
    id: int
    value: Any


@dataclass(frozen=True)
class Cancel:
    id: int


@dataclass(frozen=True)
class Event:
    """A reserved progress/output message; carried through but not emitted."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


class Channel:
    """JSON lines over a connected socket, with the hello handshake.

    Never closes the socket it was given unless `close` is called.
    """

    def __init__(self, sock: socket.socket, role: str) -> None:
        self._sock = sock
        self._role = role
        self._rfile: IO[bytes] = sock.makefile("rb")
        self._secret_ids: set[int] = set()
        self._greeted = False
        self.eof = False

    def close(self) -> None:
        try:
            self._rfile.close()
        finally:
            self._sock.close()

    def _log(self, direction: str, obj: dict[str, Any]) -> None:
        if not log.isEnabledFor(logging.DEBUG):
            return
        shown = dict(obj)
        if obj.get("type") == "answer" and obj.get("id") in self._secret_ids:
            shown["value"] = REDACTED
        log.debug("%s %s %s", self._role, direction, json.dumps(shown, default=str))

    def send(self, obj: dict[str, Any]) -> bool:
        """Send one message; False when the peer is gone."""
        if obj.get("type") == "prompt" and obj.get("kind") == "secret":
            self._secret_ids.add(obj["id"])
        self._log("->", obj)
        try:
            self._sock.sendall(json.dumps(obj).encode() + b"\n")
        except OSError:
            self.eof = True
            return False
        return True

    def recv(self) -> dict[str, Any] | None:
        """Next message; None at EOF. Raises `ProtocolError` on garbage."""
        if self.eof:
            return None
        try:
            line = self._rfile.readline()
        except OSError:
            line = b""
        if not line:
            self.eof = True
            return None
        try:
            obj = json.loads(line)
        except ValueError as exc:
            raise ProtocolError("peer sent a line that is not JSON") from exc
        if not isinstance(obj, dict) or "type" not in obj:
            raise ProtocolError("peer sent a message without a type")
        if obj["type"] == "prompt" and obj.get("kind") == "secret":
            self._secret_ids.add(int(obj.get("id", -1)))
        self._log("<-", obj)
        return obj

    def handshake(self) -> bool:
        """Exchange hellos once. False when the peer vanished first."""
        if self._greeted:
            return not self.eof
        self.send({"type": "hello", "version": PROTOCOL_VERSION, "role": self._role})
        peer = self.recv()
        if peer is None:
            return False
        if peer["type"] != "hello":
            raise ProtocolError(f"expected hello, peer sent {peer['type']!r}")
        theirs = peer.get("version")
        if theirs != PROTOCOL_VERSION:
            raise ProtocolVersionError(
                f"prompt channel version mismatch: peer speaks {theirs!r}, "
                f"this side speaks {PROTOCOL_VERSION}"
            )
        self._greeted = True
        return True


class CommandEnd:
    """The command's end: put a question, wait for its answer."""

    def __init__(self, sock: socket.socket) -> None:
        self._ch = Channel(sock, "command")
        self._next_id = 0

    def close(self) -> None:
        self._ch.close()

    def ask(self, prompt_fields: Callable[[int], Prompt]) -> Answer | Cancel:
        """Send the prompt built for a fresh id and wait. EOF is `Cancel`.

        `prompt_fields` receives the id so callers need not manage ids.
        """
        self._next_id += 1
        prompt = prompt_fields(self._next_id)
        if not self._ch.handshake():
            return Cancel(prompt.id)
        if not self._ch.send(prompt.to_json()):
            return Cancel(prompt.id)
        while True:
            msg = self._ch.recv()
            if msg is None:
                return Cancel(prompt.id)
            kind = msg["type"]
            if kind in ("answer", "cancel") and msg.get("id") == prompt.id:
                if kind == "cancel":
                    return Cancel(prompt.id)
                return Answer(prompt.id, msg.get("value"))
            # Stale ids, events and unknown types are skipped.


class UiEnd:
    """The UI's end: receive questions, answer or cancel them.

    `serve(handler)` is the whole loop; ticket code may instead drive
    `next_prompt` / `answer` / `cancel` itself.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._ch = Channel(sock, "ui")

    def close(self) -> None:
        self._ch.close()

    def next_prompt(self) -> Prompt | None:
        """Block for the next prompt; None when the command closed its end."""
        if not self._ch.handshake():
            return None
        while True:
            msg = self._ch.recv()
            if msg is None:
                return None
            if msg["type"] == "prompt":
                return Prompt.from_json(msg)
            # hello repeats, events and unknown types are skipped.

    def answer(self, prompt: Prompt, value: Any) -> None:
        self._ch.send({"type": "answer", "id": prompt.id, "value": value})

    def cancel(self, prompt: Prompt) -> None:
        self._ch.send({"type": "cancel", "id": prompt.id})

    def serve(self, handler: Callable[[Prompt], Any | None]) -> None:
        """Answer every prompt with `handler`'s result until the command ends.

        A handler returning `None` cancels; a raised exception cancels the
        prompt and propagates. Option values travel as the option *id*
        (choose_one) or a list of ids (choose_many).
        """
        while (prompt := self.next_prompt()) is not None:
            try:
                value = handler(prompt)
            except BaseException:
                self.cancel(prompt)
                raise
            if value is None:
                self.cancel(prompt)
            else:
                self.answer(prompt, value)
