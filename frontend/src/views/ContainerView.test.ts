import {flushPromises, mount} from "@vue/test-utils"
import {describe, expect, it, vi} from "vitest"
import {createMemoryHistory} from "vue-router"
import {setToken} from "../api"
import {createAppRouter} from "../router"
import {withDashboardMode} from "../test-http"
import ContainerView from "./ContainerView.vue"
import {DASHBOARD, Dashboard} from "../dashboard"
import {orchestrationView} from "../test-fixtures"

function jsonResponse(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: {"Content-Type": "application/json"},
    })
}

const liveList = {
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
    ],
}

const historyList = {
    ...liveList,
    total: 1,
    limit: 20,
    offset: 0,
    sessions: [
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

/** A fetch that answers the live and the history request with these bodies. */
function serve(live: unknown = liveList, history: unknown = historyList, status = 200) {
    return vi.fn<typeof fetch>((input) => Promise.resolve(
        jsonResponse(status, String(input).includes("/sessions/history?") ? history : live),
    ))
}

async function mountContainer(fetchMock: ReturnType<typeof vi.fn<typeof fetch>>, dashboard?: Dashboard, name = "alpha-1") {
    setToken("tok")
    vi.stubGlobal("fetch", withDashboardMode(fetchMock))
    const router = createAppRouter(createMemoryHistory())
    await router.push(`/containers/${name}`)
    await router.isReady()
    const wrapper = mount(ContainerView, {global: {plugins: [router], provide: dashboard ? {[DASHBOARD]: dashboard} : {}}})
    await flushPromises()
    return {wrapper, router}
}

describe("ContainerView", () => {
    it("pages earlier sessions on the server and leaves live sessions on top", async () => {
        const fetchMock = vi.fn<typeof fetch>((input) => {
            const url = new URL(String(input), "http://localhost")
            const offset = Number(url.searchParams.get("offset"))
            return Promise.resolve(jsonResponse(200, url.pathname.endsWith("/history")
                ? {...historyList, total: 21, limit: 20, offset, sessions: [{...historyList.sessions[0], title: offset ? "Older page" : "Newest page"}]}
                : liveList))
        })
        const {wrapper} = await mountContainer(fetchMock)
        expect(wrapper.text()).toContain("Page 1 of 2")
        expect(wrapper.get('button[aria-label="Previous earlier sessions"]').attributes("disabled")).toBeDefined()
        await wrapper.get('button[aria-label="Next earlier sessions"]').trigger("click")
        await flushPromises()
        expect(String(fetchMock.mock.calls.at(-1)![0])).toContain("limit=20&offset=20")
        expect(wrapper.text()).toContain("Page 2 of 2")
        expect(wrapper.findAll("li.session")[0].text()).toContain("Fix the login bug")
        expect(wrapper.text()).toContain("Older page")
        expect(wrapper.text()).not.toContain("Newest page")
        expect(wrapper.get('button[aria-label="Next earlier sessions"]').attributes("disabled")).toBeDefined()
        await wrapper.get('button[aria-label="Previous earlier sessions"]').trigger("click")
        await flushPromises()
        expect(wrapper.text()).toContain("Newest page")
        expect(fetchMock.mock.calls.filter(call => String(call[0]).endsWith("/sessions"))).toHaveLength(1)
        wrapper.unmount()
    })

    it.each([21, 0])("clamps and refetches when history shrinks to %i sessions", async (remaining) => {
        let total = 41
        let holdLast: (r: Response) => void = () => {}
        const fetchMock = vi.fn<typeof fetch>((input) => {
            const url = new URL(String(input), "http://localhost")
            if (!url.pathname.endsWith("/history")) return Promise.resolve(jsonResponse(200, {...liveList, sessions: []}))
            const offset = Number(url.searchParams.get("offset"))
            if (offset === 40) return new Promise<Response>(resolve => {holdLast = resolve})
            return Promise.resolve(jsonResponse(200, {...historyList, total, offset,
                sessions: total ? [{...historyList.sessions[0], title: offset ? "Last remaining page" : "Newest page"}] : []}))
        })
        const {wrapper} = await mountContainer(fetchMock)
        await wrapper.get('button[aria-label="Next earlier sessions"]').trigger("click")
        await flushPromises()
        await wrapper.get('button[aria-label="Next earlier sessions"]').trigger("click")
        await flushPromises()
        total = remaining
        holdLast(jsonResponse(200, {...historyList, total, offset: 40, sessions: []}))
        await flushPromises()
        expect(String(fetchMock.mock.calls.at(-1)![0])).toContain(`offset=${remaining ? 20 : 0}`)
        expect(wrapper.text()).toContain(remaining ? "Page 2 of 2" : "Page 1 of 1")
        if (remaining) {
            expect(wrapper.text()).toContain("Last remaining page")
            expect(wrapper.text()).not.toContain("No sessions.")
        } else expect(wrapper.text()).toContain("No sessions.")
        expect(wrapper.get('button[aria-label="Next earlier sessions"]').attributes("disabled")).toBeDefined()
        wrapper.unmount()
    })

    it("debounces server search, resets the history page and shows no matches without hiding live sessions", async () => {
        const fetchMock = vi.fn<typeof fetch>((input) => {
            const url = new URL(String(input), "http://localhost")
            const searching = url.searchParams.has("q")
            return Promise.resolve(jsonResponse(200, url.pathname.endsWith("/history")
                ? {...historyList, total: searching ? 0 : 21, sessions: searching ? [] : historyList.sessions}
                : liveList))
        })
        const {wrapper} = await mountContainer(fetchMock)
        await wrapper.get('button[aria-label="Next earlier sessions"]').trigger("click")
        await flushPromises()
        const calls = fetchMock.mock.calls.length
        vi.useFakeTimers()
        try {
            const search = wrapper.get('input[type="search"]')
            await search.setValue("no")
            await vi.advanceTimersByTimeAsync(200)
            await search.setValue("no & matches")
            await vi.advanceTimersByTimeAsync(299)
            expect(fetchMock).toHaveBeenCalledTimes(calls)
            await vi.advanceTimersByTimeAsync(1)
            await flushPromises()
            const url = new URL(String(fetchMock.mock.calls.at(-1)![0]), "http://localhost")
            expect(url.searchParams.get("q")).toBe("no & matches")
            expect(url.searchParams.get("offset")).toBe("0")
            expect(fetchMock).toHaveBeenCalledTimes(calls + 1)
            expect(wrapper.text()).toContain("No matches")
            expect(wrapper.text()).toContain("Page 1 of 1")
            expect(wrapper.findAll("li.session")).toHaveLength(1)
            expect(wrapper.text()).toContain("Fix the login bug")
            await search.setValue("")
            await vi.advanceTimersByTimeAsync(300)
            await flushPromises()
            expect(wrapper.text()).not.toContain("No matches")
            expect(wrapper.findAll("li.session")).toHaveLength(2)
        } finally {
            wrapper.unmount()
            vi.useRealTimers()
        }
    })

    it("ignores an old search response after the search is cleared", async () => {
        let answerSearch: (r: Response) => void = () => {}
        const fetchMock = vi.fn<typeof fetch>((input) => String(input).includes("q=old")
            ? new Promise<Response>(resolve => { answerSearch = resolve })
            : serve()(input))
        const {wrapper} = await mountContainer(fetchMock)
        vi.useFakeTimers()
        try {
            const search = wrapper.get('input[type="search"]')
            await search.setValue("old")
            await vi.advanceTimersByTimeAsync(300)
            expect(wrapper.text()).toContain("Loading earlier sessions")
            await search.setValue("")
            await vi.advanceTimersByTimeAsync(300)
            await flushPromises()
            answerSearch(jsonResponse(200, {...historyList, total: 0, sessions: []}))
            await flushPromises()
            expect(wrapper.findAll("li.session")).toHaveLength(2)
            expect(wrapper.text()).not.toContain("No matches")
        } finally {
            wrapper.unmount()
            vi.useRealTimers()
        }
    })

    it("resets search and pagination when navigating to another container", async () => {
        const fetchMock = serve(liveList, {...historyList, total: 21})
        const {wrapper, router} = await mountContainer(fetchMock)
        await wrapper.get('button[aria-label="Next earlier sessions"]').trigger("click")
        await flushPromises()
        vi.useFakeTimers()
        try {
            await wrapper.get('input[type="search"]').setValue("unfinished")
            await router.push("/containers/alpha-2")
            await flushPromises()
            await vi.advanceTimersByTimeAsync(300)
            await flushPromises()
            expect((wrapper.get('input[type="search"]').element as HTMLInputElement).value).toBe("")
            expect(String(fetchMock.mock.calls.at(-1)![0])).toBe("/api/containers/alpha-2/sessions/history?limit=20&offset=0")
            expect(wrapper.text()).toContain("Page 1 of 2")
        } finally {
            wrapper.unmount()
            vi.useRealTimers()
        }
    })

    it("links an orchestrator's children and updates the list live", async () => {
        const dashboard = new Dashboard(() => ({start() {}, stop() {}, send() {}}))
        dashboard.state.view = orchestrationView()
        const {wrapper} = await mountContainer(serve(), dashboard, "alpha-orch")
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
        const {wrapper} = await mountContainer(serve(), dashboard, "alpha-task-a")
        expect(wrapper.get(".parent a").attributes("href")).toBe("/containers/alpha-orch")
        expect(wrapper.get(".parent").text()).toContain("Child of orch · working · codex/opus")
        expect(wrapper.get(".parent").text()).toContain("orchestrator not listed")
    })

    it("loads the selected child's sessions when following a relationship link", async () => {
        const dashboard = new Dashboard(() => ({start() {}, stop() {}, send() {}}))
        dashboard.state.view = orchestrationView()
        const fetchMock = serve()
        const {wrapper} = await mountContainer(fetchMock, dashboard, "alpha-orch")
        fetchMock.mockImplementation(() => Promise.resolve(jsonResponse(200, {...liveList, sessions: []})))
        await wrapper.get(".children a").trigger("click")
        await flushPromises()
        expect(fetchMock.mock.calls.map(call => call[0])).toEqual([
            "/api/containers/alpha-orch/sessions",
            "/api/containers/alpha-orch/sessions/history?limit=20&offset=0",
            "/api/containers/alpha-task-a/sessions",
            "/api/containers/alpha-task-a/sessions/history?limit=20&offset=0",
        ])
        expect(wrapper.text()).not.toContain("Fix the login bug")
        expect(wrapper.text()).toContain("No sessions")
    })

    it("lists live sessions first, marked live, then the recorded ones", async () => {
        const fetchMock = serve()
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
        const {wrapper} = await mountContainer(serve())
        expect(wrapper.findAll("li.session a")[0].attributes("href")).toBe(
            "/repos/alpha/sessions/claude/s-live",
        )
    })

    it("says so when the container has no sessions", async () => {
        const empty = {...liveList, sessions: []}
        const {wrapper} = await mountContainer(serve(empty, empty))
        expect(wrapper.text()).toContain("No sessions")
    })

    it("says the container was not found on a 404", async () => {
        const {wrapper} = await mountContainer(serve({detail: "x"}, {detail: "x"}, 404))
        expect(wrapper.text()).toContain("Container not found")
    })

    it("shows the live sessions while the history is still loading", async () => {
        let answerHistory: (r: Response) => void = () => {}
        const fetchMock = vi.fn<typeof fetch>((url) => String(url).includes("/sessions/history?")
            ? new Promise<Response>(resolve => { answerHistory = resolve })
            : Promise.resolve(jsonResponse(200, liveList)))
        const {wrapper} = await mountContainer(fetchMock)

        expect(wrapper.findAll("li.session")).toHaveLength(1)
        expect(wrapper.text()).toContain("Fix the login bug")
        expect(wrapper.text()).toContain("Loading earlier sessions…")
        expect(wrapper.text()).not.toContain("No sessions")

        const scroll = vi.spyOn(window, "scrollTo").mockImplementation(() => {})
        scroll.mockClear()
        answerHistory(jsonResponse(200, historyList))
        await flushPromises()
        expect(scroll).not.toHaveBeenCalled()
        scroll.mockRestore()
        expect(wrapper.findAll("li.session")).toHaveLength(2)
        expect(wrapper.text()).not.toContain("Loading earlier sessions…")
    })

    it("keeps the live sessions when the history fails", async () => {
        const fetchMock = vi.fn<typeof fetch>((url) => Promise.resolve(String(url).includes("/sessions/history?")
            ? jsonResponse(500, {detail: "x"})
            : jsonResponse(200, liveList)))
        const {wrapper} = await mountContainer(fetchMock)

        expect(wrapper.findAll("li.session")).toHaveLength(1)
        expect(wrapper.text()).toContain("Earlier sessions could not be loaded.")
    })

    it("shows a session in both answers once, as live", async () => {
        const both = {...historyList, sessions: [{...liveList.sessions[0], live: false}, ...historyList.sessions]}
        const {wrapper} = await mountContainer(serve(liveList, both))

        const rows = wrapper.findAll("li.session")
        expect(rows).toHaveLength(2)
        expect(rows[0].find(".live").exists()).toBe(true)
        expect(rows[1].text()).toContain("Untitled session")
    })
})
