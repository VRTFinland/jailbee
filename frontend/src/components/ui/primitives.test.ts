import {describe, expect, it} from "vitest"
import {mount} from "@vue/test-utils"
import Button from "./Button.vue"
import IconButton from "./IconButton.vue"
import Switch from "./Switch.vue"
import StatusChip from "./StatusChip.vue"
import ListRow from "./ListRow.vue"

// The static UI primitives (#167 ticket 05): observable behaviour only. The
// look (tokens, state layers, focus ring, e-ink) is checked in the browser by
// tests_e2e/test_design_system.py against the /ui gallery.

describe("Button", () => {
    it("is a non-submitting button named by its content", () => {
        const wrapper = mount(Button, {slots: {default: "New container"}})
        const button = wrapper.get("button")
        expect(button.attributes("type")).toBe("button")
        expect(button.text()).toBe("New container")
    })

    it.each(["primary", "tonal", "text", "danger"] as const)("renders the %s variant", (variant) => {
        const wrapper = mount(Button, {props: {variant}, slots: {default: "Go"}})
        expect(wrapper.get("button").attributes("data-variant")).toBe(variant)
    })

    it("defaults to the tonal variant", () => {
        const wrapper = mount(Button, {slots: {default: "Go"}})
        expect(wrapper.get("button").attributes("data-variant")).toBe("tonal")
    })

    it("emits clicks while enabled and none when disabled", async () => {
        const enabled = mount(Button, {slots: {default: "Go"}})
        await enabled.get("button").trigger("click")
        expect(enabled.emitted("click")).toHaveLength(1)

        const disabled = mount(Button, {props: {disabled: true}, slots: {default: "Go"}})
        expect(disabled.get("button").attributes("disabled")).toBeDefined()
        await disabled.get("button").trigger("click")
        expect(disabled.emitted("click")).toBeUndefined()
    })

    it("can submit a form", () => {
        const wrapper = mount(Button, {props: {type: "submit"}, slots: {default: "Log in"}})
        expect(wrapper.get("button").attributes("type")).toBe("submit")
    })

    it("draws a leading icon hidden from assistive technology", () => {
        const wrapper = mount(Button, {props: {icon: "add"}, slots: {default: "New"}})
        const icon = wrapper.get("svg")
        expect(icon.attributes("aria-hidden")).toBe("true")
        expect(wrapper.get("button").text()).toBe("New")
    })
})

describe("IconButton", () => {
    it("is a button named by its required label, its icon decorative", () => {
        const wrapper = mount(IconButton, {props: {icon: "menu", label: "Menu"}})
        const button = wrapper.get("button")
        expect(button.attributes("type")).toBe("button")
        expect(button.attributes("aria-label")).toBe("Menu")
        expect(button.attributes("title")).toBe("Menu")
        expect(wrapper.get("svg").attributes("aria-hidden")).toBe("true")
    })

    it("emits clicks while enabled and none when disabled", async () => {
        const enabled = mount(IconButton, {props: {icon: "close", label: "Close"}})
        await enabled.get("button").trigger("click")
        expect(enabled.emitted("click")).toHaveLength(1)

        const disabled = mount(IconButton, {props: {icon: "close", label: "Close", disabled: true}})
        expect(disabled.get("button").attributes("disabled")).toBeDefined()
        await disabled.get("button").trigger("click")
        expect(disabled.emitted("click")).toBeUndefined()
    })
})

describe("Switch", () => {
    it("is a switch named by its label with its state exposed", () => {
        const off = mount(Switch, {props: {modelValue: false, label: "Follow live"}})
        const control = off.get('[role="switch"]')
        expect(control.element.tagName).toBe("BUTTON")
        expect(control.attributes("type")).toBe("button")
        expect(control.attributes("aria-label")).toBe("Follow live")
        expect(control.attributes("aria-checked")).toBe("false")

        const on = mount(Switch, {props: {modelValue: true, label: "Follow live"}})
        expect(on.get('[role="switch"]').attributes("aria-checked")).toBe("true")
    })

    it("emits the toggled value and follows its model", async () => {
        const wrapper = mount(Switch, {
            props: {
                modelValue: false,
                label: "Follow live",
                "onUpdate:modelValue": (value: boolean) => wrapper.setProps({modelValue: value}),
            },
        })
        const control = wrapper.get('[role="switch"]')
        await control.trigger("click")
        expect(wrapper.emitted("update:modelValue")).toEqual([[true]])
        expect(control.attributes("aria-checked")).toBe("true")
        await control.trigger("click")
        expect(wrapper.emitted("update:modelValue")).toEqual([[true], [false]])
        expect(control.attributes("aria-checked")).toBe("false")
    })

    it("does not toggle when disabled", async () => {
        const wrapper = mount(Switch, {props: {modelValue: false, label: "Follow live", disabled: true}})
        const control = wrapper.get('[role="switch"]')
        expect(control.attributes("disabled")).toBeDefined()
        await control.trigger("click")
        expect(wrapper.emitted("update:modelValue")).toBeUndefined()
    })
})

describe("StatusChip", () => {
    it.each([
        ["running", "running"], ["frozen", "frozen"], ["stopped", "stopped"], ["booting", "booting"],
        ["autostart", "autostart"], ["job", "job"], ["failed", "failed"],
        ["waiting", "waiting for you"], ["busy", "working"], ["idle", "idle"],
        ["shell", "shell"], ["error", "error"],
    ] as const)("words %s as text beside a decorative icon", (status, text) => {
        const wrapper = mount(StatusChip, {props: {status}})
        expect(wrapper.text()).toBe(text)
        expect(wrapper.attributes("data-status")).toBe(status)
        expect(wrapper.get("svg").attributes("aria-hidden")).toBe("true")
    })

    it("takes a more specific wording", () => {
        const wrapper = mount(StatusChip, {props: {status: "error", label: "API error: overloaded"}})
        expect(wrapper.text()).toBe("API error: overloaded")
        expect(wrapper.attributes("data-status")).toBe("error")
    })
})

describe("ListRow", () => {
    it("is a list item with a title, a secondary line and a trailing slot", () => {
        const wrapper = mount(ListRow, {
            props: {title: "dev-plan", secondary: "waiting for you · 2 min"},
            slots: {trailing: '<span class="t">chip</span>'},
        })
        expect(wrapper.element.tagName).toBe("LI")
        expect(wrapper.get(".list-row-title").text()).toBe("dev-plan")
        expect(wrapper.get(".list-row-secondary").text()).toBe("waiting for you · 2 min")
        expect(wrapper.get(".list-row-trailing .t").text()).toBe("chip")
    })

    it("takes a title slot, so a view can make the title a link", () => {
        const wrapper = mount(ListRow, {slots: {title: '<a href="#/c/x">x</a>'}})
        expect(wrapper.get(".list-row-title a").attributes("href")).toBe("#/c/x")
        expect(wrapper.find(".list-row-secondary").exists()).toBe(false)
        expect(wrapper.find(".list-row-trailing").exists()).toBe(false)
    })

    it("exposes the selected row to assistive technology", () => {
        const plain = mount(ListRow, {props: {title: "a"}})
        expect(plain.attributes("aria-current")).toBeUndefined()
        const selected = mount(ListRow, {props: {title: "a", selected: true}})
        expect(selected.attributes("aria-current")).toBe("true")
    })
})
