import {mount} from "@vue/test-utils"
import {describe, expect, it} from "vitest"
import {createMemoryHistory} from "vue-router"
import {createAppRouter} from "../router"
import type {FrontPageView} from "../protocol"
import RepoList from "./RepoList.vue"

const view: FrontPageView = {
    seq: 1,
    gathered_at: "2026-11-21T12:00:00Z",
    columns: [
        {name: "name", header: "NAME"},
        {name: "state", header: "ST"},
        {name: "agent_compact", header: "AI"},
        {name: "pr", header: "PR"},
    ],
    repos: [
        {
            prefix: "alpha",
            containers: [
                {name: "alpha-1", indent: "", agent: "waiting", state: "Running", phase: "booting", depth: 0, orchestrator: false, child_count: 0, child: null, cells: ["1", "▶", "◆ 5m", ""]},
                {name: "alpha-2", indent: "└ ", agent: null, state: "Stopped", phase: null, depth: 0, orchestrator: false, child_count: 0, child: null, cells: ["2", "■", "—", "#7"]},
            ],
        },
        {prefix: "empty", containers: []},
    ],
}

function mountList() {
    return mount(RepoList, {props: {view}, global: {plugins: [createAppRouter(createMemoryHistory())]}})
}

describe("RepoList", () => {
    it("visibly nests children and shows orchestration facts without colour", async () => {
        const wrapper = mountList()
        const parent = {...view.repos[0].containers[0], orchestrator: true, child_count: 2}
        const child = {...view.repos[0].containers[1], depth: 1,
            child: {orchestrator: parent.name, orchestrator_short_name: "1", state: "done", runner: "codex/opus"}}
        await wrapper.setProps({view: {...view, repos: [{prefix: "alpha", containers: [parent, child]}]}})
        const rows = wrapper.findAll("li.row")
        expect(rows[0].text()).toContain("Orchestrator · 2 children")
        expect(rows[1].classes()).toContain("nested")
        expect(rows[1].text()).toContain("Child of 1 · done · codex/opus")
        expect(rows[1].get(".orchestration a").attributes("href")).toBe("/containers/alpha-1")
    })

    it("keeps an absent orchestrator's child visible with its short name", async () => {
        const wrapper = mountList()
        const orphan = {...view.repos[0].containers[1], child: {
            orchestrator: "alpha-missing-parent", orchestrator_short_name: "missing-parent", state: "working", runner: null,
        }}
        await wrapper.setProps({view: {...view, repos: [{prefix: "alpha", containers: [orphan]}]}})
        const row = wrapper.get("li.row")
        expect(row.classes()).not.toContain("nested")
        expect(row.text()).toContain("Child of missing-parent · working")
        expect(row.text()).toContain("orchestrator not listed")
    })

    it("labels a hidden orchestrator without creating a link", async () => {
        const wrapper = mountList()
        const child = {...view.repos[0].containers[1], child: {
            orchestrator: null, orchestrator_short_name: "hidden orchestrator", state: "working", runner: null,
        }}
        await wrapper.setProps({view: {...view, repos: [{prefix: "alpha", containers: [child]}]}})
        expect(wrapper.get(".orchestration").text()).toContain("Child of hidden orchestrator")
        expect(wrapper.find(".orchestration a").exists()).toBe(false)
    })

    it("groups containers under a heading per repository", () => {
        const wrapper = mountList()
        const repos = wrapper.findAll("section.repo")
        expect(repos.map((r) => r.get("h2").text())).toEqual(["alpha", "empty"])
        expect(repos[0].findAll("li").map((li) => li.attributes("data-container"))).toEqual([
            "alpha-1",
            "alpha-2",
        ])
        expect(repos[1].text()).toContain("No containers.")
    })

    it("links each repository heading to its session list", () => {
        const links = mountList().findAll("h2 a")
        expect(links.map((a) => a.attributes("href"))).toEqual(["/repos/alpha", "/repos/empty"])
    })

    it("links each container's name to its container view", () => {
        const links = mountList().findAll("li.row a.name")
        expect(links.map((a) => a.attributes("href"))).toEqual([
            "/containers/alpha-1",
            "/containers/alpha-2",
        ])
    })

    it("marks the agent state so a waiting agent stands out", () => {
        const wrapper = mountList()
        const rows = wrapper.findAll("li.row")
        expect(rows[0].attributes("data-agent")).toBe("waiting")
        expect(rows[0].get(".agent").text()).toBe("waiting for you")
        expect(rows[1].attributes("data-agent")).toBeUndefined()
        expect(rows[1].find(".agent").exists()).toBe(false)
    })

    it("marks each row with its row phase and state for the theme to style", () => {
        const rows = mountList().findAll("li.row")
        expect(rows[0].attributes("data-phase")).toBe("booting")
        expect(rows[0].attributes("data-state")).toBe("Running")
        expect(rows[1].attributes("data-phase")).toBeUndefined()
        expect(rows[1].attributes("data-state")).toBe("Stopped")
    })

    it("names the phase in text so it does not rely on colour (e-ink)", () => {
        const rows = mountList().findAll("li.row")
        expect(rows[0].get(".phase").text()).toBe("booting")
        expect(rows[1].find(".phase").exists()).toBe(false)
    })

    it("preserves sync padding in a preformatted value", () => {
        const syncView: FrontPageView = {
            ...view,
            columns: [{name: "name", header: "NAME"}, {name: "sync", header: "SYNC"}],
            repos: [{prefix: "alpha", containers: [
                {...view.repos[0].containers[0], cells: ["1", "    ↑  1 ↓123 +1 -0"]},
                {...view.repos[0].containers[1], cells: ["2", "pr  ↑123 ↓  2 +42 -7"]},
            ]}],
        }
        const wrapper = mount(RepoList, {props: {view: syncView}, global: {plugins: [createAppRouter(createMemoryHistory())]}})
        expect(wrapper.findAll("dd pre").map((value) => value.element.textContent)).toEqual([
            "    ↑  1 ↓123 +1 -0", "pr  ↑123 ↓  2 +42 -7",
        ])
    })

    it("shows the core's cells under their headers and skips empty ones", () => {
        const wrapper = mountList()
        const rows = wrapper.findAll("li.row")
        expect(rows[0].get(".name").text()).toBe("1")
        expect(rows[0].findAll(".cell").map((c) => c.text())).toEqual(["ST▶", "AI◆ 5m"])
        expect(rows[1].findAll(".cell").map((c) => c.text())).toEqual(["ST■", "AI—", "PR#7"])
    })
})
