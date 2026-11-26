import {mount} from "@vue/test-utils"
import {describe, expect, it} from "vitest"
import StatusChip from "./ui/StatusChip.vue"
import {containerStatus} from "./ui/status"

// Phases must remain distinguishable without colour, including from Stopped.
describe("row phase presentation", () => {
    it("gives every phase and stopped state a distinct painted icon", () => {
        const rows = [
            ...["booting", "autostart", "job", "failed"].map(phase => ({state: "Running", phase})),
            {state: "Stopped", phase: null},
        ]
        const icons = rows.map(row => {
            const chip = mount(StatusChip, {props: containerStatus(row)})
            expect(chip.text()).toBe(row.phase ?? "stopped")
            const path = chip.get("svg path").attributes("d")
            expect(path).toBeTruthy()
            chip.unmount()
            return path
        })
        expect(new Set(icons).size).toBe(rows.length)
    })
})
