import {mount} from "@vue/test-utils"
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest"
import {nextTick} from "vue"
import {createMemoryHistory} from "vue-router"
import {DASHBOARD, Dashboard, type SocketLike} from "../dashboard"
import type {FrontPageView} from "../protocol"
import {PROTOCOL_VERSION} from "../protocol"
import {createAppRouter} from "../router"
import {reloadSettings, updateSettings} from "../settings"
import type {SocketHandlers} from "../socket"
import FrontPage from "./FrontPage.vue"
import {orchestrationView} from "../test-fixtures"

function viewWith(name: string, seq: number): FrontPageView {
    return {
        seq,
        gathered_at: "2026-11-21T12:00:00Z",
        columns: [{name: "name", header: "NAME"}],
        repos: [{prefix: "alpha", containers: [{name, indent: "", agent: null, state: "Running", phase: null, depth: 0, orchestrator: false, child_count: 0, child: null, cells: [name]}]}],
    }
}

function setup(refreshMs = 60_000) {
    let handlers: SocketHandlers | null = null
    const dashboard = new Dashboard((h): SocketLike => {
        handlers = h
        return {start() {}, stop() {}, send() {}}
    })
    dashboard.start()
    const push = (view: FrontPageView) => {
        handlers!.onMessage({type: "front_page", v: PROTOCOL_VERSION, view})
    }
    push(viewWith("alpha-1", 1))
    const wrapper = mount(FrontPage, {
        props: {refreshMs},
        global: {
            plugins: [createAppRouter(createMemoryHistory())],
            provide: {[DASHBOARD]: dashboard},
        },
    })
    return {wrapper, push}
}

describe("FrontPage", () => {
    beforeEach(() => {
        vi.useFakeTimers()
        vi.stubGlobal("matchMedia", () => ({matches: false, addEventListener() {}, removeEventListener() {}}))
        reloadSettings()
    })
    afterEach(() => {
        updateSettings({displayProfile: "normal"})
        vi.useRealTimers()
    })

    it.each([true, false])("reserves the title bar and banner on page turns (title visible: %s)", async (visible) => {
        updateSettings({displayProfile: "eink", showTitleBar: visible, pageStep: 100})
        vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
            const bottom = this.classList.contains("title-bar") ? 74 : this.classList.contains("outdated") ? 30 : 0
            return {top: 0, bottom, height: bottom, width: 0, left: 0, right: 0, x: 0, y: 0, toJSON() {}}
        })
        vi.spyOn(window, "innerHeight", "get").mockReturnValue(800)
        vi.spyOn(document.documentElement, "scrollHeight", "get").mockReturnValue(4000)
        const scroll = vi.spyOn(window, "scrollTo").mockImplementation(() => {})
        const banner = document.createElement("aside")
        banner.className = "outdated"
        document.body.append(banner)
        const {wrapper} = setup()
        try {
            await nextTick()
            window.dispatchEvent(new KeyboardEvent("keydown", {key: "m"}))
            expect(scroll).toHaveBeenLastCalledWith(0, visible ? 726 : 770)
        } finally {
            wrapper.unmount()
            banner.remove()
            vi.restoreAllMocks()
            updateSettings({showTitleBar: true})
        }
    })

    it("re-nests an orchestrator and two children on live snapshots without reloading", async () => {
        const {wrapper, push} = setup()
        push(orchestrationView())
        await nextTick()
        expect(wrapper.findAll("li.row").map(row => row.attributes("data-container"))).toEqual([
            "alpha-orch", "alpha-task-a", "alpha-task-b",
        ])
        expect(wrapper.findAll("li.nested")).toHaveLength(2)
        const next = orchestrationView()
        next.seq = 2
        next.repos[0].containers.pop()
        next.repos[0].containers[0].child_count = 1
        push(next)
        await nextTick()
        expect(wrapper.findAll("li.nested")).toHaveLength(1)
        expect(wrapper.text()).not.toContain("task-b")
        expect(wrapper.text()).toContain("Orchestrator · 1 child")
    })

    it("shows every snapshot at once in the normal profile", async () => {
        const {wrapper, push} = setup()
        push(viewWith("alpha-2", 2))
        await nextTick()
        expect(wrapper.text()).toContain("alpha-2")
        expect(wrapper.find("button.refresh").exists()).toBe(false)
    })

    it("e-ink: a new snapshot is not shown until the interval passes", async () => {
        updateSettings({displayProfile: "eink"})
        const {wrapper, push} = setup(60_000)
        expect(wrapper.text()).toContain("alpha-1")
        push(viewWith("alpha-2", 2))
        await nextTick()
        expect(wrapper.text()).not.toContain("alpha-2")

        await vi.advanceTimersByTimeAsync(59_000)
        expect(wrapper.text()).not.toContain("alpha-2")
        await vi.advanceTimersByTimeAsync(1_000)
        expect(wrapper.text()).toContain("alpha-2")
        expect(wrapper.text()).not.toContain("alpha-1")
    })

    it("e-ink: the refresh button shows the latest snapshot on demand", async () => {
        updateSettings({displayProfile: "eink"})
        const {wrapper, push} = setup()
        push(viewWith("alpha-2", 2))
        await nextTick()
        await wrapper.get("button.refresh").trigger("click")
        expect(wrapper.text()).toContain("alpha-2")
    })

    it("e-ink: stops the refresh timer when the profile goes back to normal", async () => {
        updateSettings({displayProfile: "eink"})
        const {wrapper, push} = setup()
        updateSettings({displayProfile: "normal"})
        await nextTick()
        push(viewWith("alpha-3", 3))
        await nextTick()
        expect(wrapper.text()).toContain("alpha-3")
        expect(vi.getTimerCount()).toBe(0)
    })
})
