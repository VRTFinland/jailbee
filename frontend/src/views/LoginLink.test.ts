import {flushPromises, mount} from "@vue/test-utils"
import {describe, expect, it, vi} from "vitest"
import {createMemoryHistory} from "vue-router"
import {getToken} from "../api"
import {createAppRouter} from "../router"
import LoginView from "./LoginView.vue"

function json(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: {"Content-Type": "application/json"},
    })
}

function stubFetch(auth: {status: number; body: unknown}, methods: unknown = {methods: ["pin"]}) {
    const mock = vi.fn(async (url: string, _init?: RequestInit) => {
        if (url === "/api/auth/methods") return json(200, methods)
        if (url === "/api/auth") return json(auth.status, auth.body)
        throw new Error(`unexpected fetch ${url}`)
    })
    vi.stubGlobal("fetch", mock)
    return mock
}

async function openLoginLink(token: string) {
    const router = createAppRouter(createMemoryHistory())
    await router.push(`/login?link=${token}`)
    await router.isReady()
    const wrapper = mount(LoginView, {global: {plugins: [router]}})
    await flushPromises()
    return {wrapper, router}
}

describe("login link", () => {
    it("exchanges the token for a session and drops it from the address", async () => {
        const fetchMock = stubFetch({status: 200, body: {token: "tok-9"}})

        const {router} = await openLoginLink("one-time")

        const bodies = fetchMock.mock.calls
            .filter(([url]) => url === "/api/auth")
            .map(([, init]) => JSON.parse((init as RequestInit).body as string))
        expect(bodies).toHaveLength(1)
        expect(bodies[0]).toMatchObject({method: "link", token: "one-time"})
        expect(bodies[0].device_name.length).toBeGreaterThan(0)
        expect(getToken()).toBe("tok-9")
        expect(router.currentRoute.value.path).toBe("/")
        expect(router.currentRoute.value.fullPath).not.toContain("one-time")
    })

    it("says an expired or used link was refused and leaves the login form", async () => {
        stubFetch({status: 401, body: {detail: "no"}})

        const {wrapper, router} = await openLoginLink("stale")

        expect(wrapper.text()).toContain("jailbee remote web open")
        expect(getToken()).toBeNull()
        expect(router.currentRoute.value.path).toBe("/login")
        expect(router.currentRoute.value.fullPath).not.toContain("stale")
    })
})

describe("auth disabled", () => {
    it("lets the router skip the login view", async () => {
        stubFetch({status: 500, body: {}}, {methods: [], auth_disabled: true})
        const router = createAppRouter(createMemoryHistory())

        await router.push("/")
        await router.isReady()

        expect(router.currentRoute.value.path).toBe("/")
    })

    it("still sends an unauthenticated visitor to the login view when auth is on", async () => {
        stubFetch({status: 500, body: {}}, {methods: ["pin"], auth_disabled: false})
        const router = createAppRouter(createMemoryHistory())

        await router.push("/")
        await router.isReady()

        expect(router.currentRoute.value.path).toBe("/login")
    })
})
