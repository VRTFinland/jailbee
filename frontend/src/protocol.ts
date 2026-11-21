// Mirror of src/jailbee/web/protocol.py. Bump PROTOCOL_VERSION in both files
// on any change either side could misread.

export const PROTOCOL_VERSION = 3

export const CLOSE_UNAUTHORIZED = 4001
export const CLOSE_PROTOCOL_MISMATCH = 4002
export const CLOSE_BAD_MESSAGE = 4400

export interface ClientAuth {
    type: "auth"
    v: number
    token: string
}

/** Which transcript a live subscription follows; `subagent_id` picks a subagent's. */
export interface FollowKey {
    prefix: string
    agent: string
    session_id: string
    subagent_id: string | null
}

/** Follow a transcript from `offset` (`TranscriptView.offset`). */
export interface Subscribe {
    type: "subscribe"
    v: number
    key: FollowKey
    offset: number
}

export interface Unsubscribe {
    type: "unsubscribe"
    v: number
    key: FollowKey
}

export type ClientMessage = ClientAuth | Subscribe | Unsubscribe

export interface ColumnHead {
    name: string
    header: string
}

export interface ContainerRow {
    name: string
    indent: string
    agent: string | null
    cells: string[]
}

export interface RepoRow {
    prefix: string
    containers: ContainerRow[]
}

export interface FrontPageView {
    seq: number
    gathered_at: string
    columns: ColumnHead[]
    repos: RepoRow[]
}

export interface ServerHello {
    type: "hello"
    v: number
    device_name: string
}

export interface FrontPage {
    type: "front_page"
    v: number
    view: FrontPageView
}

export interface Status {
    type: "status"
    v: number
    message: string | null
}

/** Entries a followed transcript gained; never empty. */
export interface TranscriptAppend {
    type: "append"
    v: number
    key: FollowKey
    entries: RenderedEntry[]
    offset: number
}

/** A subscription refused (unknown or hidden session): nothing follows. */
export interface SubscribeError {
    type: "subscribe_error"
    v: number
    key: FollowKey
    message: string
}

export type ServerMessage = ServerHello | FrontPage | Status | TranscriptAppend | SubscribeError

/** One row of `GET /api/repos/{prefix}/sessions` (REST, not the WebSocket). */
export interface SessionSummary {
    session_id: string
    agent: string
    title: string
    last_activity: string | null
    models: string[]
    /** The container the session was seen in; null when unattributed. */
    container: string | null
}

export interface SessionList {
    sessions: SessionSummary[]
}

/** One row of `GET /api/containers/{name}/sessions`. */
export interface ContainerSession {
    session_id: string
    agent: string
    live: boolean
    title: string
    last_activity: string | null
    models: string[]
}

export interface ContainerSessionList {
    container: string
    repo: string
    sessions: ContainerSession[]
}

// ---- a session's transcript (REST now; the WebSocket's live appends reuse RenderedEntry) ----
// Every `html` / `input_html` field is rendered and sanitised by the server
// (raw HTML escaped, unsafe link schemes dropped): it is the only HTML
// the session view inserts.

interface RenderedBase {
    id: string
    timestamp: string | null
}

export interface RenderedUser extends RenderedBase {
    kind: "user"
    html: string
    /** An agent's own wrapper text (`<command-name>`, `<system-reminder>`, ...). */
    meta: boolean
}

export interface RenderedAssistant extends RenderedBase {
    kind: "assistant"
    html: string
    model: string | null
}

export interface RenderedThinking extends RenderedBase {
    kind: "thinking"
    /** Empty when the agent stored the thinking redacted. */
    html: string
    model: string | null
}

export interface RenderedToolCall extends RenderedBase {
    kind: "tool_call"
    call_id: string
    name: string
    summary: string
    input_html: string
    model: string | null
}

export interface RenderedToolResult extends RenderedBase {
    kind: "tool_result"
    call_id: string
    html: string
    truncated: boolean
    full_length: number
    is_error: boolean
}

export interface RenderedSubagent extends RenderedBase {
    kind: "subagent"
    call_id: string
    agent_id: string
    agent_type: string | null
    description: string | null
}

export type RenderedEntry =
    | RenderedUser
    | RenderedAssistant
    | RenderedThinking
    | RenderedToolCall
    | RenderedToolResult
    | RenderedSubagent

/** `GET /api/repos/{prefix}/sessions/{agent}/{session_id}`. */
export interface TranscriptView {
    session_id: string
    agent: string
    title: string
    last_activity: string | null
    models: string[]
    entries: RenderedEntry[]
    /** Byte offset the entries were read to: where a live subscription resumes. */
    offset: number
}
