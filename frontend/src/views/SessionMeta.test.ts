import {enableAutoUnmount, flushPromises, mount} from "@vue/test-utils"
import {afterEach, describe, expect, it, vi} from "vitest"
import {createMemoryHistory} from "vue-router"
import {setToken} from "../api"
import type {RenderedEntry, TranscriptView} from "../protocol"
import {createAppRouter} from "../router"
import {updateSettings} from "../settings"
import SessionView from "./SessionView.vue"

enableAutoUnmount(afterEach)

function json(body: unknown): Response {
    return new Response(JSON.stringify(body), {
        status: 200,
        headers: {"Content-Type": "application/json"},
    })
}

function meta(id: string, label: string, over: Partial<RenderedEntry> = {}): RenderedEntry {
    return {
        kind: "user",
        id,
        timestamp: null,
        html: "",
        meta: true,
        label,
        expandable: true,
        ...over,
    } as RenderedEntry
}

const typed: RenderedEntry = {
    kind: "user",
    id: "u3",
    timestamp: null,
    html: "<p>Fix it</p>",
    meta: false,
    label: null,
    expandable: false,
}

function view(entries: RenderedEntry[]): TranscriptView {
    return {
        offset: 0,
        session_id: "s-1",
        agent: "claude",
        title: "Fix the login bug",
        last_activity: null,
        models: [],
        entries,
    }
}

const collapsed = view([
    meta("m1", "Task notification"),
    meta("m2", "System reminder"),
    meta("m3", "Command: /model opus"),
    typed,
])

async function mountSession(fetchMock: ReturnType<typeof vi.fn>) {
    setToken("tok")
    vi.stubGlobal("fetch", fetchMock)
    const router = createAppRouter(createMemoryHistory())
    await router.push("/repos/alpha/sessions/claude/s-1")
    await router.isReady()
    const wrapper = mount(SessionView, {global: {plugins: [router]}, attachTo: document.body})
    await flushPromises()
    return wrapper
}

afterEach(() => {
    updateSettings({agentContext: "hidden"})
    vi.unstubAllGlobals()
    document.body.innerHTML = ""
})

describe("SessionView agent context", () => {
    it("asks for the stored mode and renders collapsed records as closed one-line labels", async () => {
        updateSettings({agentContext: "collapsed"})
        const fetchMock = vi.fn().mockResolvedValue(json(collapsed))
        const wrapper = await mountSession(fetchMock)

        expect(fetchMock.mock.calls[0][0]).toBe("/api/repos/alpha/sessions/claude/s-1?meta=collapsed")
        const records = wrapper.findAll("details.meta-record")
        expect(records.map((r) => r.get("summary").text())).toEqual([
            "Task notification",
            "System reminder",
            "Command: /model opus",
        ])
        expect(records.every((r) => !r.attributes("open") && r.attributes("open") === undefined)).toBe(true)
        expect(wrapper.text()).toContain("Fix it")
        expect(fetchMock).toHaveBeenCalledTimes(1)
    })

    it("fetches the content on first expand only, and shows it", async () => {
        updateSettings({agentContext: "collapsed"})
        const fetchMock = vi
            .fn()
            .mockResolvedValueOnce(json(collapsed))
            .mockResolvedValue(json({id: "m1", html: '<pre class="meta">Job done</pre>'}))
        const wrapper = await mountSession(fetchMock)
        const first = wrapper.findAll("details.meta-record")[0]

        first.element.setAttribute("open", "")
        await first.trigger("toggle")
        await flushPromises()

        expect(fetchMock.mock.calls[1][0]).toBe("/api/repos/alpha/sessions/claude/s-1/entries/m1")
        expect(first.text()).toContain("Job done")

        first.element.removeAttribute("open")
        await first.trigger("toggle")
        first.element.setAttribute("open", "")
        await first.trigger("toggle")
        await flushPromises()
        expect(fetchMock).toHaveBeenCalledTimes(2)
    })

    it("shows a label line for a slash command in hidden mode and nothing else", async () => {
        const hidden = view([meta("m3", "Command: /model opus", {expandable: false}), typed])
        const wrapper = await mountSession(vi.fn().mockResolvedValue(json(hidden)))
        expect(wrapper.find("details").exists()).toBe(false)
        expect(wrapper.get(".meta-label").text()).toBe("Command: /model opus")
        expect(wrapper.findAll(".entry.user")).toHaveLength(2)
    })

    it("shows label and content in shown mode, and keeps the session title", async () => {
        updateSettings({agentContext: "shown"})
        const shown = view([
            meta("m1", "Task notification", {
                html: '<pre class="meta">&lt;task-notification&gt;x&lt;/task-notification&gt;</pre>',
                expandable: false,
            }),
            typed,
        ])
        const wrapper = await mountSession(vi.fn().mockResolvedValue(json(shown)))
        expect(wrapper.find("details").exists()).toBe(false)
        expect(wrapper.get(".meta-label").text()).toBe("Task notification")
        expect(wrapper.text()).toContain("<task-notification>")
        expect(wrapper.get("h1").text()).toBe("Fix the login bug")
    })

    it("re-reads the transcript when the setting changes", async () => {
        const fetchMock = vi.fn().mockResolvedValueOnce(json(view([typed]))).mockResolvedValue(json(collapsed))
        const wrapper = await mountSession(fetchMock)
        expect(wrapper.find("details").exists()).toBe(false)

        updateSettings({agentContext: "collapsed"})
        await flushPromises()

        expect(fetchMock.mock.calls[1][0]).toBe("/api/repos/alpha/sessions/claude/s-1?meta=collapsed")
        expect(wrapper.findAll("details.meta-record")).toHaveLength(3)
    })
})
