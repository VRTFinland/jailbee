import {enableAutoUnmount, flushPromises, mount} from "@vue/test-utils"
import {afterEach, describe, expect, it, vi} from "vitest"
import {createMemoryHistory} from "vue-router"
import {setToken} from "../api"
import {DASHBOARD, Dashboard, type SocketLike} from "../dashboard"
import {
    PROTOCOL_VERSION,
    type ClientMessage,
    type FollowKey,
    type RenderedEntry,
    type TranscriptView,
} from "../protocol"
import {createAppRouter} from "../router"
import {withDashboardMode} from "../test-http"
import {reloadSettings, updateSettings} from "../settings"
import type {SocketHandlers} from "../socket"
import SessionView from "./SessionView.vue"

enableAutoUnmount(afterEach)

const transcript: TranscriptView = {
    session_id: "s-1",
    agent: "claude",
    title: "Fix the login bug",
    last_activity: null,
    models: [],
    offset: 420,
    entries: [{kind: "user", id: "u1", timestamp: null, html: "<p>Fix it</p>", meta: false, label: null, expandable: false}],
}

const answer: RenderedEntry = {
    kind: "assistant",
    id: "a1",
    timestamp: null,
    html: "<p>Working on it</p>",
    model: "claude-opus-5-5",
}

async function mountLive(path: string) {
    setToken("tok")
    const fetchMock = vi.fn().mockImplementation(async () =>
        new Response(JSON.stringify(transcript), {
            status: 200,
            headers: {"Content-Type": "application/json"},
        }),
    )
    vi.stubGlobal("fetch", withDashboardMode(fetchMock))
    let handlers: SocketHandlers | null = null
    const sent: ClientMessage[] = []
    const dashboard = new Dashboard((h): SocketLike => {
        handlers = h
        return {start() {}, stop() {}, send: (m) => sent.push(m)}
    })
    dashboard.start()
    handlers!.onConnectionChange?.(true)
    handlers!.onMessage({type: "hello", v: PROTOCOL_VERSION, device_name: "boox"})

    const router = createAppRouter(createMemoryHistory())
    await router.push(path)
    await router.isReady()
    const wrapper = mount(SessionView, {
        global: {plugins: [router], provide: {[DASHBOARD]: dashboard}},
        attachTo: document.body,
    })
    await flushPromises()
    const append = async (key: FollowKey, entries: RenderedEntry[]) => {
        handlers!.onMessage({type: "append", v: PROTOCOL_VERSION, key, entries, offset: 999})
        await flushPromises()
    }
    const burst = async (key: FollowKey, first: RenderedEntry[], second: RenderedEntry[]) => {
        handlers!.onMessage({type: "append", v: PROTOCOL_VERSION, key, entries: first, offset: 700})
        // Deliver another frame after Vue patches the first, before its scroll.
        queueMicrotask(() => {
            handlers!.onMessage({type: "append", v: PROTOCOL_VERSION, key, entries: second, offset: 999})
        })
        await flushPromises()
    }
    const hello = () => handlers!.onMessage({type: "hello", v: PROTOCOL_VERSION, device_name: "boox"})
    return {
        wrapper,
        sent,
        append,
        burst,
        hello,
        fetchMock,
        connection: (connected: boolean) => handlers!.onConnectionChange?.(connected),
    }
}

const sessionKey: FollowKey = {prefix: "alpha", agent: "claude", session_id: "s-1", subagent_id: null}

function setPageGeometry(scrollHeight: number, scrollY: number) {
    Object.defineProperty(document.documentElement, "scrollHeight", {
        value: scrollHeight,
        configurable: true,
    })
    Object.defineProperty(window, "scrollY", {value: scrollY, configurable: true})
    Object.defineProperty(window, "innerHeight", {value: 800, configurable: true})
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
        if (this.classList.contains("transcript")) return {top: -scrollY, bottom: scrollHeight - scrollY} as DOMRect
        return {top: 0, bottom: 0} as DOMRect
    })
}

afterEach(() => {
    updateSettings({displayProfile: "normal", einkFollow: false})
    document.body.innerHTML = ""
    vi.restoreAllMocks()
})

