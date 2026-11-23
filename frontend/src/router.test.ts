import {describe, expect, it, vi} from "vitest"
import {createMemoryHistory} from "vue-router"
import {setToken} from "./api"
import {createAppRouter} from "./router"

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

    it("keeps every route on setup once setup mode was seen", async () => {
        setToken("session")
        const fetchMock = vi.fn(async () => Response.json({mode: "setup"}))
        vi.stubGlobal("fetch", fetchMock)
        const router = createAppRouter(createMemoryHistory())
        await router.push("/")

        await router.push("/settings")

        expect(router.currentRoute.value.name).toBe("setup")
        expect(fetchMock).toHaveBeenCalledTimes(1)
    })
})
