import {readFileSync} from "node:fs"
import {describe, expect, it} from "vitest"

// jsdom does not evaluate scoped CSS, so the rule bodies are parsed from the
// source and the declarations asserted: every phase must be styled and mapped
// onto every theme, and e-ink must tell them apart without colour.
const PHASES = ["booting", "autostart", "job", "failed"]
const vueSource = readFileSync("src/components/RepoList.vue", "utf8")
const tokensSource = readFileSync("src/styles/tokens.css", "utf8")

interface Rule {
    selector: string
    decls: Record<string, string>
}

/** Flat CSS rules in source order (no nesting or at-rules in these files). */
function parseRules(css: string): Rule[] {
    const rules: Rule[] = []
    const re = /([^{}]+)\{([^{}]*)\}/g
    for (const m of css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(re)) {
        const decls: Record<string, string> = {}
        for (const d of m[2].split(";")) {
            const i = d.indexOf(":")
            if (i > 0) decls[d.slice(0, i).trim()] = d.slice(i + 1).trim()
        }
        rules.push({selector: m[1].trim().replace(/\s+/g, " "), decls})
    }
    return rules
}

const style = vueSource.slice(vueSource.indexOf("<style"))
const rows = parseRules(style)
const tokens = parseRules(tokensSource)

function decls(rules: Rule[], selector: string): Record<string, string> {
    const found = rules.filter((r) => r.selector === selector)
    expect(found.length, selector).toBe(1)
    return found[0].decls
}

describe("row phase styling", () => {
    it.each([
        ["light", ':root, [data-theme="light"]'],
        ["dark", '[data-theme="dark"]'],
        ["eink", '[data-theme="eink"]'],
    ])("the %s theme defines every phase token as a colour", (_theme, selector) => {
        const block = decls(tokens.filter((r) => `--md-phase-booting` in r.decls), selector)
        for (const phase of PHASES) expect(block[`--md-phase-${phase}`], phase).toMatch(/^#[0-9a-f]{6}$/i)
        const aliases = decls(tokens, ":root, [data-theme]")
        for (const phase of PHASES) expect(aliases[`--phase-${phase}`]).toBe(`var(--md-phase-${phase})`)
    })

    it("maps each phase's edge and label onto its own token", () => {
        for (const phase of PHASES) {
            const edge = decls(rows, `.row[data-phase="${phase}"]`)
            expect(edge["border-left-color"]).toBe(`var(--phase-${phase})`)
            const label = decls(rows, `.row[data-phase="${phase}"] .phase`)
            expect(label.color).toBe(`var(--phase-${phase})`)
        }
    })

    it("lets a phase win over the waiting-agent accent edge", () => {
        const at = (sel: string) => rows.findIndex((r) => r.selector === sel)
        const waiting = at('.row[data-agent="waiting"]')
        expect(waiting).toBeGreaterThanOrEqual(0)
        for (const phase of PHASES) expect(at(`.row[data-phase="${phase}"]`)).toBeGreaterThan(waiting)
    })

    it("dims a Stopped row", () => {
        expect(decls(rows, '.row[data-state="Stopped"]').opacity).toBe("0.6")
    })

    it("tells phases apart in e-ink by edge shape, not colour", () => {
        const eink = (p: string) => decls(rows, `:root[data-theme="eink"] .row[data-phase="${p}"]`)
        expect(eink("booting")["border-left-style"]).toBe("dashed")
        expect(eink("autostart")["border-left-style"]).toBe("dotted")
        expect(eink("job")["border-left-style"]).toBe("double")
        expect(eink("failed")["border-left-width"]).toBe("8px")
    })

    it("marks a Stopped row in e-ink with an open outline no phase edge can mimic", () => {
        const d = decls(rows, ':root[data-theme="eink"] .row[data-state="Stopped"]')
        expect(d.opacity).toBe("1")
        for (const side of ["top", "right", "bottom"]) expect(d[`border-${side}-style`], side).toBe("none")
        expect(d["border-left-style"]).toBeUndefined()
    })
})
