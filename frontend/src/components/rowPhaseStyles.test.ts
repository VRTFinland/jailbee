import {readFileSync} from "node:fs"
import {describe, expect, it} from "vitest"

// jsdom does not evaluate scoped CSS, so the styling is checked in the source:
// every phase must be styled and mapped onto every theme.
const PHASES = ["booting", "autostart", "job", "failed"]
const vue = readFileSync("src/components/RepoList.vue", "utf8")
const tokens = readFileSync("src/styles/tokens.css", "utf8")

function themeBlock(selector: string): string {
    const start = tokens.indexOf(selector + " {")
    expect(start, selector).toBeGreaterThanOrEqual(0)
    return tokens.slice(start, tokens.indexOf("}", start))
}

describe("row phase styling", () => {
    it.each(["light", "dark", "eink"])("the %s theme defines every phase token", (theme) => {
        const block = theme === "light" ? themeBlock(":root") : themeBlock(`:root[data-theme="${theme}"]`)
        for (const phase of PHASES) expect(block).toContain(`--phase-${phase}:`)
    })

    it.each(PHASES)("styles the %s row edge and label", (phase) => {
        expect(vue).toContain(`.row[data-phase="${phase}"] {`)
        expect(vue).toContain(`.row[data-phase="${phase}"] .phase {`)
    })

    it("dims a Stopped row and tells phases apart in e-ink without colour", () => {
        expect(vue).toContain('.row[data-state="Stopped"] {')
        for (const phase of PHASES)
            expect(vue).toContain(`:root[data-theme="eink"] .row[data-phase="${phase}"] {`)
        expect(vue).toContain(':root[data-theme="eink"] .row[data-state="Stopped"] {')
    })
})
