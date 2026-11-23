import {describe, expect, it, vi} from "vitest"
import {getToken, loginWithPin} from "./api"

describe("login rate-limit message", () => {
    it.each([
        ["1", "1 second"],
        ["59", "59 seconds"],
        ["60", "1 minute"],
        ["61", "2 minutes"],
        ["1800", "30 minutes"],
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
