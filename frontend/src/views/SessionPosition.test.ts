import {flushPromises, mount} from "@vue/test-utils"
import {afterEach, describe, expect, it, vi} from "vitest"
import {createMemoryHistory} from "vue-router"
import {setToken} from "../api"
import {readPosition, writePosition} from "../readingPosition"
import {createAppRouter} from "../router"
import {updateSettings} from "../settings"
import {withDashboardMode} from "../test-http"
import SessionView from "./SessionView.vue"

const key = ["alpha", "claude", "s-1"]
const transcript = {offset: 0, session_id: "s-1", agent: "claude", title: "Test", models: [], entries: [
    {kind: "user", id: "u1", html: "one", meta: false},
    {kind: "assistant", id: "a1", html: "two"},
]}

async function open(path = "/repos/alpha/sessions/claude/s-1") {
    setToken("token")
    vi.stubGlobal("fetch", withDashboardMode(vi.fn().mockResolvedValue(new Response(JSON.stringify(transcript), {status: 200}))))
    const router = createAppRouter(createMemoryHistory())
    await router.push(path)
    await router.isReady()
    const wrapper = mount(SessionView, {global: {plugins: [router]}, attachTo: document.body})
    await flushPromises()
    return wrapper
}

afterEach(() => {
    localStorage.clear()
    updateSettings({displayProfile: "normal", followNewMessages: true, einkFollow: false})
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    document.body.innerHTML = ""
})

describe("remembered session position", () => {
    it.each([
        ["entry", {entryId: "a1", atEnd: false}, true, "entry-a1"],
        ["end without follow", {entryId: "u1", atEnd: true}, false, "entry-u1"],
        ["missing entry", {entryId: "missing", atEnd: false}, true, null],
    ])("restores %s", async (_name, position, follow, expected) => {
        updateSettings({followNewMessages: follow})
        writePosition(localStorage, key, position)
        const scroll = vi.fn()
        Element.prototype.scrollIntoView = scroll
        const to = vi.spyOn(window, "scrollTo").mockImplementation(() => {})
        const wrapper = await open()
        if (expected) expect((scroll.mock.instances[0] as Element | undefined)?.id).toBe(expected)
        else expect(to).toHaveBeenCalledWith(0, 0)
        wrapper.unmount()
    })

    it("saves the first entry below the readable top on page hide", async () => {
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            if (this.id === "entry-u1") return {top: -20, bottom: 50} as DOMRect
            if (this.id === "entry-a1") return {top: 150, bottom: 500} as DOMRect
            if (this.classList.contains("transcript")) return {top: -20, bottom: 2000} as DOMRect
            return {top: 0, bottom: 100} as DOMRect
        })
        const wrapper = await open()
        window.dispatchEvent(new Event("pagehide"))
        expect(readPosition(localStorage, key)).toEqual({entryId: "a1", atEnd: false})
        wrapper.unmount()
    })

    it("opens at the end when follow is enabled", async () => {
        writePosition(localStorage, key, {entryId: "u1", atEnd: true})
        const to = vi.spyOn(window, "scrollTo").mockImplementation(() => {})
        const wrapper = await open()
        expect(to).toHaveBeenCalled()
        wrapper.unmount()
    })

    it("prefers an explicit deep link over the saved entry", async () => {
        writePosition(localStorage, key, {entryId: "u1", atEnd: false})
        const scroll = vi.fn()
        Element.prototype.scrollIntoView = scroll
        const wrapper = await open("/repos/alpha/sessions/claude/s-1#entry-a1")
        expect((scroll.mock.instances[0] as Element | undefined)?.id).toBe("entry-a1")
        wrapper.unmount()
    })
})
