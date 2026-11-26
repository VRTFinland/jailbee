import {mount} from "@vue/test-utils"
import {defineComponent} from "vue"
import QuickSettings from "./QuickSettings.vue"
import {beforeEach, describe, expect, it} from "vitest"
import {reloadSettings} from "../settings"
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
    return mount(defineComponent({
        components: {QuickSettings, RepoList}, props: ["view", "listedContainers"],
        template: '<QuickSettings context="containers" /><RepoList :view="view" :listed-containers="listedContainers" />',
    }), {props: {view}, global: {plugins: [createAppRouter(createMemoryHistory())]}})
}

describe("RepoList", () => {
    beforeEach(() => reloadSettings())

    it("keeps a row phase over a stopped state and renders its words with a vector icon", async () => {
        const wrapper = mountList()
        const stopped = {...view.repos[0].containers[0], state: "Stopped", phase: "booting"}
        await wrapper.setProps({view: {...view, repos: [{prefix: "alpha", containers: [stopped]}]}})
        const row = wrapper.get('[data-container="alpha-1"]')
        const phase = row.get('[data-status="booting"]')
        expect(phase.text()).toBe("booting")
        expect(phase.find('svg[aria-hidden="true"]').exists()).toBe(true)
        expect(row.find('[data-status="stopped"]').exists()).toBe(false)
        expect(row.get('[data-status="waiting"]').text()).toBe("waiting for you")
        expect(row.get("a").attributes("href")).toBe("/containers/alpha-1")
    })

    it("preserves the absence of a separate Running or Frozen state label without a phase", async () => {
        const wrapper = mountList()
        for (const state of ["Running", "Frozen"]) {
            await wrapper.setProps({view: {...view, repos: [{prefix: "alpha", containers: [
                {...view.repos[0].containers[0], state, phase: null, agent: null},
            ]}]}})
            expect(wrapper.find(".phase").exists()).toBe(false)
            expect(wrapper.get(".cell").text()).toBe("ST▶")
        }
    })

    it("switches persisted filters, dims context, hides empty repos and follows live membership", async () => {
        const wrapper = mountList()
        const parent = {...view.repos[0].containers[0], agent: null, state: "Stopped"}
        const child = {...view.repos[0].containers[1], depth: 1, state: "Running", agent: "busy"}
        const filteredView = {...view, repos: [{prefix: "alpha", containers: [parent, child]}, view.repos[1]],
            filters: {running: {visible: [child.name, parent.name], context: [parent.name]},
                active: {visible: [child.name, parent.name], context: [parent.name]}}}
        await wrapper.setProps({view: filteredView})
        await wrapper.get('button[aria-label="Quick settings"]').trigger("click")
        await wrapper.get('button[aria-label="Active"]').trigger("click")
        expect(wrapper.get('button[aria-label="Active"]').attributes("aria-pressed")).toBe("true")
        expect(wrapper.findAll("section.repo")).toHaveLength(1)
        expect(wrapper.findAll("li.row")[0].classes()).toContain("filter-context")
        expect(wrapper.findAll("li.row")[1].classes()).toContain("nested")
        expect(localStorage.getItem("jailbee.containerFilter")).toBe("active")
        await wrapper.setProps({view: {...filteredView, filters: {...filteredView.filters,
            active: {visible: [], context: []}}}})
        expect(wrapper.findAll("li.row")).toHaveLength(0)
        expect(wrapper.get('.filter-notice').text()).toBe("Showing: Active · show all")
        expect(wrapper.find('.filters').exists()).toBe(false)
        expect(wrapper.find('.filter-marker').exists()).toBe(true)
        await wrapper.get('button[aria-label="Running"]').trigger("click")
        expect(wrapper.findAll("li.row")).toHaveLength(2)
        await wrapper.get('button[aria-label="All"]').trigger("click")
        expect(wrapper.findAll("section.repo")).toHaveLength(2)
        expect(wrapper.find(".filter-context").exists()).toBe(false)
        expect(wrapper.find(".filter-notice").exists()).toBe(false)
        expect(wrapper.find(".filter-marker").exists()).toBe(false)
    })

    it("uses the filtered tree layout for the only visible sibling", async () => {
        const wrapper = mountList()
        const parent = view.repos[0].containers[0]
        const children = ["a", "b", "c"].map(suffix => ({...view.repos[0].containers[1],
            name: `alpha-${suffix}`, indent: suffix === "c" ? "└ " : "├ ", depth: 1,
            child: {orchestrator: parent.name, orchestrator_short_name: "1", state: "working", runner: null}}))
        await wrapper.setProps({view: {...view, repos: [{prefix: "alpha", containers: [parent, ...children]}],
            filters: {active: {visible: [parent.name, "alpha-b"], context: [parent.name],
                indents: {[parent.name]: "", "alpha-b": "└ "}}}}})
        await wrapper.get('button[aria-label="Quick settings"]').trigger("click")
        await wrapper.get('button[aria-label="Active"]').trigger("click")
        expect(wrapper.get('[data-container="alpha-b"] .indent').element.textContent).toBe("└ ")
        await wrapper.get('button[aria-label="All"]').trigger("click")
        expect(wrapper.get('[data-container="alpha-b"] .indent').element.textContent).toBe("├ ")
    })

    it("explains recent-idle membership with a highlighted age", async () => {
        const wrapper = mountList()
        const fresh = {...view.repos[0].containers[0], agent: "idle", agent_recent_idle: "5m"}
        await wrapper.setProps({view: {...view, repos: [{prefix: "alpha", containers: [fresh]}]}})
        expect(wrapper.get(".agent").text()).toBe("idle 5m")
        expect(wrapper.get(".agent").classes()).toContain("agent-recent-idle")
    })

    it("keeps recent-idle emphasis beside a more urgent session", async () => {
        const wrapper = mountList()
        const waiting = {...view.repos[0].containers[0], agent_recent_idle: "5m"}
        await wrapper.setProps({view: {...view, repos: [{prefix: "alpha", containers: [waiting]}]}})
        expect(wrapper.findAll(".agent").map(badge => badge.text())).toEqual(["waiting for you", "idle 5m"])
        expect(wrapper.get(".agent-recent-idle").text()).toBe("idle 5m")
    })

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

    it("finds a listed orchestrator in another repository, including a repo-only view", async () => {
        const wrapper = mountList()
        const parent = view.repos[0].containers[0]
        const child = {...view.repos[0].containers[1], name: "beta-task", child: {
            orchestrator: parent.name, orchestrator_short_name: "1", state: "working", runner: null,
        }}
        const beta = {prefix: "beta", containers: [child]}
        await wrapper.setProps({view: {...view, repos: [view.repos[0], beta]}})
        expect(wrapper.get('[data-container="beta-task"]').text()).not.toContain("orchestrator not listed")
        await wrapper.setProps({view: {...view, repos: [beta]}, listedContainers: [parent.name, child.name]})
        expect(wrapper.get('[data-container="beta-task"]').text()).not.toContain("orchestrator not listed")
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
        const links = mountList().findAll("li.row a")
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

    it("words an API-error agent state and styles it like a waiting one", async () => {
        const wrapper = mountList()
        const stuck = {...view.repos[0].containers[0], agent: "error:quota"}
        await wrapper.setProps({view: {...view, repos: [{prefix: "alpha", containers: [stuck]}]}})
        const row = wrapper.get("li.row")
        expect(row.attributes("data-agent")).toBe("error")
        expect(row.get(".agent").text()).toBe("API error: quota")
        expect(row.get(".agent").attributes("data-status")).toBe("error")
    })

    it("marks each row with its row phase and state for the theme to style", () => {
        const rows = mountList().findAll("li.row")
        expect(rows[0].attributes("data-phase")).toBe("booting")
        expect(rows[0].attributes("data-state")).toBe("Running")
        expect(rows[1].attributes("data-phase")).toBeUndefined()
        expect(rows[1].attributes("data-state")).toBe("Stopped")
    })

    it("names the phase and stopped state in text so neither relies on colour (e-ink)", () => {
        const rows = mountList().findAll("li.row")
        expect(rows[0].get(".phase").text()).toBe("booting")
        expect(rows[1].get(".phase").text()).toBe("Stopped")
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
