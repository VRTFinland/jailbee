import {readFileSync, readdirSync} from "node:fs"
import path from "node:path"
import {expect, it} from "vitest"
import ts from "typescript"

function importsReka(source: string) {
    const scripts = [...source.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)]
    const code = scripts.length ? scripts.map((match) => match[1]).join("\n") : source
    const file = ts.createSourceFile("source.ts", code, ts.ScriptTarget.Latest, true)
    let found = false
    function visit(node: ts.Node) {
        if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier
            && ts.isStringLiteral(node.moduleSpecifier)
            && /^reka-ui(?:\/|$)/.test(node.moduleSpecifier.text)) found = true
        if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword
            && node.arguments[0] && ts.isStringLiteral(node.arguments[0])
            && /^reka-ui(?:\/|$)/.test(node.arguments[0].text)) found = true
        ts.forEachChild(node, visit)
    }
    visit(file)
    return found
}

it.each([
    'import "reka-ui"', 'import {DialogRoot} from "reka-ui"',
    'export {DialogRoot} from "reka-ui"', 'const lib = import("reka-ui")',
])("detects a forbidden dependency: %s", (source) => {
    expect(importsReka(source)).toBe(true)
})

it("keeps Reka UI imports inside the UI primitives", () => {
    const root = path.resolve(import.meta.dirname, "../..")
    const violations = readdirSync(root, {recursive: true}).filter((name) => {
        const file = String(name)
        return /\.(vue|ts)$/.test(file) && !file.endsWith(".test.ts") && !file.startsWith("components/ui/")
            && importsReka(readFileSync(path.join(root, file), "utf8"))
    })
    expect(violations).toEqual([])
})
