// The security-key (FIDO2) button on the login view (#109 ticket 12).
import {flushPromises, mount} from "@vue/test-utils"
import {describe, expect, it, vi} from "vitest"
import {createMemoryHistory} from "vue-router"
import {getToken} from "../api"
import {createAppRouter} from "../router"
import LoginView from "./LoginView.vue"

type Route = {status: number; body: unknown}

function routedFetch(routes: Record<string, Route>) {
    return vi.fn(async (url: string, _init?: RequestInit) => {
        const route = routes[url]
        if (!route) throw new Error(`unexpected fetch ${url}`)
        return new Response(JSON.stringify(route.body), {
            status: route.status,
            headers: {"Content-Type": "application/json"},
        })
    })
}

async function mountLogin() {
    const router = createAppRouter(createMemoryHistory())
    await router.push("/login")
    await router.isReady()
    const wrapper = mount(LoginView, {global: {plugins: [router]}})
    await flushPromises()
    return {wrapper, router}
}

const bytes = (...b: number[]) => new Uint8Array(b).buffer

/** What `navigator.credentials.get` resolves to, enough for the JSON encoding. */
function fakeAssertion() {
    return {
        id: "AQI",
        rawId: bytes(1, 2),
        type: "public-key",
        response: {
            clientDataJSON: bytes(3),
            authenticatorData: bytes(4),
            signature: bytes(5),
            userHandle: null,
        },
    }
}

describe("LoginView security key", () => {
    it("offers no security-key button while no key is registered", async () => {
        vi.stubGlobal("fetch", routedFetch({"/api/auth/methods": {status: 200, body: {methods: ["pin"]}}}))
        const {wrapper} = await mountLogin()

        expect(wrapper.find("[data-test=security-key]").exists()).toBe(false)
    })

    it("offers the button, not a tab, once the server advertises fido2", async () => {
        vi.stubGlobal(
            "fetch",
            routedFetch({"/api/auth/methods": {status: 200, body: {methods: ["pin", "fido2"]}}}),
        )
        const {wrapper} = await mountLogin()

        expect(wrapper.find("[data-test=security-key]").exists()).toBe(true)
        expect(wrapper.find("[role=tab][data-method=fido2]").exists()).toBe(false)
        expect(wrapper.find("input[name=pin]").exists()).toBe(true)
    })

    it("signs the server's challenge with the key and logs in", async () => {
        const fetchMock = routedFetch({
            "/api/auth/methods": {status: 200, body: {methods: ["fido2"]}},
            "/api/auth/fido2/begin": {
                status: 200,
                body: {
                    challenge_id: "c-1",
                    options: {challenge: "AAE", rpId: "jb.example.com", allowCredentials: [{type: "public-key", id: "AQI"}]},
                },
            },
            "/api/auth": {status: 200, body: {token: "tok-key"}},
        })
        vi.stubGlobal("fetch", fetchMock)
        const get = vi.fn(async (_opts: CredentialRequestOptions) => fakeAssertion())
        vi.stubGlobal("navigator", {credentials: {get}})
        const {wrapper, router} = await mountLogin()

        expect(wrapper.find("input[name=device]").exists()).toBe(false)
        await wrapper.get("[data-test=security-key]").trigger("click")
        await flushPromises()

        const publicKey = get.mock.calls[0][0].publicKey!
        expect(new Uint8Array(publicKey.challenge as ArrayBuffer)).toEqual(new Uint8Array([0, 1]))
        expect(publicKey.rpId).toBe("jb.example.com")
        const [, init] = fetchMock.mock.calls.find(([url]) => url === "/api/auth")!
        expect(JSON.parse((init as RequestInit).body as string)).toEqual({
            method: "fido2",
            challenge_id: "c-1",
            credential: {
                id: "AQI",
                rawId: "AQI",
                type: "public-key",
                response: {clientDataJSON: "Aw", authenticatorData: "BA", signature: "BQ", userHandle: null},
            },
        })
        expect(getToken()).toBe("tok-key")
        expect(router.currentRoute.value.name).toBe("front")
    })
})
