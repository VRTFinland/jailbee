"""The `/api/ws` WebSocket protocol: typed JSON messages with a version.

Every message is one JSON object with ``type`` (its name) and ``v`` (the
protocol version it was written for). The browser's mirror of these types is
`frontend/src/protocol.ts`; bump `PROTOCOL_VERSION` on any change either side
could misread, in both files.

Handshake: the browser's first message is `ClientAuth` (the token travels in
the body, never the URL, so it stays out of access logs). The server answers
`ServerHello`, or closes with one of the ``CLOSE_*`` codes. After that the
server pushes `FrontPage` on every state-service snapshot and `Status` when
the state service's health changes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

PROTOCOL_VERSION = 2

CLOSE_UNAUTHORIZED = 4001
"""No valid session token: the browser goes back to the login page."""
CLOSE_PROTOCOL_MISMATCH = 4002
"""The browser speaks another protocol version: it should reload the app."""
CLOSE_BAD_MESSAGE = 4400
"""A message that is not valid JSON or not a known message type."""


class ProtocolError(ValueError):
    """A client message that does not parse."""


class _Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    v: int = PROTOCOL_VERSION


# ---- client -> server ------------------------------------------------------


class ClientAuth(_Message):
    type: Literal["auth"] = "auth"
    token: str


ClientMessage = Annotated[ClientAuth, Field(discriminator="type")]
"""Every client message type. Later slices add transcript subscribe/unsubscribe."""

_CLIENT: TypeAdapter[ClientAuth] = TypeAdapter(ClientMessage)


def decode_client(text: str) -> ClientAuth:
    try:
        return _CLIENT.validate_json(text)
    except ValidationError as exc:
        raise ProtocolError(str(exc)) from exc


# ---- server -> client ------------------------------------------------------


class ColumnHead(BaseModel):
    """One column: the core's field name (stable key) and its dashboard header."""

    model_config = ConfigDict(frozen=True)
    name: str
    header: str


class ContainerRow(BaseModel):
    """One container: its name, fork-tree indent glyphs, agent state and cells.

    ``cells`` is plain text aligned with the page's ``columns``. ``agent`` is
    the most urgent agent state (``waiting``, ``busy``, ``idle``, ...), or
    None when no agent runs there.
    """

    model_config = ConfigDict(frozen=True)
    name: str
    indent: str
    agent: str | None
    cells: list[str]


class RepoRow(BaseModel):
    """A repository heading and its containers."""

    model_config = ConfigDict(frozen=True)
    prefix: str
    containers: list[ContainerRow]


class SessionView(BaseModel):
    """One agent session in a repository's session list (REST, not the WebSocket)."""

    model_config = ConfigDict(frozen=True)
    session_id: str
    agent: str
    title: str
    last_activity: datetime | None
    models: list[str]
    container: str | None = None
    """The container the session was seen in, or None when unattributed."""


class SessionListView(BaseModel):
    """`GET /api/repos/{prefix}/sessions`: newest activity first."""

    model_config = ConfigDict(frozen=True)
    sessions: list[SessionView]


class FrontPageView(BaseModel):
    """The whole front page, sent in full on every snapshot."""

    model_config = ConfigDict(frozen=True)
    seq: int
    gathered_at: datetime
    columns: list[ColumnHead]
    repos: list[RepoRow]


class ServerHello(_Message):
    type: Literal["hello"] = "hello"
    device_name: str


class FrontPage(_Message):
    type: Literal["front_page"] = "front_page"
    view: FrontPageView


class Status(_Message):
    """The state service's health: ``message`` is None while healthy."""

    type: Literal["status"] = "status"
    message: str | None


ServerMessage = Annotated[ServerHello | FrontPage | Status, Field(discriminator="type")]

_SERVER: TypeAdapter[ServerHello | FrontPage | Status] = TypeAdapter(ServerMessage)


def encode(message: _Message) -> str:
    return message.model_dump_json()


def decode_server(text: str) -> ServerHello | FrontPage | Status:
    """For tests and tools: parse a server message."""
    return _SERVER.validate_json(text)
