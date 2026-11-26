import {enableAutoUnmount, flushPromises, mount} from "@vue/test-utils"
import {getByRole, queryByRole} from "@testing-library/dom"
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest"
import {createMemoryHistory} from "vue-router"
import {Dashboard, DASHBOARD} from "../dashboard"
import {setToken} from "../api"
import type {TranscriptView} from "../protocol"
import {createAppRouter} from "../router"
import {withDashboardMode} from "../test-http"
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
        {kind: "user", id: "u1", timestamp: null, html: "<p>Fix it</p>", meta: false, label: null, expandable: false},
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
            html: "",
            meta: true,
            label: "Command: /clear",
            expandable: false,
        },
        {kind: "assistant", id: "a2", timestamp: null, html: "<p>Done.</p>", model: "claude-sonnet-5"},
    ],
}

async function mountSession(fetchMock: ReturnType<typeof vi.fn<typeof fetch>>, dashboard?: Dashboard) {
    setToken("tok")
    vi.stubGlobal("fetch", withDashboardMode(fetchMock))
    const router = createAppRouter(createMemoryHistory())
    await router.push("/repos/alpha/sessions/claude/s-1")
    await router.isReady()
    const wrapper = mount(SessionView, {global: {plugins: [router], provide: dashboard ? {[DASHBOARD as symbol]: dashboard} : {}}, attachTo: document.body})
    await flushPromises()
    return {wrapper, router}
}

beforeEach(() => {
    localStorage.setItem("jailbee.composerExpanded", "1")
    document.body.innerHTML = ""
    removeItem("jailbee.readingPositions")
})

afterEach(() => {
    removeItem("jailbee.showThinking")
    removeItem("jailbee.readingPositions")
    updateSettings({displayProfile: "normal", showThinking: false, agentContext: "hidden", pageControls: "indicator"})
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    if (originalScrollIntoView) Object.defineProperty(Element.prototype, "scrollIntoView", originalScrollIntoView)
    else Reflect.deleteProperty(Element.prototype, "scrollIntoView")
    // Unmount portalled dialogs before removing their DOM.
    // Auto-unmount runs after this hook.

})

