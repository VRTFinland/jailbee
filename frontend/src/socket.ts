// The /api/ws connection: auth handshake, typed messages, reconnect.
import {clearToken, getToken} from "./api"
import {
    CLOSE_PROTOCOL_MISMATCH,
    CLOSE_SETUP_MODE,
    CLOSE_UNAUTHORIZED,
    PROTOCOL_VERSION,
    type ClientMessage,
    type ServerMessage,
} from "./protocol"

export interface SocketHandlers {
    onMessage(message: ServerMessage): void
    onConnectionChange?(connected: boolean): void
    /** The session is gone: the caller sends the user to the login page. */
    onUnauthorized(): void
    /** The server runs FIDO2 setup mode: the caller opens the setup view. */
    onSetupMode?(): void
    /** Reloading did not recover a compatible client; ask the user to retry. */
    onOutdated?(): void
}

const BACKOFF_MS = [1000, 2000, 4000, 8000, 15000]
const RELOAD_KEY = "jailbee.protocolReload"
const RELOAD_WINDOW_MS = 60_000

function mayReload(): boolean {
    try {
        const last = Number(sessionStorage.getItem(RELOAD_KEY))
        const now = Date.now()
        if (last > 0 && now - last < RELOAD_WINDOW_MS) return false
        sessionStorage.setItem(RELOAD_KEY, String(now))
        return true
    } catch {
        // Without durable tab storage an automatic reload could loop forever.
        return false
    }
}

export class DashboardSocket {
    private ws: WebSocket | null = null
    private attempt = 0
    private stopped = false
    private retry: ReturnType<typeof setTimeout> | null = null

    constructor(private readonly handlers: SocketHandlers) {}

    start(): void {
        this.stopped = false
        this.open()
    }

    stop(): void {
        this.stopped = true
        if (this.retry !== null) clearTimeout(this.retry)
        this.ws?.close()
        this.ws = null
    }

    send(message: ClientMessage): void {
        this.ws?.send(JSON.stringify(message))
    }

    private open(): void {
        const token = getToken()
        if (!token) {
            this.handlers.onUnauthorized()
            return
        }
        const scheme = location.protocol === "https:" ? "wss:" : "ws:"
        const ws = new WebSocket(`${scheme}//${location.host}/api/ws`)
        this.ws = ws
        ws.onopen = () => {
            this.send({type: "auth", v: PROTOCOL_VERSION, token})
        }
        ws.onmessage = (event: MessageEvent<string>) => {
            const message = JSON.parse(event.data) as ServerMessage
            if (message.type === "hello") {
                this.attempt = 0
                this.handlers.onConnectionChange?.(true)
            }
            this.handlers.onMessage(message)
        }
        ws.onclose = (event: CloseEvent) => {
            this.ws = null
            this.handlers.onConnectionChange?.(false)
            if (this.stopped) return
            if (event.code === CLOSE_UNAUTHORIZED) {
                clearToken()
                this.handlers.onUnauthorized()
                return
            }
            if (event.code === CLOSE_SETUP_MODE) {
                this.handlers.onSetupMode?.()
                return
            }
            if (event.code === CLOSE_PROTOCOL_MISMATCH) {
                // Retry fresh assets once, but stop if a cache keeps serving
                // the stale client (or tab storage cannot guard the retry).
                if (mayReload()) location.reload()
                else this.handlers.onOutdated?.()
                return
            }
            const delay = BACKOFF_MS[Math.min(this.attempt, BACKOFF_MS.length - 1)]
            this.attempt += 1
            this.retry = setTimeout(() => this.open(), delay)
        }
    }
}
