import {beforeEach, describe, expect, it, vi} from "vitest"
import {mount} from "@vue/test-utils"
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
})
