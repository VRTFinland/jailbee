import {mount} from "@vue/test-utils"
import {describe, expect, it} from "vitest"
import MenuRow from "./MenuRow.vue"

describe("MenuRow", () => {
    it("exposes an action and its selected preference without inventing menu keyboard semantics", async () => {
        const row = mount(MenuRow, {props: {action: true, selected: true}, slots: {default: "Running"}})
        expect(row.get("button").attributes("aria-pressed")).toBe("true")
        expect(row.get("button").attributes("type")).toBe("button")
        await row.get("button").trigger("click")
        expect(row.emitted("click")).toHaveLength(1)
    })
})
