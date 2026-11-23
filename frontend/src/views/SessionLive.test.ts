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
    vi.stubGlobal(
        "fetch",
        vi.fn().mockImplementation(async () =>
            new Response(JSON.stringify(transcript), {
                status: 200,
                headers: {"Content-Type": "application/json"},
            }),
        ),
    )
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
    const hello = () => handlers!.onMessage({type: "hello", v: PROTOCOL_VERSION, device_name: "boox"})
    return {
        wrapper,
        sent,
        append,
        hello,
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
}

afterEach(() => {
    updateSettings({displayProfile: "normal"})
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
        const {sent} = await mountLive("/repos/alpha/sessions/claude/s-1")
        sent.length = 0
        updateSettings({agentContext: "collapsed"})
        try {
            await flushPromises()
            expect(sent).toEqual([
                {type: "unsubscribe", v: PROTOCOL_VERSION, key: sessionKey},
                {type: "subscribe", v: PROTOCOL_VERSION, key: sessionKey, offset: 420, meta: "collapsed"},
            ])
            const urls = (fetch as unknown as {mock: {calls: unknown[][]}}).mock.calls.map((c) => c[0])
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

    it("leaves the scroll position alone when the reader was further up", async () => {
        setPageGeometry(5000, 100)
        const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation(() => {})
        const {append} = await mountLive("/repos/alpha/sessions/claude/s-1")
        await append(sessionKey, [answer])
        expect(scrollTo).not.toHaveBeenCalled()
    })

    it("shows Latest answer when a newer reply arrives below the fold", async () => {
        setPageGeometry(5000, 100)
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            const top = this.id === "entry-a2" ? 1200 : this.id === "entry-a1" ? 200 : 0
            return {top, bottom: top + 100} as DOMRect
        })
        const {wrapper, append} = await mountLive("/repos/alpha/sessions/claude/s-1")
        await append(sessionKey, [answer])
        expect(wrapper.find(".jump-latest").exists()).toBe(false)
        await append(sessionKey, [{...answer, id: "a2"}])
        expect(wrapper.find(".jump-latest").exists()).toBe(true)
        wrapper.unmount()
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
        expect(wrapper.get(".new-messages").text()).toContain("2 new messages")
        expect(scrollTo).not.toHaveBeenCalled()

        await append(sessionKey, [{...answer, id: "a3"}])
        expect(wrapper.get(".new-messages").text()).toContain("3 new messages")
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
        await wrapper.get(".jump-latest").trigger("click")
        expect(scrollTo).toHaveBeenLastCalledWith(0, 1280)
        await wrapper.get("button.next").trigger("click")
        expect(scrollTo).toHaveBeenLastCalledWith(0, 1872)
        await wrapper.get("button.prev").trigger("click")
        expect(wrapper.find(".jump-latest").exists()).toBe(false)
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
            return {top: 0, bottom: 0} as DOMRect
        })
        const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation(() => {})
        const {wrapper, append} = await mountLive("/repos/alpha/sessions/claude/s-1")
        await append(sessionKey, [answer])
        await wrapper.get(".new-messages").trigger("click")
        await wrapper.get(".jump-latest").trigger("click")
        expect(scrollTo).toHaveBeenLastCalledWith(0, 1500)
        expect(wrapper.get(".page-indicator").text()).toContain("Page 4 /")
    })
})
