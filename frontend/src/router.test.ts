import {describe, expect, it, vi} from "vitest"
import {createMemoryHistory} from "vue-router"
import {setToken} from "./api"
import {createAppRouter, takeResumePath} from "./router"

describe("server mode routing", () => {
    it.each(["/login", "/", "/repos/alpha", "/containers/alpha-1", "/settings", "/unknown"])(
        "routes %s to setup even with a stored session",
        async (path) => {
            setToken("stale-token")
            vi.stubGlobal("fetch", vi.fn(async () => Response.json({mode: "setup"})))
            const router = createAppRouter(createMemoryHistory())

            await router.push(path)

            expect(router.currentRoute.value.name).toBe("setup")
        },
    )

    it("asks the server's mode once at start-up, not on every navigation", async () => {
        setToken("session")
        const fetchMock = vi.fn(async () => Response.json({mode: "dashboard"}))
        vi.stubGlobal("fetch", fetchMock)
        const router = createAppRouter(createMemoryHistory())

        await router.push("/repos/alpha")
        await router.push("/settings")
        await router.push("/containers/alpha-1")

        expect(router.currentRoute.value.name).toBe("container")
        expect(fetchMock).toHaveBeenCalledTimes(1)
    })

    it("leaves setup on the next navigation once the server is back in dashboard mode", async () => {
        setToken("session")
        let mode = "setup"
        const fetchMock = vi.fn(async () => Response.json({mode}))
        vi.stubGlobal("fetch", fetchMock)
        const router = createAppRouter(createMemoryHistory())
        await router.push("/")
        expect(router.currentRoute.value.name).toBe("setup")

        mode = "dashboard"
        await router.push("/login")

        expect(router.currentRoute.value.name).toBe("login")
        await router.push("/settings")
        expect(router.currentRoute.value.name).toBe("settings")
    })

    it("keeps a dashboard answer across signed-in and public navigations", async () => {
        setToken("session")
        const fetchMock = vi.fn(async () => Response.json({mode: "dashboard"}))
        vi.stubGlobal("fetch", fetchMock)
        const router = createAppRouter(createMemoryHistory())

        await router.push("/")
        await router.push("/login")
        await router.push("/settings")
        await router.push("/repos/alpha")

        expect(fetchMock).toHaveBeenCalledTimes(1)
    })
})

describe("resume path", () => {
    function setupRouter() {
        vi.stubGlobal("fetch", vi.fn(async () => Response.json({mode: "setup"})))
        return createAppRouter(createMemoryHistory())
    }

    it("is remembered by the guard alone when a signed-in route is sent to setup", async () => {
        const router = setupRouter()
        await router.push("/repos/alpha?tab=x")
        expect(router.currentRoute.value.name).toBe("setup")
        expect(takeResumePath()).toBe("/repos/alpha?tab=x")
    })

    it("does not remember a public route, whatever its query", async () => {
        const router = setupRouter()
        await router.push("/login?link=secret")
        expect(takeResumePath()).toBe("/")
    })

    it("does not outlive the setup episode left by hand", async () => {
        const router = setupRouter()
        await router.push("/repos/alpha")
        vi.stubGlobal("fetch", vi.fn(async () => Response.json({mode: "dashboard"})))
        setToken("t")
        await router.push("/settings")
        expect(takeResumePath()).toBe("/")
    })
})
