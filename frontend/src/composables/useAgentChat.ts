import {computed, ref} from "vue"
import type {MessageOutcome, RenderedEntry} from "../protocol"

const WAIT_MS = 30000
// Claude's terminal editor expands pasted tabs before writing the user turn.
const normalize = (text: string) => text.replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, "").replace(/\t/g, "    ").trim()

/** One send gate shared by the composer and other session input surfaces. */
export function useAgentChat(available: () => boolean, deliver: (text: string) => Promise<MessageOutcome>) {
    const draft = ref("")
    const sending = ref(false)
    const awaiting = ref(false)
    const error = ref("")
    const status = ref("")
    let expected: string | null = null
    let matched = false
    let generation = 0
    let timer: ReturnType<typeof setTimeout> | undefined
    const busy = computed(() => sending.value || awaiting.value)
    function reset({sessionChanged = false}: {sessionChanged?: boolean} = {}) {
        // Reloads lose echo continuity, not the result of an already submitted POST.
        if (sessionChanged) generation++
        if (timer !== undefined) clearTimeout(timer)
        timer = undefined
        if (sessionChanged) sending.value = false
        awaiting.value = false
        expected = null
        matched = false
        status.value = ""
        error.value = ""
    }
    function finish(confirmed = true) {
        if (timer !== undefined) clearTimeout(timer)
        timer = undefined
        awaiting.value = false
        expected = null
        status.value = confirmed ? "Delivered" : "Pasted; transcript confirmation unavailable"
    }
    function observe(entries: RenderedEntry[]) {
        if (expected === null || !entries.some(entry => entry.kind === "user" && !entry.meta &&
            entry.text !== undefined && normalize(entry.text) === expected)) return
        matched = true
        if (!sending.value) finish()
    }
    async function send(text: string): Promise<boolean> {
        if (busy.value || !available() || !text.trim()) return false
        const current = generation
        sending.value = true
        error.value = ""
        status.value = ""
        expected = normalize(text)
        matched = false
        try {
            const result = await deliver(text)
            if (current !== generation) return false
            if (result.code !== "delivered" && result.code !== "delivered_with_warning") {
                expected = null
                error.value = result.message
                return false
            }
            if (draft.value === text) draft.value = ""
            if (result.code === "delivered_with_warning") {
                expected = null
                status.value = result.message
                return true
            }
            if (expected === null) finish(false)
            else if (matched) finish()
            else {
                awaiting.value = true
                status.value = "Waiting for transcript"
                timer = setTimeout(() => finish(false), WAIT_MS)
            }
            return true
        } catch {
            if (current === generation) {
                expected = null
                error.value = "The message could not be sent."
            }
            return false
        } finally {
            if (current === generation) sending.value = false
        }
    }
    return {draft, sending, awaiting, busy, error, status, send, observe, reset}
}
