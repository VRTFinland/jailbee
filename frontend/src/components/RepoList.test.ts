import {mount} from "@vue/test-utils"
import {describe, expect, it} from "vitest"
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
                {name: "alpha-1", indent: "", agent: "waiting", cells: ["1", "▶", "◆ 5m", ""]},
                {name: "alpha-2", indent: "└ ", agent: null, cells: ["2", "■", "—", "#7"]},
            ],
        },
        {prefix: "empty", containers: []},
    ],
}

describe("RepoList", () => {
    it("groups containers under a heading per repository", () => {
        const wrapper = mount(RepoList, {props: {view}})
        const repos = wrapper.findAll("section.repo")
        expect(repos.map((r) => r.get("h2").text())).toEqual(["alpha", "empty"])
        expect(repos[0].findAll("li").map((li) => li.attributes("data-container"))).toEqual([
            "alpha-1",
            "alpha-2",
        ])
        expect(repos[1].text()).toContain("No containers.")
    })

    it("marks the agent state so a waiting agent stands out", () => {
        const wrapper = mount(RepoList, {props: {view}})
        const rows = wrapper.findAll("li.row")
        expect(rows[0].attributes("data-agent")).toBe("waiting")
        expect(rows[0].get(".agent").text()).toBe("waiting for you")
        expect(rows[1].attributes("data-agent")).toBeUndefined()
        expect(rows[1].find(".agent").exists()).toBe(false)
    })

    it("shows the core's cells under their headers and skips empty ones", () => {
        const wrapper = mount(RepoList, {props: {view}})
        const rows = wrapper.findAll("li.row")
        expect(rows[0].get(".name").text()).toBe("1")
        expect(rows[0].findAll(".cell").map((c) => c.text())).toEqual(["ST▶", "AI◆ 5m"])
        expect(rows[1].findAll(".cell").map((c) => c.text())).toEqual(["ST■", "AI—", "PR#7"])
    })
})
