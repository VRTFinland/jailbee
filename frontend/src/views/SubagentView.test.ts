import {flushPromises, mount} from "@vue/test-utils"
import {afterEach, describe, expect, it, vi} from "vitest"
import {createMemoryHistory} from "vue-router"
import {setToken} from "../api"
import type {TranscriptView} from "../protocol"
import {createAppRouter} from "../router"
import {withDashboardMode} from "../test-http"
import SessionView from "./SessionView.vue"

function jsonResponse(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: {"Content-Type": "application/json"},
    })
}

const parent: TranscriptView = {
    offset: 0,
    session_id: "s-1",
    agent: "claude",
    title: "Fix the login bug",
    last_activity: null,
    models: [],
    entries: [
        {
            kind: "tool_call",
            id: "c1",
            timestamp: null,
            call_id: "toolu_agent",
            name: "Agent",
            summary: "Agent: Review auth",
            input_html: "<pre>{}</pre>",
            model: null,
        },
        {
            kind: "tool_result",
            id: "r1",
            timestamp: null,
            call_id: "toolu_agent",
            html: "<pre>Looks fine</pre>",
            truncated: false,
            full_length: 10,
            is_error: false,
        },
        {
            kind: "subagent",
            id: "r1:agent",
            timestamp: null,
            call_id: "toolu_agent",
            agent_id: "a1b2c3",
            agent_type: "general-purpose",
            description: "Review auth",
        },
    ],
}

const child: TranscriptView = {
    offset: 0,
    session_id: "s-1",
    agent: "claude",
    title: "Review auth",
    last_activity: null,
    models: ["claude-haiku-5"],
    entries: [
        {kind: "user", id: "u1", timestamp: null, html: "<p>Review it.</p>", meta: false, label: null, expandable: false},
        {
            kind: "assistant",
            id: "a1",
            timestamp: null,
            html: "<p>Looks fine.</p>",
            model: "claude-haiku-5",
        },
    ],
}

async function mountAt(path: string, fetchMock: ReturnType<typeof vi.fn<typeof fetch>>) {
    setToken("tok")
    vi.stubGlobal("fetch", withDashboardMode(fetchMock))
    const router = createAppRouter(createMemoryHistory())
    await router.push(path)
    await router.isReady()
    const wrapper = mount(SessionView, {global: {plugins: [router]}, attachTo: document.body})
    await flushPromises()
    return {wrapper, router}
}

afterEach(() => {
    document.body.innerHTML = ""
})

describe("subagent transcripts", () => {
    it("lists a subagent under its call, linking to its own route", async () => {
        const {wrapper, router} = await mountAt(
            "/repos/alpha/sessions/claude/s-1",
            vi.fn().mockResolvedValue(jsonResponse(200, parent)),
        )
        const link = wrapper.get("a.subagent-link")
        expect(link.text()).toContain("Review auth")
        await link.trigger("click")
        await flushPromises()
        expect(router.currentRoute.value.name).toBe("subagent")
        expect(router.currentRoute.value.params).toMatchObject({
            prefix: "alpha",
            agent: "claude",
            sessionId: "s-1",
            agentId: "a1b2c3",
        })
    })

    it("fetches and renders the subagent transcript like a session", async () => {
        const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, child))
        const {wrapper} = await mountAt(
            "/repos/alpha/sessions/claude/s-1/subagents/a1b2c3",
            fetchMock,
        )
        expect(fetchMock.mock.calls[0][0]).toBe(
            "/api/repos/alpha/sessions/claude/s-1/subagents/a1b2c3?meta=hidden",
        )
        expect(wrapper.get("h1").text()).toBe("Review auth")
        expect(wrapper.get(".entry.assistant").html()).toContain("<p>Looks fine.</p>")
        expect(wrapper.get(".entry.assistant .model").text()).toBe("claude-haiku-5")
    })

    it("gets the parent breadcrumb from summaries without downloading its transcript", async () => {
        const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
            if (input === "/api/repos/alpha/sessions/claude/s-1/summary") return jsonResponse(200,
                {session_id: "s-1", agent: "claude", title: "Parent title"},
            )
            return jsonResponse(200, child)
        })
        const {wrapper} = await mountAt("/repos/alpha/sessions/claude/s-1/subagents/a1b2c3", fetchMock)
        expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
            "/api/repos/alpha/sessions/claude/s-1/subagents/a1b2c3?meta=hidden",
            "/api/repos/alpha/sessions/claude/s-1/summary",
        ])
        expect(wrapper.get('a[href="/repos/alpha/sessions/claude/s-1"]').text()).toBe("Parent title")
        wrapper.unmount()
    })

    it("shows the subagent metadata in its menu", async () => {
        const {wrapper} = await mountAt(
            "/repos/alpha/sessions/claude/s-1/subagents/a1b2c3",
            vi.fn().mockResolvedValue(jsonResponse(200, child)),
        )
        await wrapper.get('[aria-label="Quick settings"]').trigger("click")
        expect(wrapper.get('[aria-label="Session"]').text()).toContain("claude")
        expect(wrapper.get('[aria-label="Session"]').text()).toContain("claude-haiku-5")
        expect(wrapper.get('[aria-label="Session"]').text()).toContain("s-1")
        wrapper.unmount()
    })

    it("goes back to the parent session", async () => {
        const {wrapper, router} = await mountAt(
            "/repos/alpha/sessions/claude/s-1/subagents/a1b2c3",
            vi.fn().mockResolvedValue(jsonResponse(200, child)),
        )
        await wrapper.get('a[href="/repos/alpha/sessions/claude/s-1"]').trigger("click")
        await flushPromises()
        expect(router.currentRoute.value.name).toBe("session")
        expect(router.currentRoute.value.params.sessionId).toBe("s-1")
    })

    it("says the subagent was not found on a 404", async () => {
        const {wrapper} = await mountAt(
            "/repos/alpha/sessions/claude/s-1/subagents/a1b2c3",
            vi.fn().mockResolvedValue(jsonResponse(404, {detail: "x"})),
        )
        expect(wrapper.text()).toContain("Subagent not found")
    })
})
