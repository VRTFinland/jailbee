import {flushPromises, mount} from "@vue/test-utils"
import {afterEach, describe, expect, it, vi} from "vitest"
import {createMemoryHistory} from "vue-router"
import {setToken} from "../api"
import {createAppRouter} from "../router"
import {updateSettings} from "../settings"
import {withDashboardMode} from "../test-http"
import SessionView from "./SessionView.vue"

const originalScrollIntoView = Object.getOwnPropertyDescriptor(Element.prototype, "scrollIntoView")
const transcript = {offset: 0, session_id: "s-1", agent: "claude", title: "Test", models: [], entries: [
    {kind: "user", id: "u1", html: "one", meta: false},
    {kind: "assistant", id: "a1", html: "two"},
]}

async function open(path = "/repos/alpha/sessions/claude/s-1") {
    setToken("token")
    vi.stubGlobal("fetch", withDashboardMode(vi.fn(async () => new Response(JSON.stringify(transcript), {status: 200}))))
    const router = createAppRouter(createMemoryHistory())
    await router.push(path)
    await router.isReady()
    const wrapper = mount(SessionView, {global: {plugins: [router]}, attachTo: document.body})
    await flushPromises()
    return {wrapper, router}
}

afterEach(() => {
    localStorage.clear()
    updateSettings({displayProfile: "normal", followNewMessages: true, einkFollow: false})
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    if (originalScrollIntoView) Object.defineProperty(Element.prototype, "scrollIntoView", originalScrollIntoView)
    else Reflect.deleteProperty(Element.prototype, "scrollIntoView")
    document.body.innerHTML = ""
})

describe("initial session position", () => {
    it.each([true, false])("opens at the end regardless of saved position with follow %s", async follow => {
        updateSettings({followNewMessages: follow})
        vi.spyOn(window, "innerHeight", "get").mockReturnValue(800)
        vi.spyOn(window, "scrollY", "get").mockReturnValue(300)
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            if (this.classList.contains("transcript")) return {top: -300, bottom: 2100} as DOMRect
            return {top: 0, bottom: 100} as DOMRect
        })
        localStorage.setItem('jailbee.readingPositions', JSON.stringify([{key: '["alpha","claude","s-1"]', position: {entryId: 'u1', atEnd: false}}]))
        const to = vi.spyOn(window, "scrollTo").mockImplementation(() => {})
        const {wrapper} = await open()
        expect(to).toHaveBeenLastCalledWith(0, 1600)
        wrapper.unmount()
    })

    it("prefers an explicit deep link over the end", async () => {
        const scroll = vi.fn()
        Element.prototype.scrollIntoView = scroll
        const {wrapper} = await open("/repos/alpha/sessions/claude/s-1#entry-a1")
        expect((scroll.mock.instances[0] as Element | undefined)?.id).toBe("entry-a1")
        wrapper.unmount()
    })

    it("positions each newly navigated session at the end", async () => {
        const to = vi.spyOn(window, "scrollTo").mockImplementation(() => {})
        const {wrapper, router} = await open()
        to.mockClear()
        await router.push("/repos/alpha/sessions/claude/s-2")
        await flushPromises()
        expect(to).toHaveBeenCalled()
        wrapper.unmount()
    })
})
