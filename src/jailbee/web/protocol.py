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

Live transcript follow: the browser sends `Subscribe` with the byte offset
its REST `TranscriptView` was read to; the server answers `TranscriptAppend`
with the entries written since (at once if any, then on every change), or
`SubscribeError` for a session the REST endpoint would answer 404.
`Unsubscribe` (or closing the socket) stops it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

PROTOCOL_VERSION = 7

CLOSE_UNAUTHORIZED = 4001
"""No valid session token: the browser goes back to the login page."""
CLOSE_PROTOCOL_MISMATCH = 4002
"""The browser speaks another protocol version: it should reload the app."""
CLOSE_SETUP_MODE = 4003
"""The server runs FIDO2 setup mode, not the dashboard: the browser opens the setup view."""
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


MetaMode = Literal["hidden", "collapsed", "shown"]
"""The *Agent context* display setting: what the server sends of the agent's own
wrapper records (task notifications, reminders, command output, ...). ``hidden``
(the default) omits them, ``collapsed`` sends only a label and the content is
fetched on expand, ``shown`` sends everything. A slash command the user typed
and an interruption notice keep their label line in every mode."""


class FollowKey(BaseModel):
    """Which transcript a subscription follows: a session, or one of its
    subagents when ``subagent_id`` is set. Hashable, so it keys a connection's
    subscriptions."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    prefix: str
    agent: str
    session_id: str
    subagent_id: str | None = None


class Subscribe(_Message):
    """Follow a transcript from ``offset`` (`TranscriptView.offset`). Subscribing
    again to the same key restarts it from the new offset."""

    type: Literal["subscribe"] = "subscribe"
    key: FollowKey
    offset: int = Field(ge=0)
    meta: MetaMode = "hidden"
    """The Agent context setting the appends are rendered with."""


class Unsubscribe(_Message):
    type: Literal["unsubscribe"] = "unsubscribe"
    key: FollowKey


ClientMessage = Annotated[ClientAuth | Subscribe | Unsubscribe, Field(discriminator="type")]
"""Every client message type."""

_CLIENT: TypeAdapter[ClientAuth | Subscribe | Unsubscribe] = TypeAdapter(ClientMessage)


def decode_client(text: str) -> ClientAuth | Subscribe | Unsubscribe:
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


class ChildRelation(BaseModel):
    """Display-only orchestration facts, without grant or child rights."""

    model_config = ConfigDict(frozen=True)
    orchestrator: str | None
    orchestrator_short_name: str
    state: str
    runner: str | None


class ContainerRow(BaseModel):
    """One container: its name, fork-tree indent glyphs, agent state and cells.

    ``cells`` is plain text aligned with the page's ``columns``. ``agent`` is
    the most urgent agent state (``waiting``, ``busy``, ``idle``, ...), or
    None when no agent runs there. ``state`` is the container's Incus state
    (``Running``, ``Stopped``, ...) and ``phase`` its non-normal row phase
    (``booting``, ``autostart``, ``job``, ``failed``; the dashboard core's
    ``row_phase``), or None at rest; the page styles the row from them.
    """

    model_config = ConfigDict(frozen=True)
    name: str
    indent: str
    agent: str | None
    agent_recent_idle: str | None = None
    """Age of an emphasized idle session, even if another session is more urgent."""
    state: str
    phase: str | None
    cells: list[str]
    depth: int = 0
    orchestrator: bool = False
    child_count: int = 0
    child: ChildRelation | None = None


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
    live: bool = False
    """True while the agent process runs, from the state-service snapshot."""
    container: str | None = None
    """The container the session was seen in, or None when unattributed."""


class SessionListView(BaseModel):
    """`GET /api/repos/{prefix}/sessions`: newest activity first."""

    model_config = ConfigDict(frozen=True)
    sessions: list[SessionView]


class _Rendered(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    """The transcript entry's id: stable across reads, unique in a session."""
    timestamp: datetime | None


class RenderedUser(_Rendered):
    """A prompt. ``meta`` marks an agent's own wrapper text (``<command-name>``,
    ``<system-reminder>``, ...). A meta record carries a short ``label`` ("Task
    notification", "Command: /model", "Shell: ls"); its ``html`` is the wrapper
    text verbatim when the Agent context setting is ``shown``, and empty
    otherwise. ``expandable`` says the content was withheld and `EntryContent`
    serves it. Context split off another entry (wrappers before a typed prompt,
    ``<system-reminder>`` blocks appended to a tool result) is its own record
    with the id ``<entry id>:meta``."""

    kind: Literal["user"] = "user"
    html: str
    meta: bool = False
    label: str | None = None
    expandable: bool = False


