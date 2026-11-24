import {flushPromises, mount} from "@vue/test-utils"
import {afterEach, describe, expect, it, vi} from "vitest"
import {createMemoryHistory} from "vue-router"
import {clearToken, setToken} from "./api"
import App from "./App.vue"
import {DASHBOARD, Dashboard, type SocketLike} from "./dashboard"
import {createAppRouter} from "./router"
import type {SocketHandlers} from "./socket"

function harness() {
    const sockets: {handlers: SocketHandlers; started: boolean}[] = []
    const dashboard = new Dashboard((handlers): SocketLike => {
        const socket = {
            handlers,
            started: false,
            start() {
                socket.started = true
            },
            stop() {
                socket.started = false
            },
            send() {},
        }
        sockets.push(socket)
        return socket
    })
    return {dashboard, sockets}
}

async function mountAt(path: string) {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", {status: 404})))
    const {dashboard, sockets} = harness()
    const router = createAppRouter(createMemoryHistory())
    await router.push(path)
    await router.isReady()
    const wrapper = mount(App, {global: {plugins: [router], provide: {[DASHBOARD]: dashboard}}})
    await flushPromises()
    return {wrapper, router, dashboard, sockets}
}

afterEach(() => {
    clearToken()
})

describe("App", () => {
    it("opens the dashboard socket on a deep link to a signed-in page", async () => {
        setToken("tok")
        const {sockets} = await mountAt("/repos/alpha")
        expect(sockets).toHaveLength(1)
        expect(sockets[0].started).toBe(true)
    })

    it("keeps the socket closed on the login page and opens it after signing in", async () => {
        const {router, sockets} = await mountAt("/login")
        expect(sockets).toHaveLength(0)
        setToken("tok")
        await router.push("/containers/alpha-1")
        await flushPromises()
        expect(sockets).toHaveLength(1)
        expect(sockets[0].started).toBe(true)
    })

    it("closes the socket when leaving for the login page", async () => {
        setToken("tok")
        const {router, sockets} = await mountAt("/")
        await router.push("/login")
        await flushPromises()
        expect(sockets[0].started).toBe(false)
    })

    it("opens the setup view when the socket says the server is in setup mode", async () => {
        setToken("tok")
        const {router, sockets} = await mountAt("/repos/alpha")
        sockets[0].handlers.onSetupMode?.()
        await flushPromises()
        expect(router.currentRoute.value.name).toBe("setup")
        expect(sockets[0].started).toBe(false)
    })

    it("sends the user to login when the socket says the session is gone", async () => {
        setToken("tok")
        const {router, sockets} = await mountAt("/repos/alpha")
        sockets[0].handlers.onUnauthorized()
        await flushPromises()
        expect(router.currentRoute.value.name).toBe("login")
    })

    describe("setup view", () => {
        afterEach(() => {
            vi.useRealTimers()
        })

        async function setupTab(modes: string[]) {
            vi.useFakeTimers()
            const fetchMock = vi.fn(async () => Response.json({mode: modes.shift() ?? "dashboard"}))
            vi.stubGlobal("fetch", fetchMock)
            const {dashboard} = harness()
            const router = createAppRouter(createMemoryHistory())
            await router.push("/login")
            const wrapper = mount(App, {global: {plugins: [router], provide: {[DASHBOARD]: dashboard}}})
            await flushPromises()
            return {router, wrapper, fetchMock}
        }

        it("polls while visible and leaves for login once setup mode ends", async () => {
            const {router, wrapper} = await setupTab(["setup", "setup", "setup"])
            expect(router.currentRoute.value.name).toBe("setup")

            await vi.advanceTimersByTimeAsync(2_000)
            expect(router.currentRoute.value.name).toBe("setup")
            await vi.advanceTimersByTimeAsync(2_000)
            await flushPromises()

            expect(router.currentRoute.value.name).toBe("login")
            wrapper.unmount()
        })

        it("does not poll while the page is hidden", async () => {
            const {router, wrapper, fetchMock} = await setupTab(["setup"])
            vi.spyOn(document, "hidden", "get").mockReturnValue(true)
            const before = fetchMock.mock.calls.length

            await vi.advanceTimersByTimeAsync(6_000)

            expect(fetchMock.mock.calls.length).toBe(before)
            expect(router.currentRoute.value.name).toBe("setup")
            wrapper.unmount()
            vi.restoreAllMocks()
        })
    })
})
