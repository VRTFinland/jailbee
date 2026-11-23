import {flushPromises, mount} from "@vue/test-utils"
import {describe, expect, it, vi} from "vitest"
import {createMemoryHistory} from "vue-router"
import {getToken} from "../api"
import {createAppRouter} from "../router"
import LoginView from "./LoginView.vue"

function jsonResponse(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: {"Content-Type": "application/json"},
    })
}

type Route = {status: number; body: unknown}

/** A fetch stub answering by URL; `/api/auth/methods` defaults to PIN only. */
function routedFetch(routes: Record<string, Route>) {
    const all: Record<string, Route> = {
        "/api/auth/methods": {status: 200, body: {methods: ["pin"]}},
        ...routes,
    }
    return vi.fn(async (url: string, _init?: RequestInit) => {
        const route = all[url]
        if (!route) throw new Error(`unexpected fetch ${url}`)
        return jsonResponse(route.status, route.body)
    })
}

function authCalls(fetchMock: ReturnType<typeof routedFetch>) {
    return fetchMock.mock.calls
        .filter(([url]) => url === "/api/auth")
        .map(([, init]) => JSON.parse((init as RequestInit).body as string))
}

async function mountLogin() {
    const router = createAppRouter(createMemoryHistory())
    await router.push("/login")
    await router.isReady()
    const wrapper = mount(LoginView, {global: {plugins: [router]}})
    await flushPromises()
    return {wrapper, router}
}

async function submit(wrapper: Awaited<ReturnType<typeof mountLogin>>["wrapper"], pin: string) {
    await wrapper.get("input[name=pin]").setValue(pin)
    await wrapper.get("form").trigger("submit")
    await flushPromises()
}

describe("LoginView", () => {
    it("logs in with the PIN alone, then opens the front page", async () => {
        const fetchMock = routedFetch({"/api/auth": {status: 200, body: {token: "tok-1"}}})
        vi.stubGlobal("fetch", fetchMock)
        const {wrapper, router} = await mountLogin()

        await submit(wrapper, "123456")

        expect(authCalls(fetchMock)).toEqual([{method: "pin", pin: "123456"}])
        expect(getToken()).toBe("tok-1")
        expect(router.currentRoute.value.path).toBe("/")
    })

    it("says the PIN was wrong, clears it and stays on the login page", async () => {
        vi.stubGlobal("fetch", routedFetch({"/api/auth": {status: 401, body: {detail: "no"}}}))
        const {wrapper, router} = await mountLogin()

        await submit(wrapper, "000000")

        expect(wrapper.text()).toContain("new one")
        expect((wrapper.get("input[name=pin]").element as HTMLInputElement).value).toBe("")
        expect(getToken()).toBeNull()
        expect(router.currentRoute.value.path).toBe("/login")
    })

    it("does not submit until the PIN has six digits", async () => {
        const fetchMock = routedFetch({})
        vi.stubGlobal("fetch", fetchMock)
        const {wrapper} = await mountLogin()

        await submit(wrapper, "12345")

        expect(authCalls(fetchMock)).toEqual([])
        expect(wrapper.get("button[type=submit]").attributes("disabled")).toBeDefined()
    })

    it("has no device-name field and sends none", async () => {
        const fetchMock = routedFetch({"/api/auth": {status: 200, body: {token: "t"}}})
        vi.stubGlobal("fetch", fetchMock)
        const {wrapper} = await mountLogin()

        expect(wrapper.find("input[name=device]").exists()).toBe(false)
        expect(wrapper.text()).not.toContain("This device")
        await submit(wrapper, "123456")
        expect(Object.keys(authCalls(fetchMock)[0])).not.toContain("device_name")
    })

    it("offers an authenticator code once a TOTP key exists and logs in with it", async () => {
        const fetchMock = routedFetch({
            "/api/auth/methods": {status: 200, body: {methods: ["pin", "totp"]}},
            "/api/auth": {status: 200, body: {token: "tok-2"}},
        })
        vi.stubGlobal("fetch", fetchMock)
        const {wrapper, router} = await mountLogin()

        await wrapper.get("button[data-method=totp]").trigger("click")
        await wrapper.get("input[name=code]").setValue("654321")
        await wrapper.get("form").trigger("submit")
        await flushPromises()

        expect(authCalls(fetchMock)).toEqual([{method: "totp", code: "654321"}])
        expect(getToken()).toBe("tok-2")
        expect(router.currentRoute.value.path).toBe("/")
    })

    it("shows only the code form when PIN login is off", async () => {
        vi.stubGlobal(
            "fetch",
            routedFetch({"/api/auth/methods": {status: 200, body: {methods: ["totp"]}}}),
        )
        const {wrapper} = await mountLogin()

        expect(wrapper.find("input[name=pin]").exists()).toBe(false)
        expect(wrapper.find("input[name=code]").exists()).toBe(true)
        expect(wrapper.find("button[data-method]").exists()).toBe(false)
    })

    it("says a refused code was wrong or already used and clears it", async () => {
        vi.stubGlobal(
            "fetch",
            routedFetch({
                "/api/auth/methods": {status: 200, body: {methods: ["totp"]}},
                "/api/auth": {status: 401, body: {detail: "no"}},
            }),
        )
        const {wrapper} = await mountLogin()

        await wrapper.get("input[name=code]").setValue("111111")
        await wrapper.get("form").trigger("submit")
        await flushPromises()

        expect(wrapper.text()).toContain("already used")
        expect((wrapper.get("input[name=code]").element as HTMLInputElement).value).toBe("")
        expect(getToken()).toBeNull()
    })
})
