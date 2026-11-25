import {mount} from "@vue/test-utils"
import {nextTick} from "vue"
import {expect, it, vi} from "vitest"
import JumpPanel from "./JumpPanel.vue"

it("extracts prompt text without activating elements in the live document", async () => {
    const activated = vi.fn()
    const tag = `prompt-probe-${crypto.randomUUID()}`
    customElements.define(tag, class extends HTMLElement {
        constructor() { super(); activated() }
    })
    const wrapper = mount(JumpPanel, {props: {entries: [
        {kind: "user", id: "u1", timestamp: null, html: `<${tag}>Read <strong>this</strong> &amp; that</${tag}>`, meta: false, label: null, expandable: false},
    ]}})
    expect(wrapper.get("button.excerpt").text()).toBe("Read this & that")
    expect(activated).not.toHaveBeenCalled()
    await wrapper.get("button.excerpt").trigger("click")
    expect(wrapper.emitted("select")).toEqual([["entry:u1"]])
    wrapper.unmount()
})

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
