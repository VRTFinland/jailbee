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

it("rejects raw colours, font sizes and shapes in an unlisted Vue component", async () => {
    const result = await stylelint.lint({
        cwd: frontend,
        configFile,
        files: "lint-fixtures/raw-values.vue",
    })
    expect(result.errored).toBe(true)
    const warnings = result.results.flatMap((file) => file.warnings)
    for (const property of ["color", "font-size", "border-radius"]) {
        expect(warnings.some((warning) => warning.text.includes(property))).toBe(true)
    }
})

it("lints the real tree with only the explicitly exempted legacy files", async () => {
    const config = await stylelint.resolveConfig(path.join(frontend, "src/views/GalleryView.vue"), {configFile})
    expect(config).toBeDefined()
    for (const exception of config?.ignoreFiles ?? []) {
        expect(exception).not.toMatch(/[*!?{\[]/)
    }
    const result = await stylelint.lint({cwd: frontend, configFile, files: "src/**/*.{css,vue}"})
    expect(result.results.flatMap((file) => file.warnings)).toEqual([])
    expect(result.errored).toBe(false)
    expect(result.results.some((file) => file.source?.endsWith("GalleryView.vue") && !file.ignored)).toBe(true)
})

it("enforces standalone stylesheets and declarations inside media queries", async () => {
    const result = await stylelint.lint({
        cwd: frontend, configFile,
        code: "@media (pointer: coarse) { .sample { font-size: 14px; color: red; } }",
        codeFilename: path.join(frontend, "src/styles/new-styles.css"),
    })
    expect(result.errored).toBe(true)
    expect(result.results[0].warnings).toHaveLength(2)
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
])("rejects raw values in a new unlisted component: %s", async (declaration) => {
    expect((await lintStyle(declaration)).errored).toBe(true)
})

it.each([
    "color: var(--md-on-surface); background: var(--md-surface);",
    "font-size: var(--md-type-body-medium-size); border-radius: var(--md-shape-small);",
    "z-index: var(--md-layer-dialog); padding: 0 var(--md-space-16); gap: var(--md-space-8);",
    "border: 1px solid var(--md-outline); outline: 2px solid currentColor;",
    "border-width: 0 1px 2px; border-style: solid;",
    "margin: 0 auto; background: none; color: inherit;",
    "padding-bottom: calc(var(--md-space-16) + env(safe-area-inset-bottom));",
    "inset: 0; width: 100%; grid-template-columns: minmax(5rem, 1fr);",
    "font: var(--md-type-body-medium-size) Inter;",
    "font: 500 var(--md-type-body-medium-size)/var(--md-type-body-medium-line-height) Inter;",
    "--local-space: var(--md-space-8); padding: var(--local-space);",
])("accepts tokens, layout and hairline widths: %s", async (declaration) => {
    expect((await lintStyle(declaration)).errored).toBe(false)
})
