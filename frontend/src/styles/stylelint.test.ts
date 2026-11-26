import path from "node:path"
import stylelint from "stylelint"
import {expect, it} from "vitest"

async function lintStyle(style: string, name = "NewComponent.vue") {
    return stylelint.lint({
        cwd: frontend,
        configFile,
        code: `<template><div /></template><style scoped>.sample { ${style} }</style>`,
        codeFilename: path.join(frontend, "src/components", name),
    })
}

const frontend = path.resolve(import.meta.dirname, "../..")
const configFile = path.join(frontend, "stylelint.config.mjs")

const TOKEN_RULE = "jailbee/design-tokens"

it("rejects raw colours, sizes, shapes, shadows and motion in an unlisted Vue component", async () => {
    const result = await stylelint.lint({
        cwd: frontend,
        configFile,
        files: "lint-fixtures/raw-values.vue",
    })
    expect(result.errored).toBe(true)
    const warnings = result.results.flatMap((file) => file.warnings)
    expect(warnings.some((warning) => warning.rule === "color-no-hex")).toBe(true)
    const flagged = warnings.filter((warning) => warning.rule === TOKEN_RULE)
        .map((warning) => /in "([^"]+)"/.exec(warning.text)?.[1])
    expect(flagged.sort()).toEqual([
        "border-radius", "box-shadow", "font-size", "height", "letter-spacing", "line-height",
        "max-width", "min-height", "outline-offset", "transform", "transition", "width",
    ])
})

it("allows the shared menu elevation token but not raw shadow fallbacks", async () => {
    expect((await lintStyle('box-shadow: var(--md-elevation-level-2);')).errored).toBe(false)
    expect((await lintStyle('box-shadow: var(--md-elevation-level-2, 0 3px 6px currentColor);')).errored).toBe(true)
})

it("exempts only token declarations in tokens.css; any other rule there fails", async () => {
    const lint = (code: string) => stylelint.lint({
        cwd: frontend, configFile, code, codeFilename: path.join(frontend, "src/styles/tokens.css"),
    })
    const tokens = await lint(`:root { --md-space-8: 8px; --md-primary: #3a5f94; color-scheme: light; }
        @font-face { font-family: "Inter"; font-weight: 100 900; src: url("x.woff2"); }
        @media (pointer: coarse) { :root { --md-target-row: 56px; } }`)
    expect(tokens.results[0].warnings).toEqual([])
    const rule = await lint(".notice { border: var(--md-hairline); padding: 8px; }")
    expect(rule.results[0].warnings.map((warning) => [warning.rule, warning.text])).toEqual([
        [TOKEN_RULE, expect.stringContaining('"border"')],
        [TOKEN_RULE, expect.stringContaining('"padding"')],
    ])
})

it("lints the whole real tree without file exceptions", async () => {
    const config = await stylelint.resolveConfig(path.join(frontend, "src/views/GalleryView.vue"), {configFile})
    expect(config).toBeDefined()
    expect(config?.ignoreFiles ?? []).toEqual([])
    const result = await stylelint.lint({cwd: frontend, configFile, files: "src/**/*.{css,vue}"})
    expect(result.results.flatMap((file) => file.warnings)).toEqual([])
    expect(result.errored).toBe(false)
    expect(result.results.every((file) => !file.ignored)).toBe(true)
    for (const name of ["GalleryView.vue", "highlight.css", "tokens.css"]) {
        expect(result.results.some((file) => file.source?.endsWith(name))).toBe(true)
    }
})

it("enforces standalone stylesheets and declarations inside media queries", async () => {
    const result = await stylelint.lint({
        cwd: frontend, configFile,
        code: "@media (pointer: coarse) { .sample { font-size: 14px; color: red; } }",
        codeFilename: path.join(frontend, "src/styles/new-styles.css"),
    })
    expect(result.errored).toBe(true)
    expect(result.results[0].warnings.map((warning) => warning.rule).sort()).toEqual(["color-named", TOKEN_RULE])
})

it.each([
    "z-index: 999;",
    "padding: 1px 8px;",
    "margin-inline: 12px;",
    "gap: 2px;",
    "border: 1px solid red;",
    "background: rgb(10 20 30);",
    "font: 16px sans-serif;",
    "border-top-width: 3px;",
    "outline: 4px solid var(--md-primary);",
    "inset-block-start: 8px;",
    "padding: var(--md-space-4) 8px;",
    "border-start-start-radius: 50%;",
    "color: var(--md-on-surface, red);",
    "background: linear-gradient(red, blue);",
    "--local-radius: 5px; border-radius: var(--local-radius);",
    "--local-font-size: 1rem; font-size: var(--local-font-size);",
    "--local-layer: 999; z-index: var(--local-layer);",
    "--local-space: 8px; padding: var(--local-space);",
    "--local-colour: red; color: var(--local-colour);",
    "box-shadow: 0 1px 2px var(--md-outline);",
    "box-shadow: inset 0 0 0 1px var(--md-hairline);",
    "width: 320px;",
    "height: 48px;",
    "min-width: 10px;",
    "max-height: calc(100vh - 64px);",
    "line-height: 20px;",
    "outline-offset: 4px;",
    "letter-spacing: 0.5px;",
    "transform: translateX(8px);",
    "transform: translate(0, -4px) scale(1.1);",
    "translate: 4px 0;",
    "transition: opacity 200ms ease;",
    "transition: opacity var(--md-motion-duration), transform .3s;",
    "transition-duration: 0.2s;",
    "transition-delay: 50ms;",
    "animation: spin 1s linear infinite;",
])("rejects raw values in a new unlisted component: %s", async (declaration) => {
    expect((await lintStyle(declaration)).errored).toBe(true)
})

it.each([
    "color: var(--md-on-surface); background: var(--md-surface);",
    "font-size: var(--md-type-body-medium-size); border-radius: var(--md-shape-small);",
    "z-index: var(--md-layer-dialog); padding: 0 var(--md-space-16); gap: var(--md-space-8);",
    "border: 1px solid var(--md-outline); outline: 2px solid currentColor;",
    "border-width: 0 1px 2px; border-style: solid;",
    "border-collapse: collapse; border-spacing: 0;",
    "margin: 0 auto; background: none; color: inherit;",
    "padding-bottom: calc(var(--md-space-16) + env(safe-area-inset-bottom));",
    "inset: 0; width: 100%; grid-template-columns: minmax(5rem, 1fr);",
    "font: var(--md-type-body-medium-size) Inter;",
    "font: 500 var(--md-type-body-medium-size)/var(--md-type-body-medium-line-height) Inter;",
    "--local-space: var(--md-space-8); padding: var(--local-space);",
    "box-shadow: none;",
    "width: 1px; height: 2px; max-width: 100%; min-height: var(--md-target-min);",
    "width: var(--md-switch-track-width); min-width: 0; max-width: min(100%, 40rem);",
    "line-height: 1.5; line-height: var(--md-type-body-small-line-height); letter-spacing: normal;",
    "outline-offset: var(--md-focus-ring-offset); outline-offset: -2px;",
    "transform: translateY(calc(-1 * var(--md-space-4))) rotate(90deg); translate: var(--md-space-4) 0;",
    "transition: opacity var(--md-motion-duration) var(--md-motion-easing); transition: none;",
    "transition-duration: 0s; animation: none;",
])("accepts tokens, layout and hairline widths: %s", async (declaration) => {
    expect((await lintStyle(declaration)).errored).toBe(false)
})
