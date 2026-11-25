import {readFileSync} from "node:fs"
import {describe, expect, it} from "vitest"

// jsdom does not evaluate custom properties, so the token file is read as
// text and each theme's values are resolved the way the browser would for an
// element carrying that theme: `:root` and `[data-theme]` rules always apply,
// `[data-theme="<theme>"]` rules only for that theme, in source order.
const source = readFileSync("src/styles/tokens.css", "utf8")

type Theme = "light" | "dark" | "eink"
const THEMES: Theme[] = ["light", "dark", "eink"]

interface Rule {
    selectors: string[]
    decls: [string, string][]
}

/** Top-level rules only: at-rule blocks (@media, @font-face) are dropped. */
function topLevelRules(css: string): Rule[] {
    const text = css.replace(/\/\*[\s\S]*?\*\//g, "")
    const rules: Rule[] = []
    let depth = 0
    let start = 0
    let head = ""
    let skip = false
    for (let i = 0; i < text.length; i++) {
        const ch = text[i]
        if (ch === "{") {
            if (depth === 0) {
                head = text.slice(start, i).trim()
                skip = head.startsWith("@")
                start = i + 1
            }
            depth++
        } else if (ch === "}") {
            depth--
            if (depth === 0) {
                if (!skip) {
                    const decls: [string, string][] = []
                    for (const d of text.slice(start, i).split(";")) {
                        const at = d.indexOf(":")
                        if (at > 0) decls.push([d.slice(0, at).trim(), d.slice(at + 1).trim()])
                    }
                    rules.push({selectors: head.split(",").map((s) => s.trim().replace(/\s+/g, " ")), decls})
                }
                start = i + 1
            }
        }
    }
    return rules
}

const RULES = topLevelRules(source)

function appliesTo(selector: string, theme: Theme): boolean {
    if (selector === ":root" || selector === "[data-theme]") return true
    return selector === `[data-theme="${theme}"]`
}

/** Every custom property as declared (unresolved) for an element in `theme`. */
function declared(theme: Theme): Map<string, string> {
    const out = new Map<string, string>()
    for (const rule of RULES) {
        if (!rule.selectors.some((s) => appliesTo(s, theme))) continue
        for (const [name, value] of rule.decls) if (name.startsWith("--")) out.set(name, value)
    }
    return out
}

function resolve(theme: Theme, name: string, seen: string[] = []): string {
    const value = declared(theme).get(name)
    if (value === undefined) throw new Error(`${name} is not defined in ${theme}`)
    if (seen.includes(name)) throw new Error(`cycle: ${[...seen, name].join(" -> ")}`)
    return value.replace(/var\((--[\w-]+)\)/g, (_m, ref: string) => resolve(theme, ref, [...seen, name]))
}

function luminance(hex: string): number {
    expect(hex, "a 6-digit hex colour").toMatch(/^#[0-9a-f]{6}$/i)
    const n = Number.parseInt(hex.slice(1), 16)
    return [n >> 16, (n >> 8) & 255, n & 255]
        .map((v) => v / 255)
        .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
        .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0)
}

function contrast(a: string, b: string): number {
    const [x, y] = [luminance(a), luminance(b)]
    return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05)
}

const SURFACES = [
    "--md-surface",
    "--md-surface-container-lowest",
    "--md-surface-container-low",
    "--md-surface-container",
    "--md-surface-container-high",
    "--md-surface-container-highest",
]
const PHASES = ["booting", "autostart", "job", "failed"]

/** [foreground, background] pairs that must reach WCAG AA (4.5:1) for text. */
const PAIRS: [string, string][] = [
    ...SURFACES.flatMap((bg): [string, string][] => [
        ["--md-on-surface", bg],
        ["--md-on-surface-variant", bg],
    ]),
    ["--md-primary", "--md-surface"],
    ["--md-primary", "--md-surface-container-lowest"],
    ["--md-primary", "--md-surface-container-low"],
    ["--md-on-primary", "--md-primary"],
    ["--md-on-primary-container", "--md-primary-container"],
    ["--md-error", "--md-surface"],
    ["--md-on-error", "--md-error"],
    ["--md-on-error-container", "--md-error-container"],
    ...PHASES.flatMap((p): [string, string][] => [
        [`--md-phase-${p}`, "--md-surface"],
        [`--md-phase-${p}`, "--md-surface-container-lowest"],
        [`--md-phase-${p}`, "--md-surface-container-low"],
        [`--md-phase-${p}`, "--md-surface-container"],
        [`--md-phase-${p}`, `--md-phase-${p}-container`],
        [`--md-on-phase-${p}-container`, `--md-phase-${p}-container`],
    ]),
]

