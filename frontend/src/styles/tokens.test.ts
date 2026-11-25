import {readdirSync, readFileSync} from "node:fs"
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
    // Notices sit on the primary container and carry body, muted and link text.
    ["--md-on-surface", "--md-primary-container"],
    ["--md-on-surface-variant", "--md-primary-container"],
    ["--md-primary", "--md-primary-container"],
    ["--md-error", "--md-surface"],
    ["--md-on-error", "--md-error"],
    ["--md-on-error-container", "--md-error-container"],
    ...PHASES.flatMap((p): [string, string][] => [
        [`--md-phase-${p}`, "--md-surface"],
        [`--md-phase-${p}`, "--md-surface-container-lowest"],
        [`--md-phase-${p}`, "--md-surface-container-low"],
        [`--md-phase-${p}`, "--md-surface-container"],
        [`--md-phase-${p}`, "--md-surface-container-high"],
        [`--md-phase-${p}`, "--md-surface-container-highest"],
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

    // Each old name and the md token it now stands for (ticket 14 removes them).
    const ALIASES: Record<string, string> = {
        "--bg": "--md-surface",
        "--text": "--md-on-surface",
        "--muted": "--md-on-surface-variant",
        "--border": "--md-outline-variant",
        "--border-strong": "--md-outline",
        "--accent": "--md-primary",
        "--accent-text": "--md-on-primary",
        "--danger": "--md-error",
        "--notice-bg": "--md-primary-container",
        "--phase-booting": "--md-phase-booting",
        "--phase-autostart": "--md-phase-autostart",
        "--phase-job": "--md-phase-job",
        "--phase-failed": "--md-phase-failed",
        "--font-body": "--md-font-body",
        "--font-mono": "--md-font-mono",
        "--space-1": "--md-space-4",
        "--space-2": "--md-space-8",
        "--space-3": "--md-space-16",
        "--space-4": "--md-space-24",
        "--radius": "--md-shape-small",
    }

    it.each(Object.entries(ALIASES))("keeps the old name %s as an alias of %s in every theme", (name, target) => {
        for (const theme of THEMES) expect(declared(theme).get(name), `${name} in ${theme}`).toBe(`var(${target})`)
    })

    it("keeps --surface a card surface: the lowest container in light, one tone above the page in dark", () => {
        expect(declared("light").get("--surface")).toBe("var(--md-surface-container-lowest)")
        expect(declared("eink").get("--surface")).toBe("var(--md-surface-container-lowest)")
        expect(declared("dark").get("--surface")).toBe("var(--md-surface-container-low)")
    })

    it("keeps the old spacing values", () => {
        expect(["--space-1", "--space-2", "--space-3", "--space-4"].map((n) => resolve("light", n)))
            .toEqual(["0.25rem", "0.5rem", "1rem", "1.5rem"])
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

    it("has one hit-target token per use, none duplicating another", () => {
        const targets = [...declared("light").keys()].filter((n) => n.startsWith("--md-target-")).sort()
        expect(targets).toEqual(["--md-target-min", "--md-target-row"])
    })

    // Spacing tokens are named by their size in px at the default 16px root,
    // so `--md-space-16` is 1rem: a name never hides its value, and a
    // mechanical rename to an index (`--space-3` -> `--md-space-3`) lands on
    // an undefined token, which the test below rejects.
    it("names each spacing token by its px size", () => {
        const spaces = [...declared("light").keys()].filter((n) => n.startsWith("--md-space-"))
        expect(spaces.length).toBeGreaterThanOrEqual(6)
        for (const name of spaces) {
            const px = Number(name.slice("--md-space-".length))
            expect(resolve("light", name), name).toBe(`${px / 16}rem`)
        }
    })

    it("defines every --md token the app's sources use", () => {
        const files = (readdirSync("src", {recursive: true}) as string[])
            .filter((f) => /\.(vue|css|ts)$/.test(f) && !f.endsWith(".test.ts"))
        const defined = new Set(declared("light").keys())
        const missing: string[] = []
        for (const file of files) {
            for (const [, name] of readFileSync(`src/${file}`, "utf8").matchAll(/var\((--md-[\w-]+)[,)]/g)) {
                if (!defined.has(name)) missing.push(`${file}: ${name}`)
            }
        }
        expect(missing).toEqual([])
    })

    it("stacks layers so a dialog sits above its own scrim and a menu above both", () => {
        const layer = (n: string) => Number(resolve("light", `--md-layer-${n}`))
        const order = ["raised", "sticky", "scrim", "dialog", "popover"].map(layer)
        expect(order).toEqual([...order].sort((a, b) => a - b))
        expect(new Set(order).size).toBe(order.length)
    })

    it("uses the bundled Inter as the body font", () => {
        expect(resolve("light", "--md-font-body")).toMatch(/^"Inter"/)
        expect(source).toMatch(/@font-face\s*\{[^}]*font-family:\s*"Inter"/)
    })

    it("keeps the page and app-manifest colours in step with the surfaces", () => {
        const html = readFileSync("index.html", "utf8")
        const manifest = JSON.parse(readFileSync("public/manifest.webmanifest", "utf8")) as Record<string, string>
        expect(html).toContain(`<meta name="theme-color" content="${resolve("light", "--md-surface")}" />`)
        expect(manifest.theme_color).toBe(resolve("dark", "--md-surface"))
        expect(manifest.background_color).toBe(resolve("dark", "--md-surface"))
    })

    it("caps motion at 150ms", () => {
        expect(resolve("light", "--md-motion-duration")).toBe("150ms")
    })
})