describe("SessionView live follow", () => {
    it("subscribes from the offset the transcript was read to", async () => {
        const {sent} = await mountLive("/repos/alpha/sessions/claude/s-1")
        expect(sent).toEqual([
            {type: "subscribe", v: PROTOCOL_VERSION, key: sessionKey, offset: 420, meta: "hidden"},
        ])
    })

    it("subscribes with the stored Agent context setting", async () => {
        updateSettings({agentContext: "collapsed"})
        try {
            const {sent} = await mountLive("/repos/alpha/sessions/claude/s-1")
            expect(sent).toEqual([
                {type: "subscribe", v: PROTOCOL_VERSION, key: sessionKey, offset: 420, meta: "collapsed"},
            ])
        } finally {
            updateSettings({agentContext: "hidden"})
        }
    })

    it("resubscribes with the new mode when Agent context changes", async () => {
        const {sent, fetchMock} = await mountLive("/repos/alpha/sessions/claude/s-1")
        sent.length = 0
        updateSettings({agentContext: "collapsed"})
        try {
            await flushPromises()
            expect(sent).toEqual([
                {type: "unsubscribe", v: PROTOCOL_VERSION, key: sessionKey},
                {type: "subscribe", v: PROTOCOL_VERSION, key: sessionKey, offset: 420, meta: "collapsed"},
            ])
            const urls = fetchMock.mock.calls.map((c) => c[0])
            expect(urls.at(-1)).toBe("/api/repos/alpha/sessions/claude/s-1?meta=collapsed")
        } finally {
            updateSettings({agentContext: "hidden"})
        }
    })

    it("an empty append moves the offset a reconnect resubscribes from", async () => {
        const {wrapper, sent, append, hello, connection} = await mountLive("/repos/alpha/sessions/claude/s-1")
        await append(sessionKey, [])
        expect(wrapper.findAll(".entry")).toHaveLength(1)
        sent.length = 0
        connection(false)
        connection(true)
        hello()
        expect(sent).toEqual([
            {type: "subscribe", v: PROTOCOL_VERSION, key: sessionKey, offset: 999, meta: "hidden"},
        ])
    })

    it("appends new entries in place", async () => {
        const {wrapper, append} = await mountLive("/repos/alpha/sessions/claude/s-1")
        await append(sessionKey, [answer])
        const answers = wrapper.findAll(".entry.assistant")
        expect(answers).toHaveLength(1)
        expect(answers[0].html()).toContain("Working on it")
        expect(wrapper.findAll(".entry.user")).toHaveLength(1)
    })

    it("scrolls along when the reader was at the end", async () => {
        setPageGeometry(1000, 200)
        const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation(() => {})
        const {append} = await mountLive("/repos/alpha/sessions/claude/s-1")
        await append(sessionKey, [answer])
        expect(scrollTo).toHaveBeenCalled()
    })

    it.each(["normal", "eink"] as const)("keeps following a burst across DOM updates (%s)", async (displayProfile) => {
        updateSettings({displayProfile, einkFollow: true})
        setPageGeometry(5000, 0)
        let y = 3200
        Object.defineProperty(window, "scrollY", {get: () => y, configurable: true})
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            const bottom = document.querySelector("#entry-a1") ? 4500 : 4000
            if (this.classList.contains("transcript")) return {top: -y, bottom: bottom - y} as DOMRect
            if (this.classList.contains("page-nav")) return {top: 800, bottom: 800} as DOMRect
            return {top: 0, bottom: 0} as DOMRect
        })
        vi.spyOn(window, "scrollTo").mockImplementation((_x, top) => { y = top ?? 0 })
        const {wrapper, burst} = await mountLive("/repos/alpha/sessions/claude/s-1")
        await burst(sessionKey, [answer], [{...answer, id: "a2"}])
        expect(wrapper.findAll(".entry.assistant")).toHaveLength(2)
        expect(wrapper.find(".new-messages").exists()).toBe(false)
    })

    it("keeps following when layout growth moves the end without a scroll", async () => {
        setPageGeometry(1000, 200)
        let bottom = 800
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            return {top: 0, bottom: this.classList.contains("transcript") ? bottom : 0} as DOMRect
        })
        const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation(() => {})
        const {append} = await mountLive("/repos/alpha/sessions/claude/s-1")
        bottom = 900
        window.dispatchEvent(new Event("resize"))
        await append(sessionKey, [answer])
        expect(scrollTo).toHaveBeenCalled()
    })

    it("leaves the scroll position alone when the reader was further up", async () => {
        setPageGeometry(5000, 100)
        const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation(() => {})
        const {append} = await mountLive("/repos/alpha/sessions/claude/s-1")
        await append(sessionKey, [answer])
        expect(scrollTo).not.toHaveBeenCalled()
    })

    it("stops following when later frames of one upward gesture leave the end", async () => {
        setPageGeometry(5000, 0)
        let y = 3400
        Object.defineProperty(window, "scrollY", {get: () => y, configurable: true})
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            return {top: -y, bottom: this.classList.contains("transcript") ? 4000 - y : 0} as DOMRect
        })
        const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation(() => {})
        const {wrapper, append} = await mountLive("/repos/alpha/sessions/claude/s-1")
        window.dispatchEvent(new Event("wheel"))
        y = 3300 // The first frame still shows the end.
        window.dispatchEvent(new Event("scroll"))
        y = 3000 // Momentum continues without another input event.
        window.dispatchEvent(new Event("scroll"))
        await append(sessionKey, [answer])
        expect(scrollTo).not.toHaveBeenCalled()
        expect(wrapper.get(".new-messages").text()).toBe("New messages ↓")
    })

    it("keeps follow through delayed programmatic scroll events while layout grows", async () => {
        setPageGeometry(5000, 0)
        let y = 3400
        let bottom = 4000
        Object.defineProperty(window, "scrollY", {get: () => y, configurable: true})
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            return {top: -y, bottom: this.classList.contains("transcript") ? bottom - y : 0} as DOMRect
        })
        const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation(() => {})
        const {wrapper, append} = await mountLive("/repos/alpha/sessions/claude/s-1")
        window.dispatchEvent(new KeyboardEvent("keydown", {key: "End"}))
        await append(sessionKey, [answer])
        // scrollTo's event arrives later, after more layout growth.
        bottom = 4500
        y = 3200
        window.dispatchEvent(new Event("scroll"))
        scrollTo.mockClear()
        await append(sessionKey, [{...answer, id: "a2"}])
        expect(scrollTo).toHaveBeenCalled()
        expect(wrapper.find(".new-messages").exists()).toBe(false)
    })

    it("ignores hidden thinking and folded results when choosing unseen entries", async () => {
        setPageGeometry(5000, 100)
        const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation(() => {})
        const {wrapper, append} = await mountLive("/repos/alpha/sessions/claude/s-1")
        const call: RenderedEntry = {kind: "tool_call", id: "c1", timestamp: null, call_id: "c", name: "Bash", summary: "Bash", input_html: "", model: null}
        await append(sessionKey, [call])
        await wrapper.get(".new-messages").trigger("click")
        Object.defineProperty(window, "scrollY", {value: 50, configurable: true})
        window.dispatchEvent(new Event("wheel"))
        window.dispatchEvent(new Event("scroll"))
        // Prove follow is off through its public effect before testing unseen.
        scrollTo.mockClear()
        await append(sessionKey, [{kind: "thinking", id: "probe", timestamp: null, html: "", model: null}])
        expect(scrollTo).not.toHaveBeenCalled()
        await append(sessionKey, [{kind: "thinking", id: "t1", timestamp: null, html: "", model: null}, {kind: "tool_result", id: "r1", timestamp: null, call_id: "c", html: "", truncated: false, full_length: 0, is_error: false}])
        expect(wrapper.find(".new-messages").exists()).toBe(false)
        await append(sessionKey, [answer])
        expect(wrapper.find(".new-messages").exists()).toBe(true)
    })

    it.each(["normal", "eink"] as const)("does not follow away from a long answer's start (%s)", async (displayProfile) => {
        updateSettings({displayProfile, einkFollow: true})
        setPageGeometry(5000, 0)
        let y = 0
        Object.defineProperty(window, "scrollY", {get: () => y, configurable: true})
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            if (this.classList.contains("transcript")) return {top: -y, bottom: 4000 - y} as DOMRect
            return {top: this.id === "entry-a1" ? 1200 - y : 0, bottom: 0} as DOMRect
        })
        const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation((_x, top) => { y = top ?? 0 })
        const scrollIntoView = vi.fn(() => { y = 1200 })
        Element.prototype.scrollIntoView = scrollIntoView
        try {
            const {wrapper, append} = await mountLive("/repos/alpha/sessions/claude/s-1")
            await append(sessionKey, [answer])
            await wrapper.get(".jump-latest").trigger("click")
            // An asynchronous jump may pass the end before settling at the
            // answer's start (e.g. when already in the trailing spacer).
            y = 3400
            window.dispatchEvent(new Event("scroll"))
            y = 1200
            window.dispatchEvent(new Event("scroll"))
            scrollTo.mockClear()
            await append(sessionKey, [{...answer, id: "a2"}])
            expect(scrollTo).not.toHaveBeenCalled()
            expect(wrapper.get(".new-messages").text().toLowerCase()).toContain("new message")
        } finally {
            Reflect.deleteProperty(Element.prototype, "scrollIntoView")
        }
    })

    it("keeps the first unseen answer as the New messages jump target", async () => {
        setPageGeometry(5000, 100)
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            if (this.classList.contains("transcript")) return {top: -100, bottom: 4000} as DOMRect
            const top = this.id === "entry-a2" ? 1200 : this.id === "entry-a1" ? 200 : 0
            return {top, bottom: top + 100} as DOMRect
        })
        const {wrapper, append} = await mountLive("/repos/alpha/sessions/claude/s-1")
        await append(sessionKey, [answer])
        expect(wrapper.get(".jump-latest").text()).toBe("New messages ↓")
        await append(sessionKey, [{...answer, id: "a2"}])
        expect(wrapper.get(".jump-latest").text()).toBe("New messages ↓")
        const scroll = vi.fn()
        Element.prototype.scrollIntoView = scroll
        await wrapper.get(".jump-latest").trigger("click")
        expect(scroll.mock.instances[0]).toBe(document.getElementById("entry-a1"))
        Reflect.deleteProperty(Element.prototype, "scrollIntoView")
        wrapper.unmount()
    })

    it("turns follow off on scroll up and restores it with a non-answer New messages jump", async () => {
        setPageGeometry(5000, 0)
        let y = 3200
        let bottom = 4000
        Object.defineProperty(window, "scrollY", {get: () => y, configurable: true})
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            if (this.classList.contains("transcript")) return {top: -y, bottom: bottom - y} as DOMRect
            return {top: 0, bottom: 0} as DOMRect
        })
        const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation((_x, top) => {
            y = top ?? 0
            window.dispatchEvent(new Event("scroll"))
        })
        const {wrapper, append} = await mountLive("/repos/alpha/sessions/claude/s-1")
        expect(wrapper.find(".jump-latest").exists()).toBe(false)
        await append(sessionKey, [answer])
        expect(scrollTo).toHaveBeenCalled()
        y = 100
        window.dispatchEvent(new Event("wheel"))
        window.dispatchEvent(new Event("scroll"))
        scrollTo.mockClear()
        bottom = 4500
        await append(sessionKey, [{...transcript.entries[0], id: "u2"}, {...answer, id: "a2"}])
        expect(scrollTo).not.toHaveBeenCalled()
        expect(wrapper.get(".jump-latest").text()).toBe("New messages ↓")
        await wrapper.get(".jump-latest").trigger("click")
        expect(y).toBe(3700)
        expect(wrapper.find(".jump-latest").exists()).toBe(false)
        scrollTo.mockClear()
        await append(sessionKey, [{...answer, id: "a3"}])
        expect(scrollTo).toHaveBeenCalled()
    })

    it("lets a page-turn click interrupt an unfinished automatic follow", async () => {
        updateSettings({displayProfile: "eink", einkFollow: true})
        setPageGeometry(5000, 0)
        let y = 3200
        Object.defineProperty(window, "scrollY", {get: () => y, configurable: true})
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            if (this.classList.contains("transcript")) return {top: -y, bottom: 4000 - y} as DOMRect
            if (this.classList.contains("page-nav")) return {top: 800, bottom: 800} as DOMRect
            return {top: 0, bottom: 0} as DOMRect
        })
        const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation((_x, top) => {
            y = top ?? 0
            window.dispatchEvent(new Event("scroll"))
        })
        const {wrapper, append} = await mountLive("/repos/alpha/sessions/claude/s-1")
        await append(sessionKey, [answer])
        await wrapper.get("button.prev").trigger("click")
        scrollTo.mockClear()
        await append(sessionKey, [{...answer, id: "a2"}])
        expect(scrollTo).not.toHaveBeenCalled()
        expect(wrapper.get(".new-messages").text()).toBe("1 new message ↓")
    })

    it("keeps the connection notice with the session context", async () => {
        const {wrapper, connection} = await mountLive("/repos/alpha/sessions/claude/s-1")
        connection(false)
        await flushPromises()
        expect(wrapper.get("header .notice").text()).toBe("Connecting…")
        expect(wrapper.get("header h1").text()).toBe("Fix the login bug")
        connection(true)
        await flushPromises()
        expect(wrapper.find("header .notice").exists()).toBe(false)
        wrapper.unmount()
    })

    it("unsubscribes when the view goes away", async () => {
        const {wrapper, sent} = await mountLive("/repos/alpha/sessions/claude/s-1")
        wrapper.unmount()
        expect(sent.at(-1)).toEqual({type: "unsubscribe", v: PROTOCOL_VERSION, key: sessionKey})
    })

    it("follows a subagent's own transcript on the subagent route", async () => {
        const {sent} = await mountLive("/repos/alpha/sessions/claude/s-1/subagents/a1b2c3")
        expect(sent).toEqual([
            {
                type: "subscribe",
                v: PROTOCOL_VERSION,
                key: {...sessionKey, subagent_id: "a1b2c3"},
                offset: 420,
                meta: "hidden",
            },
        ])
    })
})