class RenderedAssistant(_Rendered):
    kind: Literal["assistant"] = "assistant"
    html: str
    model: str | None


class RenderedThinking(_Rendered):
    """``html`` is empty when the agent stored the thinking redacted."""

    kind: Literal["thinking"] = "thinking"
    html: str
    model: str | None


class RenderedToolCall(_Rendered):
    """One tool call: ``summary`` is the folded one-liner, ``input_html`` the
    arguments as highlighted JSON. Its result is a separate `RenderedToolResult`
    with the same ``call_id``, which may arrive in a later read."""

    kind: Literal["tool_call"] = "tool_call"
    call_id: str
    name: str
    summary: str
    input_html: str
    model: str | None


class RenderedToolResult(_Rendered):
    """What a tool returned, escaped into ``html``; cut at the source's limit
    when ``truncated`` (``full_length`` is the untruncated length)."""

    kind: Literal["tool_result"] = "tool_result"
    call_id: str
    html: str
    truncated: bool
    full_length: int
    is_error: bool


class RenderedSubagent(_Rendered):
    """A subagent started by the tool call ``call_id``; ``agent_id`` opens it."""

    kind: Literal["subagent"] = "subagent"
    call_id: str
    agent_id: str
    agent_type: str | None
    description: str | None


RenderedEntry = Annotated[
    RenderedUser
    | RenderedAssistant
    | RenderedThinking
    | RenderedToolCall
    | RenderedToolResult
    | RenderedSubagent,
    Field(discriminator="kind"),
]
"""One transcript entry, ready to display. The REST session view and the
WebSocket's live appends carry the same shape."""


class EntryContent(BaseModel):
    """`GET .../entries/{entry_id}`: one entry's full rendering, for a
    withheld meta record the browser expands."""

    model_config = ConfigDict(frozen=True)
    id: str
    html: str


class ChatAvailability(BaseModel):
    available: bool
    code: str
    message: str


class TranscriptView(BaseModel):
    """`GET /api/repos/{prefix}/sessions/{agent}/{session_id}`: a whole session."""

    model_config = ConfigDict(frozen=True)
    session_id: str
    agent: str
    title: str
    last_activity: datetime | None
    models: list[str]
    entries: list[RenderedEntry]
    chat: ChatAvailability = ChatAvailability(
        available=False, code="permission_denied", message="Agent chat is disabled"
    )
    offset: int = 0
    """Byte offset the entries were read to: where a live `Subscribe` resumes."""


class ContainerFilterRows(BaseModel):
    """Scoped filter membership and recomputed tree prefixes, without duplicate cells."""

    model_config = ConfigDict(frozen=True)
    visible: list[str]
    context: list[str]
    indents: dict[str, str] = Field(default_factory=dict)


class FrontPageView(BaseModel):
    """The whole front page, sent in full on every snapshot."""

    model_config = ConfigDict(frozen=True)
    seq: int
    gathered_at: datetime
    columns: list[ColumnHead]
    repos: list[RepoRow]
    filters: dict[Literal["running", "active"], ContainerFilterRows] = Field(default_factory=dict)


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


class TranscriptAppend(_Message):
    """Entries a followed transcript gained; ``offset`` is where the next read
    starts. Sent only when the offset moved: ``entries`` is empty when the Agent
    context setting left out everything read. A partly written line waits until
    complete."""

    type: Literal["append"] = "append"
    key: FollowKey
    entries: list[RenderedEntry]
    offset: int
    chat: ChatAvailability | None = None


class SubscribeError(_Message):
    """A subscription refused: unknown, hidden or invalid. Nothing follows."""

    type: Literal["subscribe_error"] = "subscribe_error"
    key: FollowKey
    message: str


ServerMessageType = ServerHello | FrontPage | Status | TranscriptAppend | SubscribeError

ServerMessage = Annotated[ServerMessageType, Field(discriminator="type")]

_SERVER: TypeAdapter[ServerMessageType] = TypeAdapter(ServerMessage)


def encode(message: _Message) -> str:
    return message.model_dump_json()


def decode_server(text: str) -> ServerMessageType:
    """For tests and tools: parse a server message."""
    return _SERVER.validate_json(text)
