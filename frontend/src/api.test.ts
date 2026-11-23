import {describe, expect, it, vi} from "vitest"
import {getToken, loginWithLink, loginWithPin, loginWithTotp} from "./api"

const unavailable = "The server is not accepting logins right now (is it in setup mode?)"

describe("login failure classification", () => {
    it.each([400, 403, 404, 405, 422, 500, 502, 503])(
        "does not blame the PIN when the endpoint returns %s",
        async (status) => {
            vi.stubGlobal("fetch", vi.fn(async () => new Response(null, {status})))

            expect(await loginWithPin("123456", "tablet")).toEqual({ok: false, error: unavailable})
            expect(getToken()).toBeNull()
        },
    )

    it.each([
        [() => loginWithPin("123456", "tablet"), "Wrong or used PIN. The server printed a new one."],
        [() => loginWithTotp("123456", "tablet"), "Wrong or already used code. Wait for the next one and try again."],
        [() => loginWithLink("expired"), "This login link was already used or has expired. Run `jailbee remote web open` again."],
    ])("keeps credential-specific errors for 401", async (login, error) => {
        vi.stubGlobal("fetch", vi.fn(async () => new Response(null, {status: 401})))

        expect(await login()).toEqual({ok: false, error})
    })
})

describe("login rate-limit message", () => {
    it.each([
        ["1", "1 second"],
        ["59", "59 seconds"],
        ["1800", "1800 seconds"],
        [null, "a while"],
        ["invalid", "a while"],
        ["0", "a while"],
    ])("shows Retry-After %s as %s", async (retryAfter, wait) => {
        const headers = new Headers()
        if (retryAfter !== null) headers.set("Retry-After", retryAfter)
        vi.stubGlobal("fetch", vi.fn(async () => new Response(null, {status: 429, headers})))

        const result = await loginWithPin("123456", "shared tablet")

        expect(result).toEqual({ok: false, error: `Too many attempts. Wait ${wait} and try again.`})
        expect(getToken()).toBeNull()
    })
})
