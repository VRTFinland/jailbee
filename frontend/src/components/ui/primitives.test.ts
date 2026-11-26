import {describe, expect, it} from "vitest"
import {mount} from "@vue/test-utils"
import {createMemoryHistory, createRouter} from "vue-router"
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

    it("emits clicks while enabled", async () => {
        const enabled = mount(Button, {slots: {default: "Go"}})
        await enabled.get("button").trigger("click")
        expect(enabled.emitted("click")).toHaveLength(1)
    })

    // A disabled button gets no clicks from the browser; VTU's trigger skips
    // disabled elements too, so an `emitted` check could never fail. The
    // attribute is what the component controls.
    it("is a disabled button when disabled", () => {
        const disabled = mount(Button, {props: {disabled: true}, slots: {default: "Go"}})
        expect(disabled.get("button").attributes("disabled")).toBeDefined()
        expect(mount(Button, {slots: {default: "Go"}}).get("button").attributes("disabled")).toBeUndefined()
    })

    it.each(["compact", "narrow", "truncate"] as const)("toggles %s without changing the full name or click behaviour", async (option) => {
        const label = "A long prompt excerpt for the reader"
        const wrapper = mount(Button, {props: {[option]: true}, slots: {default: label}})
        const button = wrapper.get("button")
        expect(button.attributes(`data-${option}`)).toBe("true")
        expect(button.text()).toBe(label)
        await button.trigger("click")
        expect(wrapper.emitted("click")).toHaveLength(1)
        await wrapper.setProps({[option]: false})
        expect(button.attributes(`data-${option}`)).toBeUndefined()
        expect(button.text()).toBe(label)
        wrapper.unmount()
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
    it("toggles compact density without losing its accessible name or click event", async () => {
        const wrapper = mount(IconButton, {props: {icon: "back", label: "Previous page", compact: true}})
        const button = wrapper.get("button")
        expect(button.attributes("data-compact")).toBe("true")
        expect(button.attributes("aria-label")).toBe("Previous page")
        await button.trigger("click")
        expect(wrapper.emitted("click")).toHaveLength(1)
        await wrapper.setProps({compact: false})
        expect(button.attributes("data-compact")).toBeUndefined()
        expect(button.attributes("aria-label")).toBe("Previous page")
        wrapper.unmount()
    })

    it("defaults to text and exposes a tonal variant for floating controls", () => {
        const text = mount(IconButton, {props: {icon: "menu", label: "Menu"}})
        const tonal = mount(IconButton, {props: {icon: "menu", label: "Menu", variant: "tonal"}})
        expect(text.get("button").attributes("data-variant")).toBe("text")
        expect(tonal.get("button").attributes("data-variant")).toBe("tonal")
        text.unmount()
        tonal.unmount()
    })

    it("is a button named by its required label, its icon decorative", () => {
        const wrapper = mount(IconButton, {props: {icon: "menu", label: "Menu"}})
        const button = wrapper.get("button")
        expect(button.attributes("type")).toBe("button")
        expect(button.attributes("aria-label")).toBe("Menu")
        expect(button.attributes("title")).toBe("Menu")
        expect(wrapper.get("svg").attributes("aria-hidden")).toBe("true")
    })

    it("emits clicks while enabled", async () => {
        const enabled = mount(IconButton, {props: {icon: "close", label: "Close"}})
        await enabled.get("button").trigger("click")
        expect(enabled.emitted("click")).toHaveLength(1)
    })

    it("is a disabled button when disabled", () => {
        const disabled = mount(IconButton, {props: {icon: "close", label: "Close", disabled: true}})
        expect(disabled.get("button").attributes("disabled")).toBeDefined()
        expect(mount(IconButton, {props: {icon: "close", label: "Close"}}).get("button").attributes("disabled"))
            .toBeUndefined()
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

    it("is a disabled button when disabled", () => {
        const wrapper = mount(Switch, {props: {modelValue: false, label: "Follow live", disabled: true}})
        expect(wrapper.get('[role="switch"]').attributes("disabled")).toBeDefined()
        expect(mount(Switch, {props: {modelValue: false, label: "Follow live"}}).get('[role="switch"]')
            .attributes("disabled")).toBeUndefined()
    })
})

describe("StatusChip", () => {
    it("can emphasize a status without changing its words or meaning", async () => {
        const chip = mount(StatusChip, {props: {status: "idle", label: "idle 5m", emphasis: true}})
        expect(chip.attributes("data-emphasis")).toBe("true")
        expect(chip.attributes("data-status")).toBe("idle")
        expect(chip.text()).toBe("idle 5m")
        await chip.setProps({emphasis: false})
        expect(chip.attributes("data-emphasis")).toBeUndefined()
        expect(chip.text()).toBe("idle 5m")
        chip.unmount()
    })

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
    it("can mark supporting context without changing its content or selection", async () => {
        const row = mount(ListRow, {props: {title: "Ancestor", context: true, selected: true}})
        expect(row.classes()).toContain("filter-context")
        expect(row.text()).toBe("Ancestor")
        expect(row.attributes("aria-current")).toBe("true")
        await row.setProps({context: false})
        expect(row.classes()).not.toContain("filter-context")
        expect(row.attributes("aria-current")).toBe("true")
        row.unmount()
    })

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

    it("carries block content in its body below the title and secondary line", () => {
        const wrapper = mount(ListRow, {
            props: {title: "dev-plan", secondary: "waiting for you"},
            slots: {default: '<p class="orchestration">Orchestrator · 2 children</p><dl><dt>Sync</dt><dd>ok</dd></dl>'},
        })
        const body = wrapper.get(".list-row-body")
        expect(body.element.tagName).toBe("DIV")
        expect(body.get(".orchestration").text()).toBe("Orchestrator · 2 children")
        expect(body.get("dl dd").text()).toBe("ok")
        expect(mount(ListRow, {props: {title: "a"}}).find(".list-row-body").exists()).toBe(false)
    })

    describe("with a route", () => {
        function linked(props: Record<string, unknown>, slots: Record<string, string> = {}) {
            const router = createRouter({history: createMemoryHistory(), routes: [
                {path: "/", component: {template: "<div />"}},
                {path: "/c/:name", name: "container", component: {template: "<div />"}},
            ]})
            return {router, wrapper: mount(ListRow, {props, slots, global: {plugins: [router]}})}
        }

        it("makes the title a link to the route, which the whole row opens", async () => {
            const {router, wrapper} = linked({title: "dev-plan", to: {name: "container", params: {name: "dev-plan"}}})
            const link = wrapper.get("a.list-row-link")
            expect(link.attributes("href")).toBe("/c/dev-plan")
            expect(link.text()).toBe("dev-plan")
            expect(wrapper.classes()).toContain("linked")
            await link.trigger("click")
            await router.isReady()
            await new Promise((resolve) => setTimeout(resolve))
            expect(router.currentRoute.value.fullPath).toBe("/c/dev-plan")
        })

        it("can name a route with its supporting status and metadata", () => {
            const {wrapper} = linked({title: "Fix login", to: "/c/a", linkLabel: "Fix login live claude"})
            expect(wrapper.get("a").attributes("aria-label")).toBe("Fix login live claude")
            expect(wrapper.get("a").text()).toBe("Fix login")
        })

        it("puts aria-current on the link, not the list item", () => {
            const {wrapper} = linked({title: "a", to: "/c/a", selected: true})
            expect(wrapper.get("a").attributes("aria-current")).toBe("true")
            expect(wrapper.attributes("aria-current")).toBeUndefined()
            const {wrapper: plain} = linked({title: "a", to: "/c/a"})
            expect(plain.get("a").attributes("aria-current")).toBeUndefined()
        })

        it("keeps trailing controls and body links outside the row link", () => {
            const {wrapper} = linked({title: "a", to: "/c/a"}, {
                trailing: '<button type="button">Actions</button>',
                default: '<p>Child of <a href="#/c/ork">ork</a></p>',
            })
            const rowLink = wrapper.get("a.list-row-link")
            expect(rowLink.find("button").exists()).toBe(false)
            expect(rowLink.find("a").exists()).toBe(false)
            expect(wrapper.findAll("a")).toHaveLength(2)
        })
    })
})
