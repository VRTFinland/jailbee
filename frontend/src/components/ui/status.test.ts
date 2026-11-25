import {describe, expect, it} from "vitest"
import {mount} from "@vue/test-utils"
import StatusChip from "./StatusChip.vue"
import {agentStatus, containerStatus, rowStatuses} from "./status"
import vocabulary from "./status-vocabulary.json"

// The wire values come from the dashboard core (tests/test_web_status_vocabulary.py
// keeps the JSON equal to STATE_SORT_RANK, ROW_PHASE_STYLES and URGENCY), so a
// new core value fails here, not in a browser.

describe("status mapping", () => {
    it.each(vocabulary.states)("maps the container state %s to a known chip", (state) => {
        const status = containerStatus({state, phase: null})
        expect(status.status).not.toBe("unknown")
        expect(status.label).toBe(state.toLowerCase())
    })

    it.each(vocabulary.phases)("maps the row phase %s to a known chip over the state", (phase) => {
        expect(containerStatus({state: "Running", phase})).toEqual({status: phase, label: phase})
    })

    it.each(vocabulary.agents)("maps the agent state %s to a known chip", (family) => {
        const state = family === "error" ? "error:rate_limit" : family
        const status = agentStatus(state)
        expect(status?.status).toBe(family)
    })

    it.each([
        ["waiting", "waiting for you"], ["busy", "working"], ["idle", "idle"], ["shell", "shell"],
        ["error:overloaded", "API error: overloaded"],
    ])("words the agent state %s as %s", (state, label) => {
        expect(agentStatus(state)?.label).toBe(label)
    })

    it("has no agent chip without an agent", () => {
        expect(agentStatus(null)).toBeNull()
    })

    it.each([
        [{state: "Error", phase: null}, "Error"],
        [{state: "Running", phase: "migrating"}, "migrating"],
    ])("keeps an unknown container value as neutral raw text: %o", (row, label) => {
        expect(containerStatus(row)).toEqual({status: "unknown", label})
    })

    it("keeps an unknown agent state as neutral raw text", () => {
        expect(agentStatus("compacting")).toEqual({status: "unknown", label: "compacting"})
    })

    it("gives a row its container chip, then its agent chip", () => {
        expect(rowStatuses({state: "Stopped", phase: null, agent: null})).toEqual([
            {status: "stopped", label: "stopped"},
        ])
        expect(rowStatuses({state: "Running", phase: "booting", agent: "error:auth"})).toEqual([
            {status: "booting", label: "booting"},
            {status: "error", label: "API error: auth"},
        ])
    })

    it("renders every wire value as a chip with an icon beside its words", () => {
        const rows = [
            ...vocabulary.states.map((state) => ({state, phase: null, agent: null})),
            ...vocabulary.phases.map((phase) => ({state: "Running", phase, agent: null})),
            ...vocabulary.agents.map((agent) => ({
                state: "Running", phase: null, agent: agent === "error" ? "error:quota" : agent,
            })),
        ]
        for (const row of rows) {
            for (const status of rowStatuses(row)) {
                const chip = mount(StatusChip, {props: status})
                expect(chip.find("svg").exists()).toBe(true)
                expect(chip.text()).toBe(status.label)
            }
        }
    })
})

describe("StatusChip fallback", () => {
    it("renders an unknown status as a neutral chip with the raw text instead of throwing", () => {
        const chip = mount(StatusChip, {props: {status: "unknown", label: "Error"}})
        expect(chip.attributes("data-status")).toBe("unknown")
        expect(chip.text()).toBe("Error")
        expect(chip.find("svg").exists()).toBe(false)
    })

    it("treats an unmapped status that slipped through as unknown", () => {
        const chip = mount(StatusChip, {props: {status: "Frozen" as never}})
        expect(chip.attributes("data-status")).toBe("unknown")
        expect(chip.text()).toBe("Frozen")
    })
})
