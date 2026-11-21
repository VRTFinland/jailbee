import {flushPromises, mount} from "@vue/test-utils"
import {describe, expect, it, vi} from "vitest"
import {createMemoryHistory} from "vue-router"
import {setToken} from "../api"
import {createAppRouter} from "../router"
import RepoView from "./RepoView.vue"

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
    },
    {session_id: "s-old", agent: "claude", title: "", last_activity: null, models: []},
]

async function mountRepo(fetchMock: ReturnType<typeof vi.fn>) {
    setToken("tok")
    vi.stubGlobal("fetch", fetchMock)
    const router = createAppRouter(createMemoryHistory())
    await router.push("/repos/alpha")
    await router.isReady()
    const wrapper = mount(RepoView, {global: {plugins: [router]}})
    await flushPromises()
    return {wrapper, router}
}

describe("RepoView", () => {
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
        await wrapper.get("a.back").trigger("click")
        await flushPromises()
        expect(router.currentRoute.value.name).toBe("front")
    })
})
