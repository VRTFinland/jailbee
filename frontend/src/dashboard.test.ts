import {describe, expect, it, vi} from "vitest"
import {Dashboard, type SocketLike} from "./dashboard"
import {PROTOCOL_VERSION, type ClientMessage, type FollowKey, type ServerMessage} from "./protocol"
import type {SocketHandlers} from "./socket"

class FakeSocket implements SocketLike {
    sent: ClientMessage[] = []
    started = false
    constructor(readonly handlers: SocketHandlers) {}
    start() {
        this.started = true
    }
    stop() {
        this.started = false
    }
    send(message: ClientMessage) {
        this.sent.push(message)
    }
    hello() {
        this.handlers.onConnectionChange?.(true)
        this.handlers.onMessage({type: "hello", v: PROTOCOL_VERSION, device_name: "boox"})
    }
    drop() {
        this.handlers.onConnectionChange?.(false)
    }
    deliver(message: ServerMessage) {
        this.handlers.onMessage(message)
    }
}

function setup() {
    const sockets: FakeSocket[] = []
    const dashboard = new Dashboard((handlers) => {
        const socket = new FakeSocket(handlers)
        sockets.push(socket)
        return socket
    })
    return {dashboard, sockets}
}

const key: FollowKey = {prefix: "alpha", agent: "claude", session_id: "s-1", subagent_id: null}
const entry = {kind: "assistant", id: "a9", timestamp: null, html: "<p>new</p>", model: null} as const

describe("Dashboard", () => {
    it("opens one socket however often it is started, and tracks the front page", () => {
        const {dashboard, sockets} = setup()
        dashboard.start()
        dashboard.start()
        expect(sockets).toHaveLength(1)
        sockets[0].hello()
        expect(dashboard.state.connected).toBe(true)
        expect(dashboard.state.device).toBe("boox")
        sockets[0].deliver({type: "status", v: PROTOCOL_VERSION, message: "down"})
        expect(dashboard.state.status).toBe("down")
        dashboard.stop()
        expect(sockets[0].started).toBe(false)
        expect(dashboard.state.connected).toBe(false)
    })

    it("subscribes once connected and hands appends to the follower", () => {
        const {dashboard, sockets} = setup()
        dashboard.start()
        const onAppend = vi.fn()
        dashboard.follow(key, 120, {onAppend})
        expect(sockets[0].sent).toEqual([])

        sockets[0].hello()
        expect(sockets[0].sent).toEqual([{type: "subscribe", v: PROTOCOL_VERSION, key, offset: 120}])

        sockets[0].deliver({type: "append", v: PROTOCOL_VERSION, key, entries: [entry], offset: 200})
        expect(onAppend).toHaveBeenCalledWith([entry])
    })

    it("resubscribes from the latest offset after a reconnect", () => {
        const {dashboard, sockets} = setup()
        dashboard.start()
        sockets[0].hello()
        dashboard.follow(key, 120, {onAppend: vi.fn()})
        sockets[0].deliver({type: "append", v: PROTOCOL_VERSION, key, entries: [entry], offset: 200})
        sockets[0].drop()
        sockets[0].sent = []
        sockets[0].hello()
        expect(sockets[0].sent).toEqual([{type: "subscribe", v: PROTOCOL_VERSION, key, offset: 200}])
    })

    it("stops delivering after unfollow and tells the server", () => {
        const {dashboard, sockets} = setup()
        dashboard.start()
        sockets[0].hello()
        const onAppend = vi.fn()
        const unfollow = dashboard.follow(key, 0, {onAppend})
        unfollow()
        expect(sockets[0].sent.at(-1)).toEqual({type: "unsubscribe", v: PROTOCOL_VERSION, key})
        sockets[0].deliver({type: "append", v: PROTOCOL_VERSION, key, entries: [entry], offset: 9})
        expect(onAppend).not.toHaveBeenCalled()
    })

    it("tells the follower when the server refuses the subscription", () => {
        const {dashboard, sockets} = setup()
        dashboard.start()
        sockets[0].hello()
        const onError = vi.fn()
        dashboard.follow(key, 0, {onAppend: vi.fn(), onError})
        sockets[0].deliver({type: "subscribe_error", v: PROTOCOL_VERSION, key, message: "Unknown session"})
        expect(onError).toHaveBeenCalledWith("Unknown session")
    })

    it("keeps subagent and session subscriptions apart", () => {
        const {dashboard, sockets} = setup()
        dashboard.start()
        sockets[0].hello()
        const session = vi.fn()
        const subagent = vi.fn()
        const subKey = {...key, subagent_id: "a1"}
        dashboard.follow(key, 0, {onAppend: session})
        dashboard.follow(subKey, 0, {onAppend: subagent})
        sockets[0].deliver({type: "append", v: PROTOCOL_VERSION, key: subKey, entries: [entry], offset: 9})
        expect(subagent).toHaveBeenCalledOnce()
        expect(session).not.toHaveBeenCalled()
    })
})
