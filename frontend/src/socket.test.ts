import {afterEach, describe, expect, it, vi} from "vitest"
import {clearToken, setToken} from "./api"
import {DashboardSocket, type SocketHandlers} from "./socket"

class FakeWebSocket {
    static last: FakeWebSocket
    onopen: (() => void) | null = null
    onmessage: ((e: MessageEvent<string>) => void) | null = null
    onclose: ((e: CloseEvent) => void) | null = null
    constructor(readonly url: string) {
        FakeWebSocket.last = this
    }
    send(): void {}
    close(): void {}
}

function connect() {
    setToken("tok")
    vi.stubGlobal("WebSocket", FakeWebSocket)
    const handlers = {
        onMessage: vi.fn(),
        onUnauthorized: vi.fn(),
        onSetupMode: vi.fn(),
        onOutdated: vi.fn(),
    } satisfies SocketHandlers
    const socket = new DashboardSocket(handlers)
    socket.start()
    const close = (code: number) => FakeWebSocket.last.onclose?.({code} as CloseEvent)
    return {handlers, close, socket}
}

afterEach(() => {
    clearToken()
    vi.unstubAllGlobals()
    sessionStorage.clear()
})

describe("DashboardSocket close codes", () => {
    it("4003 opens setup mode, not the outdated notice or login", () => {
        const {handlers, close} = connect()
        close(4003)
        expect(handlers.onSetupMode).toHaveBeenCalledTimes(1)
        expect(handlers.onOutdated).not.toHaveBeenCalled()
        expect(handlers.onUnauthorized).not.toHaveBeenCalled()
    })

    it("4001 clears the session and reports unauthorized only", () => {
        const {handlers, close} = connect()
        close(4001)
        expect(handlers.onUnauthorized).toHaveBeenCalledTimes(1)
        expect(handlers.onSetupMode).not.toHaveBeenCalled()
        expect(handlers.onOutdated).not.toHaveBeenCalled()
    })

    it("4002 asks the user to retry when a reload was already tried", () => {
        sessionStorage.setItem("jailbee.protocolReload", String(Date.now()))
        const {handlers, close} = connect()
        close(4002)
        expect(handlers.onOutdated).toHaveBeenCalledTimes(1)
        expect(handlers.onSetupMode).not.toHaveBeenCalled()
        expect(handlers.onUnauthorized).not.toHaveBeenCalled()
    })
})
