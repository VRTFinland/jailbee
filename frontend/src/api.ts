// REST layer and session token. Ported from codereader's api.ts.
import {readItem, removeItem, writeItem} from "./storage"

const TOKEN_KEY = "jailbee.token"
const DEVICE_KEY = "jailbee.device"

export function getToken(): string | null {
    return readItem(TOKEN_KEY)
}

export function setToken(token: string): void {
    writeItem(TOKEN_KEY, token)
}

export function clearToken(): void {
    removeItem(TOKEN_KEY)
}

export function rememberedDeviceName(): string {
    return readItem(DEVICE_KEY) ?? ""
}

export class Unauthorized extends Error {
    constructor() {
        super("Unauthorized")
    }
}

/** An authenticated JSON request under /api. A 401 drops the token. */
export async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
    const headers = new Headers(options.headers)
    headers.set("Content-Type", "application/json")
    const token = getToken()
    if (token) headers.set("Authorization", `Bearer ${token}`)
    const res = await fetch(path, {...options, headers})
    if (res.status === 401) {
        clearToken()
        throw new Unauthorized()
    }
    if (!res.ok) {
        throw new Error(`API error ${res.status}: ${await res.text()}`)
    }
    return (await res.json()) as T
}

export type LoginResult = {ok: true} | {ok: false; error: string}

export type AuthMethod = "pin" | "totp"

/** The login methods the server offers; PIN only if it cannot be asked. */
export async function fetchAuthMethods(): Promise<AuthMethod[]> {
    try {
        const res = await fetch("/api/auth/methods")
        if (res.ok) {
            const data = (await res.json()) as {methods: string[]}
            const known = data.methods.filter((m): m is AuthMethod => m === "pin" || m === "totp")
            if (known.length > 0) return known
        }
    } catch {
        // fall through to the PIN
    }
    return ["pin"]
}

async function login(
    body: Record<string, string>,
    deviceName: string,
    refused: string,
): Promise<LoginResult> {
    let res: Response
    try {
        res = await fetch("/api/auth", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({...body, device_name: deviceName}),
        })
    } catch {
        return {ok: false, error: "The server cannot be reached."}
    }
    if (res.ok) {
        const data = (await res.json()) as {token: string}
        setToken(data.token)
        writeItem(DEVICE_KEY, deviceName)
        return {ok: true}
    }
    if (res.status === 429) {
        return {ok: false, error: "Too many attempts. Wait a minute and try again."}
    }
    return {ok: false, error: refused}
}

export function loginWithPin(pin: string, deviceName: string): Promise<LoginResult> {
    return login(
        {method: "pin", pin},
        deviceName,
        "Wrong or used PIN. The server printed a new one.",
    )
}

export function loginWithTotp(code: string, deviceName: string): Promise<LoginResult> {
    return login(
        {method: "totp", code},
        deviceName,
        "Wrong or already used code. Wait for the next one and try again.",
    )
}

export async function logout(): Promise<void> {
    try {
        await apiFetch("/api/logout", {method: "POST"})
    } catch {
        // the token is dropped regardless
    }
    clearToken()
}
