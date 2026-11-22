// The setup-mode page that registers a security key (#109 ticket 12).
import {flushPromises, mount} from "@vue/test-utils"
import {describe, expect, it, vi} from "vitest"
import {createMemoryHistory} from "vue-router"
import {createAppRouter} from "../router"
import SetupView from "./SetupView.vue"

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

const bytes = (...b: number[]) => new Uint8Array(b).buffer

const BEGIN = {
    challenge_id: "c-9",
    options: {
        rp: {id: "jb.example.com", name: "JailBee"},
        user: {id: "AQ", name: "jailbee", displayName: "JailBee"},
        challenge: "Ag",
        pubKeyCredParams: [{type: "public-key", alg: -7}],
        excludeCredentials: [],
    },
}

async function mountSetup() {
    const router = createAppRouter(createMemoryHistory())
    await router.push("/setup")
    await router.isReady()
    const wrapper = mount(SetupView, {global: {plugins: [router]}})
    await flushPromises()
    return {wrapper, router}
}

async function register(wrapper: Awaited<ReturnType<typeof mountSetup>>["wrapper"]) {
    await wrapper.get("input[name=pin]").setValue("123456")
    await wrapper.get("input[name=key-name]").setValue("YubiKey 5")
    await wrapper.get("form").trigger("submit")
    await flushPromises()
}

describe("SetupView", () => {
    it("is reachable without a login", async () => {
        const router = createAppRouter(createMemoryHistory())
        await router.push("/setup")

        expect(router.currentRoute.value.name).toBe("setup")
    })

    it("registers a key with the setup PIN and names it", async () => {
        const fetchMock = routedFetch({
            "/api/setup/fido2/begin": {status: 200, body: BEGIN},
            "/api/setup/fido2/complete": {status: 200, body: {id: 3, name: "YubiKey 5"}},
        })
        vi.stubGlobal("fetch", fetchMock)
        const create = vi.fn(async (_opts: CredentialCreationOptions) => ({
            id: "Bw",
            rawId: bytes(7),
            type: "public-key",
            response: {clientDataJSON: bytes(8), attestationObject: bytes(9)},
        }))
        vi.stubGlobal("navigator", {credentials: {create}})
        const {wrapper} = await mountSetup()

        await register(wrapper)

        const bodies = Object.fromEntries(
            fetchMock.mock.calls.map(([url, init]) => [url, JSON.parse((init as RequestInit).body as string)]),
        )
        expect(bodies["/api/setup/fido2/begin"]).toEqual({pin: "123456"})
        const publicKey = create.mock.calls[0][0].publicKey!
        expect(new Uint8Array(publicKey.challenge as ArrayBuffer)).toEqual(new Uint8Array([2]))
        expect(new Uint8Array(publicKey.user.id as ArrayBuffer)).toEqual(new Uint8Array([1]))
        expect(bodies["/api/setup/fido2/complete"]).toEqual({
            challenge_id: "c-9",
            name: "YubiKey 5",
            credential: {
                id: "Bw",
                rawId: "Bw",
                type: "public-key",
                response: {clientDataJSON: "CA", attestationObject: "CQ"},
            },
        })
        expect(wrapper.text()).toContain("Registered YubiKey 5")
    })

    it("says the PIN was wrong and asks for the new one", async () => {
        vi.stubGlobal("fetch", routedFetch({"/api/setup/fido2/begin": {status: 401, body: {detail: "x"}}}))
        const {wrapper} = await mountSetup()

        await register(wrapper)

        expect(wrapper.get("[role=alert]").text()).toContain("printed a new one")
    })

    it("tells the user to start setup mode when the server is the normal dashboard", async () => {
        vi.stubGlobal("fetch", routedFetch({"/api/setup/fido2/begin": {status: 404, body: {detail: "Not Found"}}}))
        const {wrapper} = await mountSetup()

        await register(wrapper)

        expect(wrapper.get("[role=alert]").text()).toContain("jailbee remote web auth fido2 setup")
    })
})
