// Browser storage that may be missing or refuse access (private mode,
// blocked site data): every read and write is wrapped, and a failure just
// means "not remembered".

export function readItem(key: string): string | null {
    try {
        return window.localStorage.getItem(key)
    } catch {
        return null
    }
}

export function writeItem(key: string, value: string): void {
    try {
        window.localStorage.setItem(key, value)
    } catch {
        // not remembered
    }
}

export function removeItem(key: string): void {
    try {
        window.localStorage.removeItem(key)
    } catch {
        // nothing to forget
    }
}
