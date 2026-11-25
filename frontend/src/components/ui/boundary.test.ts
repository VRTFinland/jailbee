import {readFileSync, readdirSync} from "node:fs"
import path from "node:path"
import {expect, it} from "vitest"
import ts from "typescript"

function rekaDependency(source: string) {
    const scripts = [...source.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)]
    const code = scripts.length ? scripts.map((match) => match[1]).join("\n") : source
    const file = ts.createSourceFile("source.ts", code, ts.ScriptTarget.Latest, true)
    let imported = false
    let exported = false
    function visit(node: ts.Node) {
        if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier
            && ts.isStringLiteral(node.moduleSpecifier)
            && /^reka-ui(?:\/|$)/.test(node.moduleSpecifier.text)) {
            if (ts.isExportDeclaration(node)) exported = true
            else imported = true
        }
        if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword
            && node.arguments[0] && ts.isStringLiteral(node.arguments[0])
            && /^reka-ui(?:\/|$)/.test(node.arguments[0].text)) imported = true
        ts.forEachChild(node, visit)
    }
    visit(file)
    return {imported, exported}
}

function forbidden(file: string, source: string) {
    const {imported, exported} = rekaDependency(source)
    return exported || (imported && !file.startsWith("components/ui/"))
}

it.each([
    'import "reka-ui"', 'import {DialogRoot} from "reka-ui"',
    'export {DialogRoot} from "reka-ui"', 'export * from "reka-ui"', 'const lib = import("reka-ui")',
])("detects a forbidden view dependency: %s", (source) => {
    expect(forbidden("views/Example.vue", source)).toBe(true)
})

it.each(['export * from "reka-ui"', 'export {DialogRoot as Dialog} from "reka-ui"'])
("refuses primitive re-exports: %s", (source) => {
    expect(forbidden("components/ui/index.ts", source)).toBe(true)
})

it("scans the real views and keeps Reka UI private to primitives", () => {
    const root = path.resolve(import.meta.dirname, "../..")
    const files = readdirSync(root, {recursive: true}).map(String)
        .filter((file) => /\.(vue|ts)$/.test(file) && !file.endsWith(".test.ts"))
    expect(files).toContain("views/GalleryView.vue")
    expect(files).toContain("components/ui/Dialog.vue")
    expect(rekaDependency(readFileSync(path.join(root, "components/ui/Dialog.vue"), "utf8")).imported).toBe(true)
    expect(files.filter((file) => forbidden(file, readFileSync(path.join(root, file), "utf8")))).toEqual([])
})
