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

    it("shows every primitive in every variant and state in each panel", async () => {
        const wrapper = mount(GalleryView)
        for (const panel of wrapper.findAll("section[aria-label]")) {
            const buttons = panel.findAll(".button")
            expect(buttons.map((b) => [b.attributes("data-variant"), b.attributes("disabled") !== undefined]))
                .toEqual([...["primary", "tonal", "text", "danger"].flatMap((v) => [[v, false], [v, true]]),
                    ["primary", false], ["tonal", false]])
            expect(buttons.slice(-2).map((b) => [b.text(), b.find("svg").exists()]))
                .toEqual([["New container", true], ["Settings", true]])
            const iconButtons = panel.findAll(".icon-button")
            expect(iconButtons.map((b) => b.attributes("aria-label"))).toEqual(["Menu", "Close", "Settings (disabled)", "Actions for feat-reader-chrome"])
            expect(iconButtons[2].attributes("disabled")).toBeDefined()
            const switches = panel.findAll('[role="switch"]')
            expect(switches.map((s) => [s.attributes("aria-checked"), s.attributes("disabled") !== undefined]))
                .toEqual([["true", false], ["false", false], ["true", true], ["false", true], ["false", false]])
            expect(panel.findAll(".status-chip").map((c) => c.attributes("data-status"))).toEqual([
                "running", "frozen", "stopped", "booting", "autostart", "job", "failed",
                "waiting", "busy", "idle", "shell", "error", "error", "unknown",
                "running", "booting", "failed", // the list rows' trailing chips
            ])
            expect(panel.text()).toContain("API error: overloaded")
            const rows = panel.findAll(".list-row")
            expect(rows.map((r) => r.attributes("aria-current") ?? null)).toEqual(["true", null, null, null])
            expect(rows[0].find(".status-chip").exists()).toBe(true)
            expect(rows[3].find('[role="switch"]').exists()).toBe(true)
        }
        // The row's sample switch works, each panel its own.
        const follow = wrapper.findAll('[role="switch"][aria-label="Follow live"]')
        expect(follow).toHaveLength(6)
        await follow[0].trigger("click")
        expect(follow[0].attributes("aria-checked")).toBe("true")
        expect(follow[1].attributes("aria-checked")).toBe("false")
    })

    it("draws each swatch from its token", () => {
        const wrapper = mount(GalleryView)
        const swatch = wrapper.get('[data-token="--md-primary-container"]')
        expect(swatch.attributes("style")).toContain("background: var(--md-primary-container)")
    })
})
