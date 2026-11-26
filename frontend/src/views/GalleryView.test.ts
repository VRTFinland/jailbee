import {describe, expect, it} from "vitest"
import {mount} from "@vue/test-utils"
import {createMemoryHistory} from "vue-router"
import {createAppRouter} from "../router"
import GalleryView from "./GalleryView.vue"

function mountGallery() {
    return mount(GalleryView, {global: {plugins: [createAppRouter(createMemoryHistory())]}})
}

// The gallery is the visual reference of the design system (#167): every
// theme in both display profiles, each showing the colour roles and the type
// scale. Later tickets add the UI primitives to each panel.
const THEMES = ["Light", "Dark", "E-ink"]
const PROFILES = ["normal", "e-ink"]

describe("GalleryView", () => {
    it("shows one panel per theme and display profile", () => {
        const wrapper = mountGallery()
        const names = wrapper.findAll("section[aria-label]").map((s) => s.attributes("aria-label"))
        expect(names).toEqual(THEMES.flatMap((t) => PROFILES.map((p) => `${t} theme, ${p} profile`)))
    })

    it("scopes each panel to its theme and profile", () => {
        const wrapper = mountGallery()
        const panel = wrapper.get('section[aria-label="Dark theme, e-ink profile"]')
        expect(panel.attributes("data-theme")).toBe("dark")
        expect(panel.attributes("data-profile")).toBe("eink")
    })

    it("names every colour role and type role in each panel", () => {
        const wrapper = mountGallery()
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
        const wrapper = mountGallery()
        for (const panel of wrapper.findAll("section[aria-label]")) {
            const icons = panel.findAll('[role="img"]').map((icon) => icon.attributes("aria-label"))
            expect(icons).toEqual(names)
            for (const name of names) expect(panel.text()).toContain(name)
        }
    })

    it("shows every primitive in every variant and state in each panel", async () => {
        const wrapper = mountGallery()
        for (const panel of wrapper.findAll("section[aria-label]")) {
            const buttons = panel.findAll('.group')
                .find((group) => group.get('.group-title').text() === 'Buttons')!.findAll('.button')
            expect(buttons.map((b) => [b.attributes("data-variant"), b.attributes("disabled") !== undefined]))
                .toEqual([...["primary", "tonal", "text", "danger"].flatMap((v) => [[v, false], [v, true]]),
                    ["primary", false], ["tonal", false]])
            expect(buttons.slice(-2).map((b) => [b.text(), b.find("svg").exists()]))
                .toEqual([["New container", true], ["Settings", true]])
            const iconButtons = panel.findAll(".icon-button")
            expect(iconButtons.map((b) => b.attributes("aria-label"))).toEqual(["Menu", "Close", "Menu (tonal)", "Settings (tonal, disabled)", "Settings (disabled)", "Actions for feat-reader-chrome", "Actions for dev-ork"])
            expect(iconButtons[2].attributes("data-variant")).toBe("tonal")
            expect(iconButtons[3].attributes("disabled")).toBeDefined()
            expect(iconButtons[4].attributes("disabled")).toBeDefined()
            const switches = panel.findAll('[role="switch"]')
            expect(switches.map((s) => [s.attributes("aria-checked"), s.attributes("disabled") !== undefined]))
                .toEqual([["true", false], ["false", false], ["true", true], ["false", true], ["false", false]])
            expect(panel.findAll(".status-chip").map((c) => c.attributes("data-status"))).toEqual([
                "running", "frozen", "stopped", "booting", "autostart", "job", "failed",
                "waiting", "busy", "idle", "shell", "error", "error", "unknown", "idle",
                "running", "booting", "failed", // the list rows' trailing chips
                "running", "busy", "booting", "job", "waiting", // linked and wrapping rows
            ])
            expect(panel.text()).toContain("API error: overloaded")
            const rows = panel.findAll(".list-row")
            expect(rows.map((r) => r.attributes("aria-current") ?? null)).toEqual(["true", null, null, null, null, null, null])
            expect(rows[0].find(".status-chip").exists()).toBe(true)
            expect(rows[3].find('[role="switch"]').exists()).toBe(true)
            // The linked rows (to the gallery itself: sample names are no real containers).
            expect(rows.filter(r => r.find("a.list-row-link").exists()).map((r) => r.get("a.list-row-link").attributes("href")))
                .toEqual(["/ui?row=dev-ork", "/ui?row=dev-ork-1"])
            expect(rows[4].get(".list-row-body").text()).toContain("Orchestrator · 2 children")
            expect(rows[4].findAll(".list-row-body dt").map((dt) => dt.text())).toEqual(["Sync", "Diff"])
            expect(rows[5].text()).toContain("A long container name that wraps instead of truncating")
            expect(rows[6].get("a.list-row-link").attributes("aria-current")).toBe("true")
            expect(rows[6].get(".list-row-body a").text()).toBe("dev-ork")
        }
        // The row's sample switch works, each panel its own.
        const follow = wrapper.findAll('[role="switch"][aria-label="Follow live"]')
        expect(follow).toHaveLength(6)
        await follow[0].trigger("click")
        expect(follow[0].attributes("aria-checked")).toBe("true")
        expect(follow[1].attributes("aria-checked")).toBe("false")
    })

    it("offers overlay triggers and labelled fields in every panel", () => {
        const wrapper = mountGallery()
        for (const panel of wrapper.findAll("section[aria-label]")) {
            expect(panel.text()).toContain("Open centred dialog")
            expect(panel.text()).toContain("Open drawer")
            expect(panel.text()).toContain("Container actions")
            expect(panel.findAll("label").map((label) => label.text())).toEqual([
                "Container name", "Invalid name", "Disabled field", "Theme", "Disabled select",
            ])
        }
    })

    it("draws each swatch from its token", () => {
        const wrapper = mountGallery()
        const swatch = wrapper.get('[data-token="--md-primary-container"]')
        expect(swatch.attributes("style")).toContain("background: var(--md-primary-container)")
    })
})
