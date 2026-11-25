import {readFileSync, readdirSync} from "node:fs"
import path from "node:path"
import {expect, it} from "vitest"

it("keeps Reka UI imports inside the UI primitives", () => {
    const root = path.resolve(import.meta.dirname, "../..")
    const violations = readdirSync(root, {recursive: true}).filter((name) => {
        const file = String(name)
        return /\.(vue|ts)$/.test(file) && !file.endsWith(".test.ts") && !file.startsWith("components/ui/")
            && /(?:from\s*|import\s*\()["']reka-ui(?:["'/])/.test(readFileSync(path.join(root, file), "utf8"))
    })
    expect(violations).toEqual([])
})
