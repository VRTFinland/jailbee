import {flushPromises, mount} from "@vue/test-utils"
import {afterEach, describe, expect, it, vi} from "vitest"
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
        session_id: "s-new",
        agent: "claude",
        title: "Fix the login bug",
        last_activity: "2026-11-01T10:05:00Z",
        models: ["claude-opus-4", "claude-haiku-4"],
        container: "alpha-1",
    },
    {
        session_id: "s-old",
        agent: "claude",
        title: "",
        last_activity: null,
        models: [],
        container: null,
    },
]

async function mountRepo(fetchMock: ReturnType<typeof vi.fn<typeof fetch>>, dashboard?: Dashboard) {
    setToken("tok")
    vi.stubGlobal("fetch", withDashboardMode(fetchMock))
    const router = createAppRouter(createMemoryHistory())
    await router.push("/repos/alpha")
    await router.isReady()
    const wrapper = mount(RepoView, {global: {plugins: [router], provide: dashboard ? {[DASHBOARD]: dashboard} : {}}})
    await flushPromises()
    return {wrapper, router}
}

describe("RepoView", () => {
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
        expect(wrapper.get('[role="dialog"]').exists()).toBe(true)
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

    it("links a row to the session route", async () => {
        const {wrapper} = await mountRepo(vi.fn().mockResolvedValue(jsonResponse(200, {sessions})))
        expect(wrapper.findAll("li.session a")[0].attributes("href")).toBe(
            "/repos/alpha/sessions/claude/s-new",
        )
    })

    it("says so when there are no sessions", async () => {
        const {wrapper} = await mountRepo(vi.fn().mockResolvedValue(jsonResponse(200, {sessions: []})))
        expect(wrapper.text()).toContain("No sessions")
    })

    it("says the repository was not found on a 404", async () => {
        const {wrapper} = await mountRepo(vi.fn().mockResolvedValue(jsonResponse(404, {detail: "x"})))
        expect(wrapper.text()).toContain("Repository not found")
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
