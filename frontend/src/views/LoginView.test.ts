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

async function mountLogin() {
    const router = createAppRouter(createMemoryHistory())
    await router.push("/login")
    await router.isReady()
    const wrapper = mount(LoginView, {global: {plugins: [router]}})
    return {wrapper, router}
}

async function submit(wrapper: Awaited<ReturnType<typeof mountLogin>>["wrapper"], pin: string, device: string) {
    await wrapper.get("input[name=device]").setValue(device)
    await wrapper.get("input[name=pin]").setValue(pin)
    await wrapper.get("form").trigger("submit")
    await flushPromises()
}

describe("LoginView", () => {
    it("logs in with the PIN and a device name, then opens the front page", async () => {
        const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, {token: "tok-1"}))
        vi.stubGlobal("fetch", fetchMock)
        const {wrapper, router} = await mountLogin()

        await submit(wrapper, "123456", "Boox")

        expect(fetchMock).toHaveBeenCalledOnce()
        const [url, init] = fetchMock.mock.calls[0]
        expect(url).toBe("/api/auth")
        expect(JSON.parse(init.body)).toEqual({method: "pin", pin: "123456", device_name: "Boox"})
        expect(getToken()).toBe("tok-1")
        expect(router.currentRoute.value.path).toBe("/")
    })

    it("says the PIN was wrong, clears it and stays on the login page", async () => {
        vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(401, {detail: "no"})))
        const {wrapper, router} = await mountLogin()

        await submit(wrapper, "000000", "Boox")

        expect(wrapper.text()).toContain("new one")
        expect((wrapper.get("input[name=pin]").element as HTMLInputElement).value).toBe("")
        expect(getToken()).toBeNull()
        expect(router.currentRoute.value.path).toBe("/login")
    })

    it("does not submit until the PIN has six digits and a device is named", async () => {
        const fetchMock = vi.fn()
        vi.stubGlobal("fetch", fetchMock)
        const {wrapper} = await mountLogin()

        await submit(wrapper, "12345", "Boox")
        await submit(wrapper, "123456", "   ")

        expect(fetchMock).not.toHaveBeenCalled()
        expect(wrapper.get("button[type=submit]").attributes("disabled")).toBeDefined()
    })

    it("remembers the device name of the last login", async () => {
        vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(200, {token: "t"})))
        const first = await mountLogin()
        await submit(first.wrapper, "123456", "Kitchen tablet")

        const second = await mountLogin()

        expect((second.wrapper.get("input[name=device]").element as HTMLInputElement).value).toBe(
            "Kitchen tablet",
        )
    })
})
