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

    it("checks the live server again when following a route", async () => {
        setToken("session")
        let mode = "dashboard"
        vi.stubGlobal("fetch", vi.fn(async () => Response.json({mode})))
        const router = createAppRouter(createMemoryHistory())
        await router.push("/repos/alpha")
        expect(router.currentRoute.value.name).toBe("repo")

        mode = "setup"
        await router.push("/settings")

        expect(router.currentRoute.value.name).toBe("setup")
    })
})