describe("design tokens", () => {
    describe.each(THEMES)("the %s theme", (theme) => {
        it.each(PAIRS)("%s on %s reaches WCAG AA", (fg, bg) => {
            const ratio = contrast(resolve(theme, fg), resolve(theme, bg))
            expect(ratio, `${fg} on ${bg}: ${ratio.toFixed(2)}`).toBeGreaterThanOrEqual(4.5)
        })
    })

    it("defines every colour role in every theme, so a scoped theme overrides them all", () => {
        const colourRoles = (theme: Theme) =>
            RULES.filter((r) => r.selectors.includes(`[data-theme="${theme}"]`))
                .flatMap((r) => r.decls.map(([n]) => n))
                .filter((n) => n.startsWith("--md-"))
                .sort()
        expect(colourRoles("light").length).toBeGreaterThan(20)
        expect(colourRoles("dark")).toEqual(colourRoles("light"))
        expect(colourRoles("eink")).toEqual(colourRoles("light"))
    })

    it("keeps e-ink black on white", () => {
        const eink = RULES.filter((r) => r.selectors.includes('[data-theme="eink"]')).flatMap((r) => r.decls)
        for (const [name] of eink) {
            if (!name.startsWith("--md-")) continue
            expect(["#000000", "#ffffff"], name).toContain(resolve("eink", name).toLowerCase())
        }
        expect(resolve("eink", "--bg")).toBe("#ffffff")
        expect(resolve("eink", "--text")).toBe("#000000")
    })

    it("derives light and dark from the same blue seed", () => {
        expect(resolve("light", "--md-primary")).toBe("#3a5f94")
        expect(resolve("dark", "--md-primary")).toBe("#a7c8ff")
    })

    it.each([
        "--bg", "--surface", "--text", "--muted", "--border", "--border-strong", "--accent",
        "--accent-text", "--danger", "--notice-bg",
        "--phase-booting", "--phase-autostart", "--phase-job", "--phase-failed",
        "--space-1", "--space-2", "--space-3", "--space-4", "--radius", "--font-body",
    ])("keeps the old name %s as an alias of an md token", (name) => {
        for (const theme of THEMES) {
            expect(declared(theme).get(name), `${name} in ${theme}`).toMatch(/^var\(--md-[\w-]+\)$/)
        }
    })

    it("has the Material 3 shape scale", () => {
        expect(resolve("light", "--md-shape-small")).toBe("8px")
        expect(resolve("light", "--md-shape-medium")).toBe("12px")
        expect(resolve("light", "--md-shape-large")).toBe("16px")
        expect(resolve("light", "--md-shape-extra-large")).toBe("28px")
        expect(resolve("light", "--md-shape-full")).toBe("9999px")
    })

    it.each(["title", "body", "label"].flatMap((role) => ["large", "medium", "small"].map((size) => `${role}-${size}`)))(
        "has the %s type role in rem",
        (role) => {
            expect(resolve("light", `--md-type-${role}-size`)).toMatch(/^[\d.]+rem$/)
            expect(resolve("light", `--md-type-${role}-line-height`)).toMatch(/^[\d.]+rem$/)
            expect(resolve("light", `--md-type-${role}-weight`)).toMatch(/^\d{3}$/)
        },
    )

    it("sizes rows for the pointer and never goes under 44px for a control", () => {
        expect(resolve("light", "--md-target-min")).toBe("44px")
        expect(resolve("light", "--md-target-row")).toBe("48px")
        expect(source).toMatch(/@media \(pointer: coarse\)\s*\{\s*:root\s*\{\s*--md-target-row:\s*56px;/)
    })

    it("uses the bundled Inter as the body font", () => {
        expect(resolve("light", "--md-font-body")).toMatch(/^"Inter"/)
        expect(source).toMatch(/@font-face\s*\{[^}]*font-family:\s*"Inter"/)
    })

    it("caps motion at 150ms", () => {
        expect(resolve("light", "--md-motion-duration")).toBe("150ms")
    })
})
