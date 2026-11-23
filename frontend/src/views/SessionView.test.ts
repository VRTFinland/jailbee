import {enableAutoUnmount, flushPromises, mount} from "@vue/test-utils"
import {afterEach, describe, expect, it, vi} from "vitest"
import {createMemoryHistory} from "vue-router"
import {setToken} from "../api"
import type {TranscriptView} from "../protocol"
import {createAppRouter} from "../router"
import {reloadSettings, updateSettings} from "../settings"
import {removeItem} from "../storage"
import SessionView from "./SessionView.vue"

enableAutoUnmount(afterEach)
const originalScrollIntoView = Object.getOwnPropertyDescriptor(Element.prototype, "scrollIntoView")

function jsonResponse(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: {"Content-Type": "application/json"},
    })
}

const transcript: TranscriptView = {
    offset: 0,
    session_id: "s-1",
    agent: "claude",
    title: "Fix the login bug",
    last_activity: "2026-11-01T10:05:00Z",
    models: ["claude-opus-5-5"],
    entries: [
        {kind: "user", id: "u1", timestamp: null, html: "<p>Fix it</p>", meta: false},
        {kind: "thinking", id: "t1", timestamp: null, html: "<p>Hmm, auth</p>", model: "m"},
        {
            kind: "assistant",
            id: "a1",
            timestamp: null,
            html: "<h2>Plan</h2>",
            model: "claude-opus-5-5",
        },
        {
            kind: "tool_call",
            id: "c1",
            timestamp: null,
            call_id: "toolu_1",
            name: "Bash",
            summary: "Bash: pytest",
            input_html: "<pre>{}</pre>",
            model: "claude-opus-5-5",
        },
        {
            kind: "tool_result",
            id: "r1",
            timestamp: null,
            call_id: "toolu_1",
            html: "<pre>ok</pre>",
            truncated: false,
            full_length: 2,
            is_error: false,
        },
        {
            kind: "user",
            id: "u2",
            timestamp: null,
            html: '<pre class="meta">&lt;command-name&gt;/clear&lt;/command-name&gt;</pre>',
            meta: true,
        },
        {kind: "assistant", id: "a2", timestamp: null, html: "<p>Done.</p>", model: "claude-sonnet-5"},
    ],
}

async function mountSession(fetchMock: ReturnType<typeof vi.fn>) {
    setToken("tok")
    vi.stubGlobal("fetch", fetchMock)
    const router = createAppRouter(createMemoryHistory())
    await router.push("/repos/alpha/sessions/claude/s-1")
    await router.isReady()
    const wrapper = mount(SessionView, {global: {plugins: [router]}, attachTo: document.body})
    await flushPromises()
    return {wrapper, router}
}

afterEach(() => {
    removeItem("jailbee.showThinking")
    updateSettings({displayProfile: "normal", showThinking: false})
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    if (originalScrollIntoView) Object.defineProperty(Element.prototype, "scrollIntoView", originalScrollIntoView)
    else Reflect.deleteProperty(Element.prototype, "scrollIntoView")
    document.body.innerHTML = ""
})

