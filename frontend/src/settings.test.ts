import {beforeEach, describe, expect, it, vi} from "vitest"
import {readItem} from "./storage"
import {reloadSettings, settings, startSettings, updateSettings} from "./settings"

function stubScheme(dark: boolean) {
    vi.stubGlobal("matchMedia", (query: string) => ({
        matches: query.includes("dark") ? dark : false,
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
    }))
}

describe("settings", () => {
    beforeEach(() => {
        stubScheme(false)
        reloadSettings()
    })

    it("defaults the theme to the device's colour scheme", () => {
        stubScheme(true)
        reloadSettings()
        startSettings()
        expect(settings.theme).toBe("auto")
        expect(document.documentElement.dataset.theme).toBe("dark")
        stubScheme(false)
        reloadSettings()
        expect(settings.theme).toBe("auto")
        // Re-applying the same Auto setting also resolves the current scheme.
        updateSettings({bodyFontSize: 17})
        expect(document.documentElement.dataset.theme).toBe("light")
    })

    it("updates Auto when the device scheme changes without overriding an explicit theme", () => {
        let dark = false
        let change: (() => void) | undefined
        vi.stubGlobal("matchMedia", () => ({
            get matches() { return dark },
            addEventListener: (_event: string, listener: () => void) => { change = listener },
        }))
        startSettings()
        expect(document.documentElement.dataset.theme).toBe("light")
        dark = true
        change?.()
        expect(document.documentElement.dataset.theme).toBe("dark")
        updateSettings({theme: "light"})
        change?.()
        expect(document.documentElement.dataset.theme).toBe("light")
    })

    it("applies the theme and font sizes to the document root", () => {
        startSettings(document.documentElement)
        updateSettings({theme: "eink"})
        expect(document.documentElement.dataset.theme).toBe("eink")
        updateSettings({bodyFontSize: 20, codeFontSize: 15})
        expect(document.documentElement.style.getPropertyValue("--font-size-body")).toBe("20px")
        expect(document.documentElement.style.getPropertyValue("--font-size-code")).toBe("15px")
    })

    it("remembers choices per device across a reload", () => {
        updateSettings({theme: "dark", bodyFontSize: 18, showThinking: true})
        reloadSettings()
        expect(settings.theme).toBe("dark")
        expect(settings.bodyFontSize).toBe(18)
        expect(settings.showThinking).toBe(true)
        expect(readItem("jailbee.showThinking")).toBe("1")
    })

    it("keeps e-ink follow off by default and remembers opting in", () => {
        expect(settings.einkFollow).toBe(false)
        updateSettings({einkFollow: true})
        reloadSettings()
        expect(settings.einkFollow).toBe(true)
        expect(readItem("jailbee.einkFollow")).toBe("1")
    })

    it("remembers the container filter and rejects unknown values", () => {
        expect(settings.containerFilter).toBe("all")
        updateSettings({containerFilter: "active"})
        reloadSettings()
        expect(settings.containerFilter).toBe("active")
        localStorage.setItem("jailbee.containerFilter", "paused")
        reloadSettings()
        expect(settings.containerFilter).toBe("all")
    })

    it("keeps font sizes within a readable range", () => {
        updateSettings({bodyFontSize: 4, codeFontSize: 400})
        expect(settings.bodyFontSize).toBe(12)
        expect(settings.codeFontSize).toBe(28)
    })

    it("ignores stored garbage", () => {
        localStorage.setItem("jailbee.theme", "neon")
        localStorage.setItem("jailbee.bodyFontSize", "huge")
        reloadSettings()
        expect(settings.theme).toBe("auto")
        expect(settings.bodyFontSize).toBe(16)
    })

    it("works with defaults and in memory when storage is unavailable", () => {
        vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
            throw new Error("denied")
        })
        vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
            throw new Error("denied")
        })
        reloadSettings()
        expect(settings.theme).toBe("auto")
        expect(settings.showThinking).toBe(false)
        updateSettings({theme: "dark"})
        expect(settings.theme).toBe("dark")
    })

    it("keeps the display profile separate from the theme", () => {
        startSettings(document.documentElement)
        expect(settings.displayProfile).toBe("normal")
        updateSettings({displayProfile: "eink"})
        expect(document.documentElement.dataset.profile).toBe("eink")
        expect(readItem("jailbee.displayProfile")).toBe("eink")
        updateSettings({theme: "dark"})
        expect(document.documentElement.dataset.profile).toBe("eink")
        expect(document.documentElement.dataset.theme).toBe("dark")
        updateSettings({displayProfile: "normal"})
        reloadSettings()
        expect(settings.displayProfile).toBe("normal")
    })
})
