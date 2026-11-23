import {beforeEach, describe, expect, it, vi} from "vitest"
import {flushPromises, mount} from "@vue/test-utils"
import {Dashboard, DASHBOARD, type SocketLike} from "../dashboard"
import SettingsView from "./SettingsView.vue"
import {reloadSettings, settings, startSettings} from "../settings"

describe("SettingsView", () => {
    beforeEach(() => {
        vi.stubGlobal("matchMedia", () => ({matches: false, addEventListener() {}, removeEventListener() {}}))
        reloadSettings()
        startSettings(document.documentElement)
    })

    it("switching the theme sets data-theme on the document", async () => {
        const wrapper = mount(SettingsView, {global: {stubs: {RouterLink: true}}})
        await wrapper.get("input[name=theme][value=dark]").setValue(true)
        expect(document.documentElement.dataset.theme).toBe("dark")
        await wrapper.get("input[name=theme][value=eink]").setValue(true)
        expect(document.documentElement.dataset.theme).toBe("eink")
    })

    it("edits font sizes and the thinking toggle", async () => {
        const wrapper = mount(SettingsView, {global: {stubs: {RouterLink: true}}})
        await wrapper.get("input.body-size").setValue("19")
        await wrapper.get("input.code-size").setValue("13")
        await wrapper.get("input.show-thinking").setValue(true)
        expect(settings.bodyFontSize).toBe(19)
        expect(settings.codeFontSize).toBe(13)
        expect(settings.showThinking).toBe(true)
    })

    it("chooses the display profile independently of the theme", async () => {
        const wrapper = mount(SettingsView, {global: {stubs: {RouterLink: true}}})
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
            const wrapper = mount(SettingsView, {global: {stubs: {RouterLink: true}}})
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
                global: {stubs: {RouterLink: true}, provide: {[DASHBOARD]: dashboard}},
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

        it("refuses an empty name without calling the server", async () => {
            const fetchMock = stubSessionFetch("x")
            const wrapper = mount(SettingsView, {global: {stubs: {RouterLink: true}}})
            await flushPromises()
            await wrapper.get("input[name=device]").setValue("   ")
            await wrapper.get("form.rename").trigger("submit")
            await flushPromises()
            expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(false)
            expect(wrapper.get("[role=alert]").text()).toContain("name")
        })
    })
})