describe("SessionView", () => {
    it("fetches the session and renders prompts and answers with their models", async () => {
        const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, transcript))
        const {wrapper} = await mountSession(fetchMock)

        expect(fetchMock.mock.calls[0][0]).toBe("/api/repos/alpha/sessions/claude/s-1")
        expect(wrapper.get("h1").text()).toBe("Fix the login bug")
        expect(wrapper.get("h1").attributes("title")).toBe("Fix the login bug")
        expect(wrapper.get("h1").attributes("aria-label")).toBe("Fix the login bug")
        const answers = wrapper.findAll(".entry.assistant")
        expect(answers).toHaveLength(2)
        expect(answers[0].html()).toContain("<h2>Plan</h2>")
        expect(answers[0].get(".model").text()).toBe("claude-opus-5-5")
        expect(answers[1].get(".model").text()).toBe("claude-sonnet-5")
        expect(wrapper.get(".entry.user").html()).toContain("<p>Fix it</p>")
    })

    it("folds each tool call with its result attached, not as a separate entry", async () => {
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(200, transcript)))
        expect(wrapper.findAll(".tool-call")).toHaveLength(1)
        expect(wrapper.text()).not.toContain("ok")
        await wrapper.get("button.tool-summary").trigger("click")
        expect(wrapper.get(".tool-result").text()).toContain("ok")
    })

    it("shows an agent's wrapper prompt as a muted meta block", async () => {
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(200, transcript)))
        expect(wrapper.find(".entry.user.meta").exists()).toBe(true)
    })

    it("hides thinking until the device setting turns it on", async () => {
        reloadSettings()
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(200, transcript)))
        expect(wrapper.find(".entry.thinking").exists()).toBe(false)

        updateSettings({showThinking: true})
        await wrapper.vm.$nextTick()
        expect(wrapper.get(".entry.thinking").html()).toContain("Hmm, auth")
        updateSettings({showThinking: false})
    })

    it("jumps to the latest answer", async () => {
        const scrolled: string[] = []
        Element.prototype.scrollIntoView = vi.fn(function (this: Element) {
            scrolled.push(this.id)
        })
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(200, transcript)))
        await wrapper.get("button.jump-latest").trigger("click")
        expect(scrolled).toEqual(["entry-a2"])
    })

    it("hides Latest answer after jumping or scrolling to the reply, and restores it away", async () => {
        let answerTop = 1200
        vi.spyOn(window, "innerHeight", "get").mockReturnValue(800)
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            return {
                top: this.id === "entry-a2" ? answerTop : 0,
                bottom: this.id === "entry-a2" ? answerTop + 100 : 120,
            } as DOMRect
        })
        Element.prototype.scrollIntoView = vi.fn(() => {
            answerTop = 140
        })
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(200, transcript)))
        expect(wrapper.find(".jump-latest").exists()).toBe(true)

        await wrapper.get(".jump-latest").trigger("click")
        expect(wrapper.find(".jump-latest").exists()).toBe(false)

        answerTop = 1200
        window.dispatchEvent(new Event("scroll"))
        await wrapper.vm.$nextTick()
        expect(wrapper.find(".jump-latest").exists()).toBe(true)

        answerTop = 650
        window.dispatchEvent(new Event("scroll"))
        await wrapper.vm.$nextTick()
        expect(wrapper.find(".jump-latest").exists()).toBe(false)
    })

    it.each(["normal", "eink"] as const)("treats a reply behind the measured bottom guard as hidden (%s)", async (displayProfile) => {
        updateSettings({displayProfile})
        vi.spyOn(window, "innerHeight", "get").mockReturnValue(800)
        const guard = document.createElement("div")
        guard.className = "bottom-guard"
        document.body.append(guard)
        let answerTop = displayProfile === "eink" ? 695 : 745
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            if (this.classList.contains("bottom-guard")) return {top: 740, bottom: 800} as DOMRect
            if (this.classList.contains("page-nav")) return {top: 684, bottom: 740} as DOMRect
            return {
                top: this.id === "entry-a2" ? answerTop : 0,
                bottom: this.id === "entry-a2" ? answerTop + 20 : 120,
            } as DOMRect
        })
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(200, transcript)))
        expect(wrapper.find(".jump-latest").exists()).toBe(true)

        answerTop = displayProfile === "eink" ? 680 : 730
        window.dispatchEvent(new Event("scroll"))
        await wrapper.vm.$nextTick()
        expect(wrapper.find(".jump-latest").exists()).toBe(false)
    })

    it("keeps title and context in the header and treats replies hidden behind it as out of view", async () => {
        let answerTop = 10
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            return {
                top: this.id === "entry-a2" ? answerTop : 0,
                bottom: this.id === "entry-a2" ? answerTop + 50 : 120,
            } as DOMRect
        })
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(200, transcript)))
        expect(wrapper.get("header h1").text()).toBe("Fix the login bug")
        expect(wrapper.get("header .session-meta").text()).toContain("claude-opus-5-5")
        expect(wrapper.find(".jump-latest").exists()).toBe(true)

        answerTop = 130
        window.dispatchEvent(new Event("resize"))
        await wrapper.vm.$nextTick()
        expect(wrapper.find(".jump-latest").exists()).toBe(false)
    })

    it("says the session was not found on a 404", async () => {
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(404, {detail: "x"})))
        expect(wrapper.text()).toContain("Session not found")
    })

    it("links back to the repository", async () => {
        const {wrapper, router} = await mountSession(
            vi.fn().mockResolvedValue(jsonResponse(200, transcript)),
        )
        await wrapper.get("a.back").trigger("click")
        await flushPromises()
        expect(router.currentRoute.value.name).toBe("repo")
        expect(router.currentRoute.value.params.prefix).toBe("alpha")
    })
})
