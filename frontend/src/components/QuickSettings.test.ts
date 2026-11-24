import {flushPromises, mount} from "@vue/test-utils"
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest"
import {createMemoryHistory, createRouter} from "vue-router"
import {reloadSettings, settings, startSettings} from "../settings"
import QuickSettings from "./QuickSettings.vue"

const mounted: {unmount(): void}[] = []
beforeEach(() => reloadSettings())
afterEach(() => { for (const wrapper of mounted.splice(0)) wrapper.unmount() })

async function menu(context: "containers" | "session" | null = null) {
    const router = createRouter({history: createMemoryHistory(), routes: [
        {path: "/", component: {template: "<div />"}},
        {path: "/settings", name: "settings", component: {template: "<div />"}},
    ]})
    await router.push("/")
    const wrapper = mount(QuickSettings, {props: {context}, attachTo: document.body, global: {plugins: [router]}})
    mounted.push(wrapper)
    await wrapper.get('button[aria-label="Quick settings"]').trigger("click")
    await flushPromises()
    return wrapper
}

describe("Quick settings", () => {
    it("edits shared persisted font sizes live and cycles Auto, Light, Dark and E-ink", async () => {
        startSettings()
        const wrapper = await menu()
        await wrapper.get('[aria-label="Increase body font size"]').trigger("click")
        await wrapper.get('[aria-label="Decrease code font size"]').trigger("click")
        expect(settings.bodyFontSize).toBe(17)
        expect(localStorage.getItem("jailbee.bodyFontSize")).toBe("17")
        expect(document.documentElement.style.getPropertyValue("--font-size-code")).toBe("13px")
        const cycle = () => wrapper.get('[aria-label^="Theme:"]')
        expect(cycle().text()).toContain("Auto")
        for (const theme of ["light", "dark", "eink", "auto"]) {
            await cycle().trigger("click")
            expect(settings.theme).toBe(theme)
            expect(localStorage.getItem("jailbee.theme")).toBe(theme)
        }
    })

    it("offers only the current view's controls and marks a non-All container filter", async () => {
        const wrapper = await menu("containers")
        await wrapper.get('[aria-label="Active"]').trigger("click")
        expect(localStorage.getItem("jailbee.containerFilter")).toBe("active")
        expect(wrapper.find(".filter-marker").exists()).toBe(true)
        expect(wrapper.find('[aria-label="Show thinking"]').exists()).toBe(false)
        await wrapper.setProps({context: "session"})
        expect(wrapper.find('[aria-label="Active"]').exists()).toBe(false)
        await wrapper.get('[aria-label="Show thinking"]').setValue(true)
        expect(settings.showThinking).toBe(true)
        await wrapper.get('[aria-label="Agent context"]').setValue("collapsed")
        expect(settings.agentContext).toBe("collapsed")
        await wrapper.get('[aria-label="Follow new messages"]').setValue(false)
        expect(localStorage.getItem("jailbee.followNewMessages")).toBe("0")
        await wrapper.setProps({context: null})
        expect(wrapper.find(".view-heading").exists()).toBe(false)
    })

    it("paginates whole rows on a short e-ink screen and keeps keyboard focus inside", async () => {
        settings.displayProfile = "eink"
        vi.stubGlobal("innerHeight", 350)
        vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
            return {top: this.classList.contains("quick-panel") ? 50 : 0, height: 50} as DOMRect
        })
        const wrapper = await menu("session")
        expect(wrapper.get('[aria-label="Settings pages"]').text()).toContain("1 / 2")
        expect(wrapper.get('[aria-label="Show thinking"]').element.closest("[hidden]")).not.toBeNull()
        await wrapper.get('[aria-label="Next settings page"]').trigger("click")
        expect(wrapper.get('[aria-label="Show thinking"]').element.closest("[hidden]")).toBeNull()
        const next = wrapper.get('[aria-label="Next settings page"]')
        expect(next.attributes("disabled")).toBeDefined()
        await wrapper.get('[role="dialog"]').trigger("keydown", {key: "PageUp"})
        expect(wrapper.get('[aria-label="Settings pages"]').text()).toContain("1 / 2")
        ;(wrapper.get('[aria-label="Next settings page"]').element as HTMLButtonElement).focus()
        await wrapper.get('[aria-label="Next settings page"]').trigger("keydown", {key: "Tab"})
        expect(document.activeElement).toBe(wrapper.get('[aria-label="Decrease body font size"]').element)
    })

    it("opens from the icon, closes by icon, Escape and backdrop, and restores focus", async () => {
        const wrapper = await menu()
        const icon = wrapper.get('button[aria-label="Quick settings"]')
        expect(wrapper.find('[role="dialog"]').exists()).toBe(true)
        await icon.trigger("click")
        expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
        expect(document.activeElement).toBe(icon.element)
        await icon.trigger("click")
        await wrapper.get('[role="dialog"]').trigger("keydown", {key: "Escape"})
        expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
        expect(document.activeElement).toBe(icon.element)
        await icon.trigger("click")
        await wrapper.get(".quick-backdrop").trigger("click")
        expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
        expect(document.activeElement).toBe(icon.element)
    })
})
