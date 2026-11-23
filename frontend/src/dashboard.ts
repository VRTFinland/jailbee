// The one dashboard connection a tab holds while on any signed-in page: the
// front page's live state and the live transcript subscriptions. App.vue
// starts it on authenticated routes and stops it on public ones; views reach
// it with useDashboard().
import {inject, reactive, type InjectionKey} from "vue"
import {
    PROTOCOL_VERSION,
    type ClientMessage,
    type FollowKey,
    type FrontPageView,
    type RenderedEntry,
    type ServerMessage,
} from "./protocol"
import type {SocketHandlers} from "./socket"

export interface SocketLike {
    start(): void
    stop(): void
    send(message: ClientMessage): void
}

export type SocketFactory = (handlers: SocketHandlers) => SocketLike

export interface DashboardState {
    view: FrontPageView | null
    status: string | null
    connected: boolean
    device: string
}

export interface FollowHandlers {
    /** New entries, in transcript order. */
    onAppend(entries: RenderedEntry[]): void
    /** The server refused the subscription; nothing more arrives. */
    onError?(message: string): void
}

interface Follow {
    key: FollowKey
    offset: number
    handlers: FollowHandlers
}

function keyId(key: FollowKey): string {
    return JSON.stringify([key.prefix, key.agent, key.session_id, key.subagent_id])
}

export class Dashboard {
    readonly state = reactive<DashboardState>({
        view: null,
        status: null,
        connected: false,
        device: "",
    })
    private socket: SocketLike | null = null
    private readonly follows = new Map<string, Follow>()
    private unauthorized: () => void = () => {}
    private setupMode: () => void = () => {}

    constructor(private readonly factory: SocketFactory) {}

    /** Where to go when the session is gone (App.vue sends the user to login). */
    onUnauthorized(handler: () => void): void {
        this.unauthorized = handler
    }

    /** Where to go when the server turns out to run setup mode (App.vue opens setup). */
    onSetupMode(handler: () => void): void {
        this.setupMode = handler
    }

    start(): void {
        if (this.socket !== null) return
        this.socket = this.factory({
            onMessage: (message) => this.handle(message),
            onConnectionChange: (connected) => {
                this.state.connected = connected
            },
            onUnauthorized: () => this.unauthorized(),
            onSetupMode: () => this.setupMode(),
        })
        this.socket.start()
    }

    stop(): void {
        this.socket?.stop()
        this.socket = null
        this.state.connected = false
    }

    /** Follow a transcript from `offset`; resubscribes after reconnects. Returns unfollow. */
    follow(key: FollowKey, offset: number, handlers: FollowHandlers): () => void {
        const id = keyId(key)
        const entry: Follow = {key, offset, handlers}
        this.follows.set(id, entry)
        if (this.state.connected) this.subscribe(entry)
        return () => {
            if (this.follows.get(id) !== entry) return
            this.follows.delete(id)
            if (this.state.connected) {
                this.socket?.send({type: "unsubscribe", v: PROTOCOL_VERSION, key})
            }
        }
    }

    private subscribe(entry: Follow): void {
        this.socket?.send({
            type: "subscribe",
            v: PROTOCOL_VERSION,
            key: entry.key,
            offset: entry.offset,
        })
    }

    private handle(message: ServerMessage): void {
        switch (message.type) {
            case "hello":
                this.state.device = message.device_name
                for (const entry of this.follows.values()) this.subscribe(entry)
                break
            case "front_page":
                this.state.view = message.view
                break
            case "status":
                this.state.status = message.message
                break
            case "append": {
                const entry = this.follows.get(keyId(message.key))
                if (entry === undefined) return
                entry.offset = message.offset
                entry.handlers.onAppend(message.entries)
                break
            }
            case "subscribe_error": {
                const id = keyId(message.key)
                const entry = this.follows.get(id)
                if (entry === undefined) return
                this.follows.delete(id)
                entry.handlers.onError?.(message.message)
                break
            }
        }
    }
}

export const DASHBOARD: InjectionKey<Dashboard> = Symbol("dashboard")

/** The app's dashboard connection; null where none is provided (isolated tests). */
export function useDashboard(): Dashboard | null {
    return inject(DASHBOARD, null)
}
