import {mount, flushPromises} from "@vue/test-utils"
import {getByRole} from "@testing-library/dom"
import userEvent from "@testing-library/user-event"
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
    await flushPromises()
    const excerpt = getByRole(document.body, "button", {name: "Read this & that"})
    expect(excerpt.textContent).toBe("Read this & that")
    expect(activated).not.toHaveBeenCalled()
    excerpt.click()
    await nextTick()
    expect(wrapper.emitted("select")).toEqual([["entry:u1"]])
    wrapper.unmount()
})

it("traps focus and restores the opening control on Escape", async () => {
    const origin = document.createElement("button")
    document.body.append(origin)
    origin.focus()
    const wrapper = mount(JumpPanel, {props: {entries: [], origin}, attachTo: document.body})
    await flushPromises()
    await new Promise(resolve => setTimeout(resolve, 30))
    const user = userEvent.setup()
    const panel = getByRole(document.body, "dialog", {name: "Jump to"})
    const close = getByRole(panel, "button", {name: "Close"})
    const end = getByRole(panel, "button", {name: "End"})
    expect(document.activeElement).toBe(close)
    await user.tab({shift: true})
    expect(document.activeElement).toBe(end)
    await user.tab()
    expect(document.activeElement).toBe(close)
    await user.keyboard("{Escape}")
    expect(wrapper.emitted("close")).toHaveLength(1)
    wrapper.unmount()
    await flushPromises()
    expect(document.activeElement).toBe(origin)
    origin.remove()
})
