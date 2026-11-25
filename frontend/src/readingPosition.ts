export interface ReadingPosition {
    entryId: string | null
    atEnd: boolean
}

type Store = Pick<Storage, "getItem" | "setItem">
const KEY = "jailbee.readingPositions"
const LIMIT = 100

type RecordEntry = {key: string, position: ReadingPosition}

function records(store: Store): RecordEntry[] {
    try {
        const value: unknown = JSON.parse(store.getItem(KEY) ?? "[]")
        if (!Array.isArray(value)) return []
        return value.filter((item): item is RecordEntry =>
            typeof item?.key === "string" && typeof item?.position?.atEnd === "boolean" &&
            (typeof item.position.entryId === "string" || item.position.entryId === null))
    } catch { return [] }
}

export function readPosition(store: Store, key: string[]): ReadingPosition | null {
    return records(store).find(item => item.key === JSON.stringify(key))?.position ?? null
}

export function writePosition(store: Store, key: string[], position: ReadingPosition, limit = LIMIT): void {
    try {
        const encoded = JSON.stringify(key)
        const next = records(store).filter(item => item.key !== encoded)
        next.push({key: encoded, position})
        store.setItem(KEY, JSON.stringify(next.slice(-limit)))
    } catch { /* Browser storage can be unavailable or full. */ }
}
