import {describe, expect, it} from "vitest"
import {mount} from "@vue/test-utils"
import GalleryView from "./GalleryView.vue"

// The gallery is the visual reference of the design system (#167): every
// theme in both display profiles, each showing the colour roles and the type
// scale. Later tickets add the UI primitives to each panel.
const THEMES = ["Light", "Dark", "E-ink"]
const PROFILES = ["normal", "e-ink"]

describe("GalleryView", () => {
    it("shows one panel per theme and display profile", () => {
        const wrapper = mount(GalleryView)
        const names = wrapper.findAll("section[aria-label]").map((s) => s.attributes("aria-label"))
        expect(names).toEqual(THEMES.flatMap((t) => PROFILES.map((p) => `${t} theme, ${p} profile`)))
    })

    it("scopes each panel to its theme and profile", () => {
        const wrapper = mount(GalleryView)
        const panel = wrapper.get('section[aria-label="Dark theme, e-ink profile"]')
        expect(panel.attributes("data-theme")).toBe("dark")
        expect(panel.attributes("data-profile")).toBe("eink")
    })

    it("names every colour role and type role in each panel", () => {
        const wrapper = mount(GalleryView)
        for (const panel of wrapper.findAll("section[aria-label]")) {
            const text = panel.text()
            for (const role of ["surface", "on-surface", "primary", "on-primary-container", "error-container",
                "outline-variant", "phase-booting", "phase-failed-container"]) {
                expect(text).toContain(`--md-${role}`)
            }
            for (const role of ["Title large", "Body medium", "Label small"]) expect(text).toContain(role)
        }
    })

    it("shows the bundled dashboard icon inventory in every theme and profile", () => {
        const names = ["menu", "back", "forward", "close", "expand", "collapse", "page-up", "page-down",
            "jump", "start", "end", "settings", "add", "remove", "waiting", "working", "idle", "error",
            "running", "stopped", "booting", "autostart", "job", "success", "failure"]
        const wrapper = mount(GalleryView)
        for (const panel of wrapper.findAll("section[aria-label]")) {
            const icons = panel.findAll('[role="img"]').map((icon) => icon.attributes("aria-label"))
            expect(icons).toEqual(names)
            for (const name of names) expect(panel.text()).toContain(name)
        }
    })

    it("draws each swatch from its token", () => {
        const wrapper = mount(GalleryView)
        const swatch = wrapper.get('[data-token="--md-primary-container"]')
        expect(swatch.attributes("style")).toContain("background: var(--md-primary-container)")
    })
})
