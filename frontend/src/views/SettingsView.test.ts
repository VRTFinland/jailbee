import {beforeEach, describe, expect, it, vi} from "vitest"
import {flushPromises, mount} from "@vue/test-utils"
import {Dashboard, DASHBOARD, type SocketLike} from "../dashboard"
import SettingsView from "./SettingsView.vue"
import {readItem} from "../storage"
import {reloadSettings, settings, startSettings} from "../settings"

describe("SettingsView", () => {
    beforeEach(() => {
        vi.stubGlobal("matchMedia", () => ({matches: false, addEventListener() {}, removeEventListener() {}}))
        reloadSettings()
        startSettings(document.documentElement)
    })

    it("switching the theme sets data-theme on the document", async () => {
        const wrapper = mount(SettingsView, {global: {stubs: {RouterLink: true, QuickSettings: true}}})
        await wrapper.get("input[name=theme][value=dark]").setValue(true)
        expect(document.documentElement.dataset.theme).toBe("dark")
        await wrapper.get("input[name=theme][value=eink]").setValue(true)
        expect(document.documentElement.dataset.theme).toBe("eink")
    })

    it("defaults Agent context to hidden and stores the choice", async () => {
        const wrapper = mount(SettingsView, {global: {stubs: {RouterLink: true, QuickSettings: true}}})
        expect((wrapper.get('select[aria-label="Agent context"]').element as HTMLSelectElement).value).toBe("hidden")
        await wrapper.get('select[aria-label="Agent context"]').setValue("collapsed")
        expect(settings.agentContext).toBe("collapsed")
        expect(readItem("jailbee.agentContext")).toBe("collapsed")
        await wrapper.get('select[aria-label="Agent context"]').setValue("hidden")
    })

    it("offers page step percentages only for e-ink and remembers the selection", async () => {
        const wrapper = mount(SettingsView, {global: {stubs: {RouterLink: true, QuickSettings: true}}})
        expect(wrapper.find('[aria-label="Page step"]').exists()).toBe(false)
        await wrapper.get("input[name=profile][value=eink]").setValue(true)
        const select = wrapper.get('[aria-label="Page step"]')
        expect(select.findAll("option").map(option => option.text())).toEqual(["100 %", "90 %", "75 %", "66 %", "50 %"])
        expect((select.element as HTMLSelectElement).value).toBe("100")
        await select.setValue("75")
        expect(settings.pageStep).toBe(75)
        expect(readItem("jailbee.pageStep")).toBe("75")
        reloadSettings()
        expect(settings.pageStep).toBe(75)
    })

    it("toggles a preference when its visible label is clicked", async () => {
        const wrapper = mount(SettingsView, {attachTo: document.body, global: {stubs: {RouterLink: true, QuickSettings: true}}})
        const label = wrapper.findAll("label").find(label => label.text() === "Show thinking")!
        expect(label).toBeDefined()
        ;(label.element as HTMLLabelElement).click()
        await flushPromises()
        expect(settings.showThinking).toBe(true)
        wrapper.unmount()
    })

    it("edits font sizes and the thinking toggle", async () => {
        const wrapper = mount(SettingsView, {global: {stubs: {RouterLink: true, QuickSettings: true}}})
        await wrapper.get("input.body-size").setValue("19")
        await wrapper.get("input.code-size").setValue("13")
        await wrapper.get('[role="switch"][aria-label="Show thinking"]').trigger("click")
        expect(settings.bodyFontSize).toBe(19)
        expect(settings.codeFontSize).toBe(13)
        expect(settings.showThinking).toBe(true)
    })

    it("chooses the display profile independently of the theme", async () => {
        const wrapper = mount(SettingsView, {global: {stubs: {RouterLink: true, QuickSettings: true}}})
        await wrapper.get("input[name=profile][value=eink]").setValue(true)
        expect(document.documentElement.dataset.profile).toBe("eink")
        await wrapper.get("input[name=theme][value=dark]").setValue(true)
        expect(document.documentElement.dataset.profile).toBe("eink")
        await wrapper.get("input[name=profile][value=normal]").setValue(true)
        expect(document.documentElement.dataset.profile).toBe("normal")
    })

    describe("session name", () => {
        function stubSessionFetch(name: string) {
            const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
                const body = init?.method === "PUT" ? JSON.parse(init.body as string) : {device_name: name}
                return new Response(JSON.stringify({device_name: body.device_name.trim()}), {
                    status: 200,
                    headers: {"Content-Type": "application/json"},
                })
            })
            vi.stubGlobal("fetch", fetchMock)
            return fetchMock
        }

        it("prefills the current name and has no product-name placeholder", async () => {
            stubSessionFetch("Firefox on Linux")
            const wrapper = mount(SettingsView, {global: {stubs: {RouterLink: true, QuickSettings: true}}})
            await flushPromises()
            const input = wrapper.get("input[name=device]")
            expect((input.element as HTMLInputElement).value).toBe("Firefox on Linux")
            expect(input.attributes("placeholder")).toBeUndefined()
        })

        it("renames the session and updates the header's device live", async () => {
            const fetchMock = stubSessionFetch("Firefox on Linux")
            const dashboard = new Dashboard((): SocketLike => ({start() {}, stop() {}, send() {}}))
            dashboard.state.device = "Firefox on Linux"
            const wrapper = mount(SettingsView, {
                global: {stubs: {RouterLink: true, QuickSettings: true}, provide: {[DASHBOARD]: dashboard}},
            })
            await flushPromises()

            await wrapper.get("input[name=device]").setValue("  Hall tablet ")
            await wrapper.get("form.rename").trigger("submit")
            await flushPromises()

            const put = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT")!
            expect(put[0]).toBe("/api/session")
            expect(JSON.parse(put[1]!.body as string)).toEqual({device_name: "Hall tablet"})
            expect(dashboard.state.device).toBe("Hall tablet")
        })

        it("refuses an empty name and wires the labelled field to its validation error", async () => {
            const fetchMock = stubSessionFetch("x")
            const wrapper = mount(SettingsView, {global: {stubs: {RouterLink: true, QuickSettings: true}}})
            await flushPromises()
            const input = wrapper.get("input[name=device]")
            expect(input.attributes("aria-label") ?? input.attributes("aria-labelledby")).toBeTruthy()
            await input.setValue("   ")
            await wrapper.get("form.rename").trigger("submit")
            await flushPromises()
            expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(false)
            expect(wrapper.get("[role=alert]").text()).toContain("name")
            expect(input.attributes("aria-invalid")).toBe("true")
            expect(wrapper.get(`#${input.attributes("aria-describedby")!.split(" ").at(-1)}`).text()).toContain("name")
        })
    })
})
