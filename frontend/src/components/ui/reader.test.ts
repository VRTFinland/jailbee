import {mount} from "@vue/test-utils"
import {expect, it} from "vitest"
import Disclosure from "./Disclosure.vue"
import TranscriptEntry from "./TranscriptEntry.vue"

it("exposes disclosure state, emits its next value and follows external changes", async () => {
    const wrapper = mount(Disclosure, {props: {label: "Bash: run tests"}})
    const button = wrapper.get("button")
    expect(button.text()).toBe("Bash: run tests")
    expect(button.attributes("aria-expanded")).toBe("false")
    await button.trigger("click")
    expect(wrapper.emitted("update:open")).toEqual([[true]])
    expect(button.attributes("aria-expanded")).toBe("true")
    await wrapper.setProps({open: false})
    expect(button.attributes("aria-expanded")).toBe("false")
    await wrapper.setProps({error: true})
    expect(button.attributes("data-error")).toBe("true")
    expect(button.text()).toBe("Bash: run tests")
})

it.each(["user", "assistant", "thinking", "orphan"] as const)("keeps %s entry document content and anchors intact", (kind) => {
    const wrapper = mount(TranscriptEntry, {
        props: {kind}, attrs: {id: "entry-1"}, slots: {default: "<p>Read this reply</p>"},
    })
    expect(wrapper.element.tagName).toBe("SECTION")
    expect(wrapper.attributes("id")).toBe("entry-1")
    expect(wrapper.get("p").text()).toBe("Read this reply")
})
