import {flushPromises, mount} from "@vue/test-utils"
import {afterEach, describe, expect, it, vi} from "vitest"
import {createMemoryHistory} from "vue-router"
import {clearToken, setToken} from "./api"
import App from "./App.vue"
import {DASHBOARD, Dashboard, type SocketLike} from "./dashboard"
import {createAppRouter, takeResumePath} from "./router"
import type {SocketHandlers} from "./socket"
import {reloadSettings, settings} from "./settings"

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

const mounted: {unmount(): void}[] = []

async function mountAt(path: string) {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", {status: 404})))
    const {dashboard, sockets} = harness()
    const router = createAppRouter(createMemoryHistory())
    await router.push(path)
    await router.isReady()
    const wrapper = mount(App, {global: {plugins: [router], provide: {[DASHBOARD]: dashboard}}})
    mounted.push(wrapper)
    await flushPromises()
    return {wrapper, router, dashboard, sockets}
}

afterEach(() => {
    for (const wrapper of mounted.splice(0)) wrapper.unmount()
    clearToken()
    takeResumePath()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
})

describe("App", () => {
    it("has quick settings on every route, shares full-page settings and resets context on navigation", async () => {
        reloadSettings()
        setToken("tok")
        const {wrapper, router} = await mountAt("/")
        const icon = () => wrapper.get('[aria-label="Quick settings"][aria-haspopup]')
        await icon().trigger("click")
        await wrapper.get('[aria-label="Increase body font size"]').trigger("click")
        await wrapper.get('[aria-label="Active"]').trigger("click")
        await wrapper.get('.quick-panel a').trigger("click")
        await flushPromises()
        expect(router.currentRoute.value.name).toBe("settings")
        expect(wrapper.find(".quick-panel").exists()).toBe(false)
        expect(wrapper.get("input.body-size").element).toHaveProperty("value", "17")
        await wrapper.get("input.body-size").setValue("20")
        await icon().trigger("click")
        expect(wrapper.get(".quick-panel").text()).toContain("Body: 20px")
        expect(settings.bodyFontSize).toBe(20)
        await router.push("/containers/alpha-1")
        await flushPromises()
        expect(wrapper.find(".quick-panel").exists()).toBe(false)
        await icon().trigger("click")
        expect(wrapper.find(".view-heading").exists()).toBe(false)
        await router.push("/repos/alpha/sessions/claude/session-1")
        await flushPromises()
        await icon().trigger("click")
        expect(wrapper.find('[aria-label="Show thinking"]').exists()).toBe(true)
    })

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

        async function setupTab(modes: string[], path = "/login") {
            vi.useFakeTimers()
            const fetchMock = vi.fn(async () => Response.json({mode: modes.shift() ?? "dashboard"}))
            vi.stubGlobal("fetch", fetchMock)
            const {dashboard, sockets} = harness()
            const router = createAppRouter(createMemoryHistory())
            await router.push(path)
            const wrapper = mount(App, {global: {plugins: [router], provide: {[DASHBOARD]: dashboard}}})
            await flushPromises()
            return {router, wrapper, fetchMock, sockets}
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
        })

        it("leaves setup for the front page when a session token is stored", async () => {
            setToken("tok")
            const {router, wrapper} = await setupTab(["setup", "setup"])
            takeResumePath()
            expect(router.currentRoute.value.name).toBe("setup")

            await vi.advanceTimersByTimeAsync(2_000)
            await flushPromises()

            expect(router.currentRoute.value.name).toBe("front")
            wrapper.unmount()
        })

        it("returns a signed-in tab to the route it was sent to setup from", async () => {
            setToken("tok")
            const {router, wrapper, sockets} = await setupTab(["dashboard", "setup"], "/repos/alpha")
            expect(router.currentRoute.value.name).toBe("repo")
            sockets[0].handlers.onSetupMode?.()
            await flushPromises()
            expect(router.currentRoute.value.name).toBe("setup")

            await vi.advanceTimersByTimeAsync(2_000)
            await flushPromises()

            expect(router.currentRoute.value.fullPath).toBe("/repos/alpha")
            wrapper.unmount()
        })

        it("does not reopen a device link that was open during setup", async () => {
            setToken("tok")
            const {router, wrapper} = await setupTab(["setup", "setup"], "/login?link=x")
            expect(router.currentRoute.value.name).toBe("setup")

            await vi.advanceTimersByTimeAsync(2_000)
            await flushPromises()

            expect(router.currentRoute.value.fullPath).toBe("/")
            wrapper.unmount()
        })

        it("does not resume an old route after setup was left by hand", async () => {
            setToken("tok")
            const {router, wrapper, sockets} = await setupTab(["dashboard", "setup"], "/repos/alpha")
            sockets[0].handlers.onSetupMode?.()
            await flushPromises()
            expect(router.currentRoute.value.name).toBe("setup")
            // Left by hand (Back / typed URL) while the server is in dashboard mode.
            await router.push("/login")
            await flushPromises()
            expect(router.currentRoute.value.name).toBe("login")

            // A later setup episode, entered from the login poll.
            vi.stubGlobal("fetch", vi.fn(async () => Response.json({mode: "setup"})))
            await vi.advanceTimersByTimeAsync(2_000)
            await flushPromises()
            expect(router.currentRoute.value.name).toBe("setup")
            vi.stubGlobal("fetch", vi.fn(async () => Response.json({mode: "dashboard"})))
            await vi.advanceTimersByTimeAsync(2_000)
            await flushPromises()

            expect(router.currentRoute.value.fullPath).toBe("/")
            wrapper.unmount()
        })

        it("does not poll the mode on a signed-in page", async () => {
            setToken("tok")
            const {wrapper, fetchMock} = await setupTab(["dashboard"], "/repos/alpha")
            const modeCalls = () =>
                (fetchMock.mock.calls as unknown[][]).filter(([url]) => String(url).includes("/api/mode")).length
            const before = modeCalls()

            await vi.advanceTimersByTimeAsync(6_000)
            document.dispatchEvent(new Event("visibilitychange"))
            window.dispatchEvent(new Event("online"))
            await flushPromises()

            expect(modeCalls()).toBe(before)
            wrapper.unmount()
        })
    })
})
