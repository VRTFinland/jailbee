import {describe, expect, it} from "vitest"
import {readPosition, writePosition} from "./readingPosition"

describe("reading positions", () => {
    it("ignores storage failures", () => {
        const store = {getItem: () => { throw new Error("blocked") }, setItem: () => { throw new Error("quota") }}
        expect(readPosition(store, ["x"])).toBeNull()
        expect(() => writePosition(store, ["x"], {entryId: null, atEnd: false})).not.toThrow()
    })

    it("keeps parent and subagent positions separate and evicts the oldest", () => {
        const storage = new Map<string, string>()
        const store = {getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => { storage.set(key, value) }}
        const parent = ["repo", "agent", "session"]
        const child = [...parent, "child"]
        writePosition(store, parent, {entryId: "a", atEnd: false}, 2)
        writePosition(store, child, {entryId: "b", atEnd: true}, 2)
        expect(readPosition(store, parent)?.entryId).toBe("a")
        expect(readPosition(store, child)?.entryId).toBe("b")
        writePosition(store, ["other"], {entryId: "c", atEnd: false}, 2)
        expect(readPosition(store, parent)).toBeNull()
        expect(readPosition(store, child)?.entryId).toBe("b")
    })
})