describe("SessionView in the e-ink profile", () => {
    const second: RenderedEntry = {...answer, id: "a2", html: "<p>Done</p>"}

    it("holds live entries back behind a new-messages notice", async () => {
        const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation(() => {})
        setPageGeometry(1000, 200)
        updateSettings({displayProfile: "eink"})
        const {wrapper, append} = await mountLive("/repos/alpha/sessions/claude/s-1")
        await append(sessionKey, [answer, second])
        expect(wrapper.findAll(".entry.assistant")).toHaveLength(0)
        expect(wrapper.get(".new-messages").text()).toBe("2 new messages ↓")
        expect(scrollTo).not.toHaveBeenCalled()

        await append(sessionKey, [{...answer, id: "a3"}, {kind: "thinking", id: "t2", timestamp: null, html: "", model: null}])
        expect(wrapper.get(".new-messages").text()).toBe("3 new messages ↓")
    })

    it("renders the held entries only when the notice is tapped", async () => {
        updateSettings({displayProfile: "eink"})
        const {wrapper, append} = await mountLive("/repos/alpha/sessions/claude/s-1")
        await append(sessionKey, [answer])
        await wrapper.get(".new-messages").trigger("click")
        expect(wrapper.findAll(".entry.assistant")).toHaveLength(1)
        expect(wrapper.find(".new-messages").exists()).toBe(false)
    })

    it("applies held entries when switching back to the normal profile", async () => {
        updateSettings({displayProfile: "eink"})
        const {wrapper, append} = await mountLive("/repos/alpha/sessions/claude/s-1")
        await append(sessionKey, [answer])
        updateSettings({displayProfile: "normal"})
        await flushPromises()
        expect(wrapper.findAll(".entry.assistant")).toHaveLength(1)
        expect(wrapper.find(".new-messages").exists()).toBe(false)
    })

    it("has page turns on the subagent route too", async () => {
        updateSettings({displayProfile: "eink"})
        const {wrapper} = await mountLive("/repos/alpha/sessions/claude/s-1/subagents/a1b2c3")
        expect(wrapper.find(".page-indicator").exists()).toBe(true)
    })

    it("reserves the sticky header on e-ink page turns and hides Latest answer on its page", async () => {
        updateSettings({displayProfile: "eink"})
        setPageGeometry(3000, 0)
        let y = 0
        Object.defineProperty(window, "scrollY", {get: () => y, configurable: true})
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            if (this.classList.contains("page-nav")) return {top: 712, bottom: 768} as DOMRect
            if (this.classList.contains("transcript")) return {top: -y, bottom: 2500 - y} as DOMRect
            const top = this.id === "entry-a1" ? 1400 - y : 0
            return {top, bottom: top + (this.id === "entry-a1" ? 50 : 120)} as DOMRect
        })
        const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation((_x, top) => {
            y = top ?? 0
            window.dispatchEvent(new Event("scroll"))
        })
        const {wrapper, append} = await mountLive("/repos/alpha/sessions/claude/s-1")
        await append(sessionKey, [answer])
        await wrapper.get(".new-messages").trigger("click")
        expect(scrollTo).toHaveBeenLastCalledWith(0, 1280)
        await wrapper.get("button.next").trigger("click")
        expect(scrollTo).toHaveBeenLastCalledWith(0, 1788)
        await wrapper.get("button.prev").trigger("click")
        expect(wrapper.get(".jump-latest").text()).toBe("Scroll to bottom ↓")
        await wrapper.get("button.prev").trigger("click")
        expect(wrapper.find(".jump-latest").exists()).toBe(true)
        wrapper.unmount()
    })

    it("keeps page turns forward when the connection notice makes the header taller", async () => {
        updateSettings({displayProfile: "eink"})
        setPageGeometry(10000, 0)
        let y = 0
        let height = 120
        const observers: (() => void)[] = []
        vi.stubGlobal("ResizeObserver", class {
            constructor(callback: () => void) { observers.push(callback) }
            observe() {}
            disconnect() {}
        })
        Object.defineProperty(window, "scrollY", {get: () => y, configurable: true})
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            if (this.classList.contains("transcript")) return {top: -y, bottom: 9500 - y} as DOMRect
            return this.classList.contains("page-nav")
                ? {top: 712, bottom: 768} as DOMRect
                : {top: 0, bottom: height} as DOMRect
        })
        vi.spyOn(window, "scrollTo").mockImplementation((_x, top) => {
            y = top ?? 0
            window.dispatchEvent(new Event("scroll"))
        })
        const {wrapper, connection} = await mountLive("/repos/alpha/sessions/claude/s-1")
        for (let i = 0; i < 5; i++) await wrapper.get("button.next").trigger("click")
        expect(y).toBe(2960)
        connection(false)
        height = 320
        observers.forEach((notify) => notify())
        await flushPromises()
        await wrapper.get("button.next").trigger("click")
        expect(y).toBe(3136)
        wrapper.unmount()
    })

    it("starts a page at the latest answer", async () => {
        reloadSettings()
        updateSettings({displayProfile: "eink"})
        setPageGeometry(3000, 0) // pages of 800 - 88 = 712px
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            if (this.classList.contains("page-nav")) return {top: 712, bottom: 768} as DOMRect
            if (this.id === "entry-a1") return {top: 1500, bottom: 1550} as DOMRect
            if (this.classList.contains("transcript")) return {top: 0, bottom: 2500} as DOMRect
            return {top: 0, bottom: 0} as DOMRect
        })
        const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation(() => {})
        const {wrapper, append} = await mountLive("/repos/alpha/sessions/claude/s-1")
        await append(sessionKey, [answer])
        await wrapper.get(".new-messages").trigger("click")
        expect(scrollTo).toHaveBeenLastCalledWith(0, 1500)
        // Let the mocked browser's scroll event synchronise the page counter.
        Object.defineProperty(window, "scrollY", {value: 1500, configurable: true})
        window.dispatchEvent(new Event("scroll"))
        await flushPromises()
        expect(wrapper.get(".page-indicator").text()).toContain("Page 4 /")
    })
})
