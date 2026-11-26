import {config} from "@vue/test-utils"
config.global.stubs.DialogPortal = {template: "<slot />"}
import {flushPromises, mount} from "@vue/test-utils"
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest"
import {createMemoryHistory} from "vue-router"
import {setToken} from "../api"
import {createAppRouter} from "../router"
import {withDashboardMode} from "../test-http"
import {updateSettings} from "../settings"
import RepoView from "./RepoView.vue"
import {DASHBOARD, Dashboard} from "../dashboard"
import {orchestrationView} from "../test-fixtures"

function jsonResponse(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: {"Content-Type": "application/json"},
    })
}

const sessions = [
    {
        live: true,
        session_id: "s-new",
        agent: "claude",
        title: "Fix the login bug",
        last_activity: "2026-11-01T10:05:00Z",
        models: ["claude-opus-4", "claude-haiku-4"],
        container: "alpha-1",
    },
    {
        live: false,
        session_id: "s-old",
        agent: "claude",
        title: "",
        last_activity: null,
        models: [],
        container: null,
    },
]

async function mountRepo(fetchMock: ReturnType<typeof vi.fn<typeof fetch>>, dashboard?: Dashboard, from = "/") {
    setToken("tok")
    vi.stubGlobal("fetch", withDashboardMode(fetchMock))
    const router = createAppRouter(createMemoryHistory())
    await router.push(from)
    await router.push("/repos/alpha")
    await router.isReady()
    const wrapper = mount(RepoView, {global: {plugins: [router], provide: dashboard ? {[DASHBOARD]: dashboard} : {}}})
    await flushPromises()
    return {wrapper, router}
}