describe("SessionView", () => {
    it("hides disabled chat controls and status while retaining questions", async () => {
        const question = "❓ **Q1** - **Deploy**: Choose.\na) Canary\n➡️ a: Safe"
        const view: TranscriptView = {...transcript, chat: {available: false, code: "permission_denied", message: "Agent chat is disabled"}, entries: [{kind: "assistant", id: "question", timestamp: null, model: "m", html: "<p>Deploy: Choose.</p>", text: question}]}
        const {wrapper} = await mountSession(vi.fn(async () => jsonResponse(200, view)))
        expect(wrapper.text()).toContain("Deploy: Choose.")
        expect(wrapper.text()).not.toContain("Agent chat disabled")
        expect(queryByRole(wrapper.element, "textbox", {name: "Message agent"})).toBeNull()
        expect(wrapper.find('[aria-label="Answer questions"]').exists()).toBe(false)
    })

    it("hides the composer while answers are open and restores its draft when hidden", async () => {
        localStorage.setItem("jailbee.composerExpanded", "1")
        const view: TranscriptView = {...transcript, chat: {available: true, code: "available", message: ""}, entries: [{kind: "assistant", id: "question", timestamp: null, model: "m", html: "<p>Choose.</p>", text: "❓ **Q1** - **Deploy**: Choose.\na) Canary\n➡️ a: Safe"}]}
        const {wrapper} = await mountSession(vi.fn(async () => jsonResponse(200, view)))
        await wrapper.get('textarea').setValue('draft')
        await wrapper.get('[aria-label="Expand answers"]').trigger('click')
        expect(queryByRole(wrapper.element, 'textbox', {name: 'Message agent'})).toBeNull()
        await wrapper.get('[aria-label="Hide"]').trigger('click')
        expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('draft')
        localStorage.removeItem("jailbee.composerExpanded")
    })

    it("keeps disabled input and answer panel for transient unavailability", async () => {
        const view: TranscriptView = {...transcript, chat: {available: false, code: "busy", message: "Agent busy"}, entries: [{kind: "assistant", id: "question", timestamp: null, model: "m", html: "<p>Choose.</p>", text: "❓ **Q1** - **Deploy**: Choose.\na) Canary\n➡️ a: Safe"}]}
        const {wrapper} = await mountSession(vi.fn(async () => jsonResponse(200, view)))
        expect(wrapper.get('textarea').attributes("disabled")).toBeDefined()
        expect(wrapper.find('[aria-label="Answer questions"]').exists()).toBe(true)
        expect(wrapper.text()).toContain("Agent busy")
    })

    it.each(["reconnect", "context reload"])("dismisses delivered answers even after %s during the POST", async reset => {
        localStorage.clear()
        const dashboard = new Dashboard(() => ({start() {}, stop() {}, send() {}}))
        dashboard.state.connected = true
        let resolve!: (response: Response) => void
        const view: TranscriptView = {...transcript, chat: {available: true, code: "available", message: ""}, entries: [{
            kind: "assistant", id: "question", message_id: "message", timestamp: null, model: "m", html: "", text: "❓ **Q1** - **Deploy**: Choose.\na) Canary\n➡️ a: Safe",
        }]}
        const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
            if (init?.method === "POST") return new Promise<Response>(r => { resolve = r })
            return jsonResponse(200, String(input).endsWith("/summary") ? {} : view)
        })
        const {wrapper} = await mountSession(fetchMock, dashboard)
        await wrapper.get('[aria-label="Expand answers"]').trigger("click")
        await wrapper.get('[aria-label="Summary"]').trigger("click")
        await wrapper.get('[aria-label="Send answers"]').trigger("click")
        if (reset === "reconnect") dashboard.state.connected = false
        else updateSettings({agentContext: "collapsed"})
        await flushPromises()
        expect(wrapper.get('[aria-label="Send answers"]').attributes("disabled")).toBeDefined()
        resolve(jsonResponse(200, {code: "delivered", message: ""}))
        await flushPromises()
        expect(wrapper.find('[aria-label="Answer questions"]').exists()).toBe(false)
        expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1)
    })

    it.each(["alpha-1", null])("keeps the root and repo and adds the summary's container only when known (%s)", async container => {
        const {wrapper} = await mountSession(vi.fn(async input => jsonResponse(200,
            String(input).endsWith('/summary') ? {container} : transcript,
        )))
        const links = wrapper.findAll('nav[aria-label="Breadcrumb"] a')
        expect(links.map(link => link.text())).toEqual(["JailBee", "alpha", ...(container ? [container] : [])])
        if (container) expect(links.at(-1)!.attributes('href')).toBe('/containers/alpha-1')
    })
    it("opens jump from the session menu and indicator, but not while typing", async () => {
        updateSettings({displayProfile: "eink"})
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(200, transcript)))
        const input = document.createElement("input")
        document.body.append(input)
        input.dispatchEvent(new KeyboardEvent("keydown", {key: "j", bubbles: true}))
        await wrapper.vm.$nextTick()
        expect(queryByRole(document.body, "dialog", {name: "Jump to"})).toBeNull()
        await wrapper.get('[aria-label="Quick settings"]').trigger("click")
        await flushPromises()
        getByRole(document.body, "button", {name: "Jump to…"}).click()
        await flushPromises()
        expect(getByRole(document.body, "dialog", {name: "Jump to"})).toBeTruthy()
        getByRole(getByRole(document.body, "dialog", {name: "Jump to"}), "button", {name: "Close"}).click()
        await flushPromises()
        await wrapper.get('button.page-indicator').trigger("click")
        await flushPromises()
        expect(getByRole(document.body, "dialog", {name: "Jump to"})).toBeTruthy()
        input.remove()
    })

    it("does not open jump over the quick settings dialog", async () => {
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(200, transcript)))
        await wrapper.get('[aria-label="Quick settings"]').trigger("click")
        expect(document.body.querySelectorAll('[role="dialog"]')).toHaveLength(1)
        document.activeElement?.dispatchEvent(new KeyboardEvent("keydown", {key: "j", bubbles: true}))
        await wrapper.vm.$nextTick()
        expect(queryByRole(document.body, "dialog", {name: "Jump to"})).toBeNull()
        expect(document.body.querySelectorAll('[role="dialog"]')).toHaveLength(1)
    })

    it("jumps to a selected prompt and latest answer", async () => {
        const scrolled: string[] = []
        Element.prototype.scrollIntoView = vi.fn(function (this: Element) { scrolled.push(this.id) })
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(200, transcript)))
        window.dispatchEvent(new KeyboardEvent("keydown", {key: "j"}))
        await wrapper.vm.$nextTick()
        await flushPromises()
        getByRole(getByRole(document.body, "dialog", {name: "Jump to"}), "button", {name: "Fix it"}).click()
        await flushPromises()
        expect(scrolled).toEqual(["entry-u1"])
        window.dispatchEvent(new KeyboardEvent("keydown", {key: "j"}))
        await wrapper.vm.$nextTick()
        await flushPromises()
        getByRole(getByRole(document.body, "dialog", {name: "Jump to"}), "button", {name: "Latest answer"}).click()
        await flushPromises()
        expect(scrolled).toEqual(["entry-u1", "entry-a2"])
    })

    it("opens a jump panel listing only user prompts in order", async () => {
        const view = {...transcript, entries: [...transcript.entries, {kind: "user" as const, id: "u3", timestamp: null, html: "<p>Next question</p>", meta: false, label: null, expandable: false}]}
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(200, view)))
        window.dispatchEvent(new KeyboardEvent("keydown", {key: "j"}))
        await wrapper.vm.$nextTick()
        await flushPromises()
        const panel = getByRole(document.body, "dialog", {name: "Jump to"})
        expect(Array.from(panel.querySelectorAll("button"), button => button.getAttribute("aria-label") ?? button.textContent)).toEqual(["Close", "Start", "Latest answer", "End", "Fix it", "Next question"])
    })
    it("fetches the session and renders prompts and answers with their models", async () => {
        const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, transcript))
        const {wrapper} = await mountSession(fetchMock)

        expect(fetchMock.mock.calls[0][0]).toBe("/api/repos/alpha/sessions/claude/s-1?meta=hidden")
        expect(wrapper.get("h1").text()).toBe("Fix the login bug")
        expect(wrapper.get("h1").attributes("title")).toContain("Session ID: s-1")
        const answers = wrapper.findAll(".entry.assistant")
        expect(answers).toHaveLength(2)
        expect(answers[0].html()).toContain("<h2>Plan</h2>")
        expect(answers[0].get(".model").text()).toBe("claude-opus-5-5")
        expect(answers[1].get(".model").text()).toBe("claude-sonnet-5")
        expect(wrapper.get(".entry.user").html()).toContain("<p>Fix it</p>")
    })

    it("shows the session metadata in its menu", async () => {
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(200, transcript)))
        await wrapper.get('[aria-label="Quick settings"]').trigger("click")
        expect(document.body.querySelector('[aria-label="Session"]')?.textContent).toContain("claude")
        expect(document.body.querySelector('[aria-label="Session"]')?.textContent).toContain("claude-opus-5-5")
        expect(document.body.querySelector('[aria-label="Session"]')?.textContent).toContain("s-1")
    })

    it("folds each tool call with its result attached, not as a separate entry", async () => {
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(200, transcript)))
        expect(wrapper.findAll(".tool-call")).toHaveLength(1)
        expect(wrapper.text()).not.toContain("ok")
        await wrapper.get("button.tool-summary").trigger("click")
        expect(wrapper.get(".tool-result").text()).toContain("ok")
    })

    it("renders an unattached tool result as document content inside its entry", async () => {
        const result = {kind: "tool_result" as const, id: "orphan", timestamp: null, call_id: "missing",
            html: "<p>Unattached output</p>", truncated: false, full_length: 17, is_error: false}
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(200, {
            ...transcript, entries: [...transcript.entries, result],
        })))
        expect(wrapper.get("#entry-orphan .prose").text()).toBe("Unattached output")
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

    it("hides the jump button at the transcript end even when the last answer starts above the viewport", async () => {
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            if (this.classList.contains("transcript")) return {top: -3000, bottom: 700} as DOMRect
            if (this.id === "entry-a2") return {top: -2000, bottom: 700} as DOMRect
            return {top: 0, bottom: 120} as DOMRect
        })
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(200, transcript)))
        expect(wrapper.find(".jump-latest").exists()).toBe(false)
    })

    it("offers Scroll to bottom while reading inside a long latest answer", async () => {
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            if (this.classList.contains("transcript")) return {top: -1000, bottom: 3000} as DOMRect
            if (this.id === "entry-a2") return {top: -200, bottom: 3000} as DOMRect
            return {top: 0, bottom: 120} as DOMRect
        })
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(200, transcript)))
        expect(wrapper.get(".jump-latest").text()).toBe("Scroll to bottom ↓")
    })

    it("offers the bottom after passing the latest answer when later tool content remains", async () => {
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            if (this.classList.contains("transcript")) return {top: -3000, bottom: 2000} as DOMRect
            if (this.id === "entry-a2") return {top: -2000, bottom: -1000} as DOMRect
            return {top: 0, bottom: 120} as DOMRect
        })
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(200, transcript)))
        expect(wrapper.get(".jump-latest").text()).toBe("Scroll to bottom ↓")
    })

    it("jumps to the latest answer", async () => {
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({top: 1200, bottom: 1300} as DOMRect)
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
            if (this.classList.contains("transcript")) return {top: -1000, bottom: answerTop + 100} as DOMRect
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
        updateSettings({displayProfile, pageControls: "bar"})
        vi.spyOn(window, "innerHeight", "get").mockReturnValue(800)
        const guard = document.createElement("div")
        guard.className = "bottom-guard"
        document.body.append(guard)
        let answerTop = displayProfile === "eink" ? 695 : 745
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            if (this.classList.contains("bottom-guard")) return {top: 740, bottom: 800} as DOMRect
            if (this.classList.contains("page-nav")) return {top: 684, bottom: 740} as DOMRect
            if (this.classList.contains("transcript")) return {top: -1000, bottom: answerTop + 20} as DOMRect
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
        expect(wrapper.get(".jump-latest").text()).toBe("Scroll to bottom ↓")
    })

    it("keeps title and context in the header while offering the transcript end", async () => {
        let answerTop = 10
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            return {
                top: this.id === "entry-a2" ? answerTop : 0,
                bottom: this.classList.contains("transcript") ? 3000 : this.id === "entry-a2" ? answerTop + 50 : 120,
            } as DOMRect
        })
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(200, transcript)))
        expect(wrapper.get("header h1").text()).toBe("Fix the login bug")
        expect(wrapper.get("header h1").attributes("title")).toContain("claude-opus-5-5")
        expect(wrapper.find(".jump-latest").exists()).toBe(true)

        answerTop = 130
        window.dispatchEvent(new Event("resize"))
        await wrapper.vm.$nextTick()
        expect(wrapper.get(".jump-latest").text()).toBe("Scroll to bottom ↓")
    })

    it("says the session was not found on a 404", async () => {
        const {wrapper} = await mountSession(vi.fn().mockResolvedValue(jsonResponse(404, {detail: "x"})))
        expect(wrapper.text()).toContain("Session not found")
        expect(wrapper.get(".notice").classes()).toContain("ui-notice")
    })

    it("links back to the repository", async () => {
        const {wrapper, router} = await mountSession(
            vi.fn().mockResolvedValue(jsonResponse(200, transcript)),
        )
        await wrapper.get('a[href="/repos/alpha"]').trigger("click")
        await flushPromises()
        expect(router.currentRoute.value.name).toBe("repo")
        expect(router.currentRoute.value.params.prefix).toBe("alpha")
    })
})
