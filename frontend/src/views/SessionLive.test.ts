import {flushPromises, mount} from "@vue/test-utils"
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

const transcript: TranscriptView = {
    session_id: "s-1",
    agent: "claude",
    title: "Fix the login bug",
    last_activity: null,
    models: [],
    offset: 420,
    entries: [{kind: "user", id: "u1", timestamp: null, html: "<p>Fix it</p>", meta: false}],
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
        vi.fn().mockResolvedValue(
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
    return {wrapper, sent, append}
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
            {type: "subscribe", v: PROTOCOL_VERSION, key: sessionKey, offset: 420},
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

    it("jumps to the page that holds the latest answer", async () => {
        reloadSettings()
        updateSettings({displayProfile: "eink"})
        setPageGeometry(3000, 0) // pages of 800 - 88 = 712px
        const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation(() => {})
        const {wrapper, append} = await mountLive("/repos/alpha/sessions/claude/s-1")
        await append(sessionKey, [answer])
        await wrapper.get(".new-messages").trigger("click")
        const el = document.getElementById("entry-a1")!
        vi.spyOn(el, "getBoundingClientRect").mockReturnValue({top: 1500} as DOMRect)
        await wrapper.get(".jump-latest").trigger("click")
        expect(scrollTo).toHaveBeenLastCalledWith(0, 2 * 712)
        expect(wrapper.get(".page-indicator").text()).toContain("Page 3 /")
    })
})
