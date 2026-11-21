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
