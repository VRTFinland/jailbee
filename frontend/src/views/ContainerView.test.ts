import {flushPromises, mount} from "@vue/test-utils"
import {describe, expect, it, vi} from "vitest"
import {createMemoryHistory} from "vue-router"
import {setToken} from "../api"
import {createAppRouter} from "../router"
import ContainerView from "./ContainerView.vue"
import {DASHBOARD, Dashboard} from "../dashboard"
import {orchestrationView} from "../test-fixtures"

function jsonResponse(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: {"Content-Type": "application/json"},
    })
}

const list = {
    container: "alpha-1",
    repo: "alpha",
    sessions: [
        {
            session_id: "s-live",
            agent: "claude",
            live: true,
            title: "Fix the login bug",
            last_activity: "2026-11-01T10:05:00Z",
            models: ["claude-opus-4"],
        },
        {
            session_id: "s-past",
            agent: "claude",
            live: false,
            title: "",
            last_activity: null,
            models: [],
        },
    ],
}

async function mountContainer(fetchMock: ReturnType<typeof vi.fn>, dashboard?: Dashboard, name = "alpha-1") {
    setToken("tok")
    vi.stubGlobal("fetch", fetchMock)
    const router = createAppRouter(createMemoryHistory())
    await router.push(`/containers/${name}`)
    await router.isReady()
    const wrapper = mount(ContainerView, {global: {plugins: [router], provide: dashboard ? {[DASHBOARD]: dashboard} : {}}})
    await flushPromises()
    return {wrapper, router}
}

describe("ContainerView", () => {
    it("links an orchestrator's children and updates the list live", async () => {
        const dashboard = new Dashboard(() => ({start() {}, stop() {}, send() {}}))
        dashboard.state.view = orchestrationView()
        const {wrapper} = await mountContainer(vi.fn().mockResolvedValue(jsonResponse(200, list)), dashboard, "alpha-orch")
        expect(wrapper.findAll(".children a").map(a => a.attributes("href"))).toEqual([
            "/containers/alpha-task-a", "/containers/alpha-task-b",
        ])
        expect(wrapper.get(".children").text()).toContain("working · codex/opus")
        dashboard.state.view = {...orchestrationView(), repos: [{prefix: "alpha", containers: []}]}
        await flushPromises()
        expect(wrapper.findAll(".children a")).toHaveLength(0)
    })

    it("links a child back to its orchestrator, even when that row is absent", async () => {
        const dashboard = new Dashboard(() => ({start() {}, stop() {}, send() {}}))
        const view = orchestrationView()
        view.repos[0].containers.shift()
        dashboard.state.view = view
        const {wrapper} = await mountContainer(vi.fn().mockResolvedValue(jsonResponse(200, list)), dashboard, "alpha-task-a")
        expect(wrapper.get(".parent a").attributes("href")).toBe("/containers/alpha-orch")
        expect(wrapper.get(".parent").text()).toContain("Child of orch · working · codex/opus")
    })

    it("loads the selected child's sessions when following a relationship link", async () => {
        const dashboard = new Dashboard(() => ({start() {}, stop() {}, send() {}}))
        dashboard.state.view = orchestrationView()
        const fetchMock = vi.fn()
            .mockResolvedValueOnce(jsonResponse(200, list))
            .mockResolvedValueOnce(jsonResponse(200, {...list, sessions: []}))
        const {wrapper} = await mountContainer(fetchMock, dashboard, "alpha-orch")
        await wrapper.get(".children a").trigger("click")
        await flushPromises()
        expect(fetchMock.mock.calls.map(call => call[0])).toEqual([
            "/api/containers/alpha-orch/sessions", "/api/containers/alpha-task-a/sessions",
        ])
        expect(wrapper.text()).not.toContain("Fix the login bug")
        expect(wrapper.text()).toContain("No sessions")
    })

    it("lists live sessions first, marked live, then the recorded ones", async () => {
        const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, list))
        const {wrapper} = await mountContainer(fetchMock)

        expect(fetchMock.mock.calls[0][0]).toBe("/api/containers/alpha-1/sessions")
        const rows = wrapper.findAll("li.session")
        expect(rows).toHaveLength(2)
        expect(rows[0].text()).toContain("Fix the login bug")
        expect(rows[0].find(".live").exists()).toBe(true)
        expect(rows[1].text()).toContain("Untitled session")
        expect(rows[1].find(".live").exists()).toBe(false)
    })

    it("opens a row's transcript through the session route", async () => {
        const {wrapper} = await mountContainer(vi.fn().mockResolvedValue(jsonResponse(200, list)))
        expect(wrapper.findAll("li.session a")[0].attributes("href")).toBe(
            "/repos/alpha/sessions/claude/s-live",
        )
    })

    it("says so when the container has no sessions", async () => {
        const empty = {...list, sessions: []}
        const {wrapper} = await mountContainer(vi.fn().mockResolvedValue(jsonResponse(200, empty)))
        expect(wrapper.text()).toContain("No sessions")
    })

    it("says the container was not found on a 404", async () => {
        const {wrapper} = await mountContainer(
            vi.fn().mockResolvedValue(jsonResponse(404, {detail: "x"})),
        )
        expect(wrapper.text()).toContain("Container not found")
    })
})