describe("RepoView", () => {
    beforeEach(() => updateSettings({liveSessionsOnly: false, openOnlyLiveSession: false}))

    it("shows only live sessions by default and allows all sessions for this visit", async () => {
        updateSettings({liveSessionsOnly: true})
        const {wrapper} = await mountRepo(vi.fn().mockResolvedValue(jsonResponse(200, {sessions})))
        expect(wrapper.findAll("li.session")).toHaveLength(1)
        await wrapper.get('[role="switch"][aria-label="Live only"]').trigger("click")
        expect(wrapper.findAll("li.session")).toHaveLength(2)
        wrapper.unmount()
    })

    it.each([0, 1, 2])("jumps only when exactly one repo session is live (%i)", async count => {
        updateSettings({liveSessionsOnly: true, openOnlyLiveSession: true})
        const rows = Array.from({length: count}, (_, i) => ({...sessions[0], session_id: `live-${i}`}))
        const {wrapper, router} = await mountRepo(vi.fn().mockResolvedValue(jsonResponse(200, {sessions: rows})))
        expect(router.currentRoute.value.name).toBe(count === 1 ? "session" : "repo")
        wrapper.unmount()
    })

    it("ignores the previous repository's late answer after switching repos", async () => {
        updateSettings({liveSessionsOnly: true, openOnlyLiveSession: true})
        let answer: (response: Response) => void = () => {}
        const {wrapper, router} = await mountRepo(vi.fn(input => String(input).includes("/alpha/")
            ? new Promise<Response>(resolve => {answer = resolve})
            : Promise.resolve(jsonResponse(200, {sessions: []}))))
        await router.push("/repos/beta")
        await flushPromises()
        answer(jsonResponse(200, {sessions}))
        await flushPromises()
        expect(router.currentRoute.value.path).toBe("/repos/beta")
        expect(wrapper.findAll("li.session")).toHaveLength(0)
        wrapper.unmount()
    })

    it("ignores a late response after leaving the repository", async () => {
        updateSettings({liveSessionsOnly: true, openOnlyLiveSession: true})
        let answer: (response: Response) => void = () => {}
        const {wrapper, router} = await mountRepo(vi.fn(() => new Promise<Response>(resolve => {answer = resolve})))
        await router.push("/settings")
        answer(jsonResponse(200, {sessions}))
        await flushPromises()
        expect(router.currentRoute.value.name).toBe("settings")
        wrapper.unmount()
    })

    it("does not jump from a subagent's repository breadcrumb", async () => {
        updateSettings({liveSessionsOnly: true, openOnlyLiveSession: true})
        const {wrapper, router} = await mountRepo(vi.fn().mockResolvedValue(jsonResponse(200, {sessions})),
            undefined, "/repos/alpha/sessions/claude/s-new/subagents/worker")
        expect(router.currentRoute.value.name).toBe("repo")
        wrapper.unmount()
    })

    it("does not bounce back when returning from the live session", async () => {
        updateSettings({liveSessionsOnly: true, openOnlyLiveSession: true})
        const {wrapper, router} = await mountRepo(vi.fn().mockResolvedValue(jsonResponse(200, {sessions})),
            undefined, "/repos/alpha/sessions/claude/s-new")
        expect(router.currentRoute.value.name).toBe("repo")
        wrapper.unmount()
    })

    afterEach(() => updateSettings({displayProfile: "normal"}))

    it("shows the repository's nested containers and updates when a child disappears", async () => {
        const dashboard = new Dashboard(() => ({start() {}, stop() {}, send() {}}))
        dashboard.state.view = orchestrationView()
        const {wrapper} = await mountRepo(vi.fn().mockResolvedValue(jsonResponse(200, {sessions})), dashboard)
        expect(wrapper.findAll("li.row").map(row => row.attributes("data-container"))).toEqual([
            "alpha-orch", "alpha-task-a", "alpha-task-b",
        ])
        expect(wrapper.findAll("li.nested")).toHaveLength(2)
        const next = orchestrationView()
        next.repos[0].containers.pop()
        next.repos[0].containers[0].child_count = 1
        dashboard.state.view = next
        await flushPromises()
        expect(wrapper.findAll("li.nested")).toHaveLength(1)
        expect(wrapper.text()).toContain("Orchestrator · 1 child")
    })

    it("offers page turns instead of scrolling in the e-ink profile", async () => {
        updateSettings({displayProfile: "eink"})
        const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, {sessions}))
        const {wrapper} = await mountRepo(fetchMock)
        expect(wrapper.find(".page-indicator").exists()).toBe(true)
    })

    it("does not show transcript metadata in the repository menu", async () => {
        const {wrapper} = await mountRepo(vi.fn().mockResolvedValue(jsonResponse(200, {sessions})))
        await wrapper.get('[aria-label="Quick settings"]').trigger("click")
        expect(wrapper.get('[role="dialog"]').attributes("aria-label")).toBe("Quick settings")
        expect(wrapper.find('[aria-label="Session"]').exists()).toBe(false)
        wrapper.unmount()
    })

    it("lists the repository's sessions with title, time and models", async () => {
        const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, {sessions}))
        const {wrapper} = await mountRepo(fetchMock)

        expect(fetchMock.mock.calls[0][0]).toBe("/api/repos/alpha/sessions")
        const rows = wrapper.findAll("li.session")
        expect(rows).toHaveLength(2)
        expect(rows[0].text()).toContain("Fix the login bug")
        expect(rows[0].text()).toContain("claude-opus-4")
        expect(rows[0].text()).toContain("claude-haiku-4")
        expect(rows[0].get("time").attributes("datetime")).toBe("2026-11-01T10:05:00Z")
        expect(rows[1].text()).toContain("Untitled session")
    })

    it("shows each session's container, or that it is unattributed", async () => {
        const {wrapper} = await mountRepo(vi.fn().mockResolvedValue(jsonResponse(200, {sessions})))
        const rows = wrapper.findAll("li.session")
        expect(rows[0].get(".container").text()).toBe("alpha-1")
        expect(rows[1].get(".container").text()).toBe("unattributed")
    })

    it("includes session metadata in link names just like ContainerView", async () => {
        const {wrapper} = await mountRepo(vi.fn().mockResolvedValue(jsonResponse(200, {sessions})))
        const links = wrapper.findAll("li.session a")
        expect(links[0].attributes("href")).toBe("/repos/alpha/sessions/claude/s-new")
        expect(links[0].attributes("aria-label")).toBe("Fix the login bug claude alpha-1 Nov 1, 2026, 12:05 PM claude-opus-4, claude-haiku-4")
        expect(links[1].attributes("aria-label")).toBe("Untitled session claude unattributed")
    })

    it("says so when there are no sessions", async () => {
        const {wrapper} = await mountRepo(vi.fn().mockResolvedValue(jsonResponse(200, {sessions: []})))
        expect(wrapper.text()).toContain("No sessions")
    })

    it("says the repository was not found on a 404", async () => {
        const {wrapper} = await mountRepo(vi.fn().mockResolvedValue(jsonResponse(404, {detail: "x"})))
        expect(wrapper.text()).toContain("Repository not found")
        expect(wrapper.get(".notice").element.tagName).toBe("P")
        expect(wrapper.get(".notice").classes()).toContain("ui-notice")
    })

    it("goes back to the front page", async () => {
        const {wrapper, router} = await mountRepo(
            vi.fn().mockResolvedValue(jsonResponse(200, {sessions})),
        )
        await wrapper.get('a[href="/"]').trigger("click")
        await flushPromises()
        expect(router.currentRoute.value.name).toBe("front")
    })
})
