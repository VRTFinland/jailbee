import {mount} from "@vue/test-utils"
import {nextTick} from "vue"
import {expect, it} from "vitest"
import JumpPanel from "./JumpPanel.vue"

it("traps focus and restores the opening control on Escape", async () => {
    const origin = document.createElement("button")
    document.body.append(origin)
    origin.focus()
    const wrapper = mount(JumpPanel, {props: {entries: [], origin}, attachTo: document.body})
    await nextTick()
    const buttons = wrapper.findAll("button")
    expect(document.activeElement).toBe(buttons[0].element)
    ;(buttons[0].element as HTMLButtonElement).focus()
    await buttons[0].trigger("keydown", {key: "Tab", shiftKey: true})
    expect(document.activeElement).toBe(buttons[3].element)
    await buttons[3].trigger("keydown", {key: "Tab"})
    expect(document.activeElement).toBe(buttons[0].element)
    await buttons[0].trigger("keydown", {key: "Escape"})
    expect(wrapper.emitted("close")).toHaveLength(1)
    wrapper.unmount()
    expect(document.activeElement).toBe(origin)
    origin.remove()
})
