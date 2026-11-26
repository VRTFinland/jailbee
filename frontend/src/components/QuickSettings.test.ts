import {flushPromises, mount} from "@vue/test-utils"
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest"
import {createMemoryHistory, createRouter} from "vue-router"
import {reloadSettings, settings, startSettings} from "../settings"
import QuickSettings from "./QuickSettings.vue"
import IconButton from "./ui/IconButton.vue"

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
    it("offers the parent link when the title bar is hidden", async () => {
        settings.showTitleBar = false
        const wrapper = await menu()
        await wrapper.setProps({parent: {label: "work", to: "/"}} as never)
        expect(wrapper.get("a").text()).toBe("Back to work")
    })
    it("shows session metadata only when supplied", async () => {
        const wrapper = await menu("session")
        await wrapper.setProps({session: {agent: "codex", models: ["model-a", "model-b"], id: "session-123"}} as never)
        expect(wrapper.get('[aria-label="Session"]').text()).toContain("codex")
        expect(wrapper.get('[aria-label="Session"]').text()).toContain("model-a, model-b")
        expect(wrapper.get('[aria-label="Session"]').text()).toContain("session-123")
        await wrapper.setProps({session: null} as never)
        expect(wrapper.find('[aria-label="Session"]').exists()).toBe(false)
    })
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
        const icon = wrapper.get('button[aria-label="Quick settings"]')
        const description = icon.attributes("aria-describedby")
        expect(description).toBeTruthy()
        expect(document.getElementById(description!)?.textContent).toContain("Container filter enabled")
        expect(wrapper.get('[role="group"][aria-label="Container filter"]').findAll('button')).toHaveLength(3)
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

    it("edits the existing e-ink follow preference independently of normal follow", async () => {
        settings.displayProfile = "eink"
        const wrapper = await menu("session")
        await wrapper.get('[aria-label="Follow new messages"]').setValue(true)
        expect(localStorage.getItem("jailbee.einkFollow")).toBe("1")
        expect(settings.followNewMessages).toBe(true)
        await wrapper.get('[aria-label="Follow new messages"]').setValue(false)
        expect(settings.einkFollow).toBe(false)
    })

    it("opens with focus inside even when the first font stepper is disabled", async () => {
        settings.bodyFontSize = 12
        const wrapper = await menu()
        expect(document.activeElement).toBe(wrapper.get('[aria-label="Increase body font size"]').element)
        await wrapper.get('[aria-label="Increase body font size"]').trigger("keydown", {key: "Tab", shiftKey: true})
        expect(document.activeElement).toBe(wrapper.get('.quick-panel .settings-action').element)
    })

    it("paginates whole rows on a short e-ink screen and keeps keyboard focus inside", async () => {
        settings.displayProfile = "eink"
        vi.stubGlobal("innerHeight", 350)
        vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
            return {top: this.classList.contains("quick-panel") ? 50 : 0, height: 50} as DOMRect
        })
        const wrapper = await menu("session")
        expect(wrapper.get('[aria-label="Settings pages"]').text()).toContain("1 / 3")
        expect(wrapper.get('[aria-label="Show thinking"]').element.closest("[hidden]")).not.toBeNull()
        await wrapper.get('[aria-label="Next settings page"]').trigger("click")
        expect(wrapper.get('[aria-label="Show thinking"]').element.closest("[hidden]")).toBeNull()
        const next = wrapper.get('[aria-label="Next settings page"]')
        await next.trigger("click")
        expect(next.attributes("disabled")).toBeDefined()
        await wrapper.get('[role="dialog"]').trigger("keydown", {key: "PageUp"})
        await wrapper.get('[role="dialog"]').trigger("keydown", {key: "PageUp"})
        expect(wrapper.get('[aria-label="Settings pages"]').text()).toContain("1 / 3")
        ;(wrapper.get('[aria-label="Next settings page"]').element as HTMLButtonElement).focus()
        await wrapper.get('[aria-label="Next settings page"]').trigger("keydown", {key: "Tab"})
        expect(document.activeElement).toBe(wrapper.get('[aria-label="Decrease body font size"]').element)
    })

    it("repaginates when session metadata arrives after opening", async () => {
        vi.stubGlobal("innerHeight", 350)
        const styles = vi.spyOn(window, "getComputedStyle").mockReturnValue({paddingTop: "8px", paddingBottom: "8px", borderTopWidth: "1px", borderBottomWidth: "1px"} as CSSStyleDeclaration)
        const geometry = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
            return {top: this.classList.contains("quick-panel") ? 50 : 0, height: 50} as DOMRect
        })
        try {
            const wrapper = await menu("session")
            await wrapper.setProps({session: {agent: "codex", models: ["model"], id: "session-123"}} as never)
            await flushPromises()
            expect(wrapper.get('.view-heading').element.closest('[hidden]')).not.toBeNull()
            expect(wrapper.get('[aria-label="Session"]').element.closest('[hidden]')).toBeNull()
        } finally {
            geometry.mockRestore()
            styles.mockRestore()
            vi.unstubAllGlobals()
        }
    })

    it("does not restore trigger focus on navigation", async () => {
        const wrapper = await menu()
        const icon = wrapper.get('button[aria-label="Quick settings"]')
        await wrapper.get('.settings-action').trigger('click')
        await flushPromises()
        expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
        expect(document.activeElement).not.toBe(icon.element)
    })

    it("does not restore trigger focus on a route change", async () => {
        const router = createRouter({history: createMemoryHistory(), routes: [
            {path: "/", component: {template: "<div />"}},
            {path: "/other", component: {template: "<div />"}},
            {path: "/settings", name: "settings", component: {template: "<div />"}},
        ]})
        await router.push("/")
        const wrapper = mount(QuickSettings, {attachTo: document.body, global: {plugins: [router]}})
        mounted.push(wrapper)
        const icon = wrapper.get('button[aria-label="Quick settings"]')
        await icon.trigger("click")
        await flushPromises()
        await router.push("/other")
        await flushPromises()
        expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
        expect(document.activeElement).not.toBe(icon.element)
    })

    it("passes the real trigger to Settings even when the component click has no DOM currentTarget", async () => {
        const openSettings = vi.fn()
        const router = createRouter({history: createMemoryHistory(), routes: [
            {path: "/", component: {template: "<div />"}},
        ]})
        await router.push("/")
        const wrapper = mount(QuickSettings, {attachTo: document.body,
            global: {plugins: [router], provide: {"open-settings": openSettings}}})
        mounted.push(wrapper)
        wrapper.getComponent(IconButton).vm.$emit("click", new MouseEvent("click"))
        await flushPromises()
        await wrapper.get('.settings-action').trigger("click")
        expect(openSettings).toHaveBeenCalledWith(wrapper.get('button[aria-label="Quick settings"]').element)
    })

    it("closes on Escape from a focused select", async () => {
        const wrapper = await menu("session")
        await wrapper.get('select').trigger('keydown', {key: 'Escape'})
        expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
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
