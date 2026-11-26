import {describe, expect, it, vi} from "vitest"
import {useAgentChat} from "./useAgentChat"

describe("agent chat", () => {
    it("accepts dialog delivery and preserves its warning without waiting for an echo", async () => {
        const warning = "The text went to an open terminal dialog; check it there."
        const chat = useAgentChat(() => true, async () => ({code: "delivered_with_warning", message: warning}))
        chat.draft.value = "hello"
        expect(await chat.send("hello")).toBe(true)
        expect(chat.draft.value).toBe("")
        expect(chat.awaiting.value).toBe(false)
        expect(chat.status.value).toBe(warning)
        chat.observe([{kind: "user", id: "1", html: "", text: "hello", meta: false, label: "", expandable: false, timestamp: null}])
        expect(chat.status.value).toBe(warning)
    })
    it("confirms Claude's real pasted user-turn text with expanded tabs", async () => {
        const chat = useAgentChat(() => true, async () => ({code: "delivered", message: ""}))
        // Claude Code 2.1.332 expands each pasted tab to four spaces before recording.
        const text = "  **bold**\nsecond line  \nthird\tline  "
        chat.draft.value = text
        await chat.send(text)
        chat.observe([{kind: "user", id: "737de03e-9079-4851-b4f3-2cbc0dd3ff71", timestamp: "2026-11-26T21:35:51.717Z",
            html: "", text: "  **bold**\nsecond line  \nthird    line", meta: false, label: null, expandable: false}])
        expect(chat.status.value).toBe("Delivered")
        expect(chat.awaiting.value).toBe(false)
        chat.reset()
    })
    it("unlocks a slash command when no ordinary transcript entry arrives", async () => {
        vi.useFakeTimers()
        const chat = useAgentChat(() => true, async () => ({code: "delivered", message: ""}))
        await chat.send("/compact")
        expect(chat.awaiting.value).toBe(true)
        await vi.advanceTimersByTimeAsync(30000)
        expect(chat.awaiting.value).toBe(false)
        expect(chat.status.value).toBe("Pasted; transcript confirmation unavailable")
        vi.useRealTimers()
    })
    it("matches trimmed and sanitized transcript text and gates concurrent sends", async () => {
        let resolve!: (value: {code: "delivered", message: string}) => void
        const deliver = vi.fn(() => new Promise<{code: "delivered", message: string}>(r => { resolve = r }))
        const chat = useAgentChat(() => true, deliver)
        chat.draft.value = "  hello\x1b[ world  "
        const first = chat.send(chat.draft.value)
        expect(await chat.send("other")).toBe(false)
        chat.observe([{kind: "user", id: "1", html: "", text: "hello[ world", meta: false, label: "", expandable: false, timestamp: null}])
        resolve({code: "delivered", message: ""})
        expect(await first).toBe(true)
        expect(chat.busy.value).toBe(false)
        expect(chat.draft.value).toBe("")
        expect(deliver).toHaveBeenCalledTimes(1)
    })
    it("keeps an in-flight delivery authoritative across same-session resets", async () => {
        let resolve!: (value: {code: "delivered", message: string}) => void
        const chat = useAgentChat(() => true, () => new Promise(r => { resolve = r }))
        chat.draft.value = "keep"
        const sending = chat.send("keep")
        chat.reset()
        expect(chat.sending.value).toBe(true)
        expect(await chat.send("keep")).toBe(false)
        resolve({code: "delivered", message: ""})
        expect(await sending).toBe(true)
        expect(chat.draft.value).toBe("")
        expect(chat.busy.value).toBe(false)
        expect(chat.status.value).toBe("Pasted; transcript confirmation unavailable")
    })
    it("ignores replies from a previous session and retains drafts on refusal", async () => {
        let resolve!: (value: {code: "delivered", message: string}) => void
        const chat = useAgentChat(() => true, () => new Promise(r => { resolve = r }))
        chat.draft.value = "keep"
        const sending = chat.send("keep")
        chat.reset({sessionChanged: true})
        resolve({code: "delivered", message: ""})
        expect(await sending).toBe(false)
        expect(chat.draft.value).toBe("keep")
        expect(chat.busy.value).toBe(false)
        const refused = useAgentChat(() => true, async () => ({code: "no_pane", message: "No pane"}))
        refused.draft.value = "keep"
        expect(await refused.send("keep")).toBe(false)
        expect(refused.draft.value).toBe("keep")
        expect(refused.error.value).toBe("No pane")
    })
})
