import {describe, expect, it, vi} from "vitest"
import {useAgentChat} from "./useAgentChat"

describe("agent chat", () => {
    it("unlocks a slash command when no ordinary transcript entry arrives", async () => {
        vi.useFakeTimers()
        const chat = useAgentChat(() => true, async () => ({code: "delivered", message: ""}))
        await chat.send("/compact")
        expect(chat.awaiting.value).toBe(true)
        await vi.advanceTimersByTimeAsync(30000)
        expect(chat.awaiting.value).toBe(false)
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
    it("retains drafts on refusal and ignores replies after reset", async () => {
        let resolve!: (value: {code: "delivered", message: string}) => void
        const chat = useAgentChat(() => true, () => new Promise(r => { resolve = r }))
        chat.draft.value = "keep"
        const sending = chat.send("keep")
        chat.reset()
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
