// REST layer and session token. Ported from codereader's api.ts.
import {readItem, removeItem, writeItem} from "./storage"
import {assertionToJSON, requestOptionsFromJSON, webauthnError} from "./webauthn"

const TOKEN_KEY = "jailbee.token"

export function getToken(): string | null {
    return readItem(TOKEN_KEY)
}

export function setToken(token: string): void {
    writeItem(TOKEN_KEY, token)
}

export function clearToken(): void {
    removeItem(TOKEN_KEY)
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

export type AuthMethod = "pin" | "totp" | "fido2"

const AUTH_METHODS: readonly string[] = ["pin", "totp", "fido2"]

/** The login methods the server offers; PIN only if it cannot be asked. */
export async function fetchAuthMethods(): Promise<AuthMethod[]> {
    try {
        const res = await fetch("/api/auth/methods")
        if (res.ok) {
            const data = (await res.json()) as {methods: string[]}
            const known = data.methods.filter((m): m is AuthMethod => AUTH_METHODS.includes(m))
            if (known.length > 0) return known
        }
    } catch {
        // fall through to the PIN
    }
    return ["pin"]
}

async function login(
    body: Record<string, unknown>,
    refused: string,
): Promise<LoginResult> {
    let res: Response
    try {
        res = await fetch("/api/auth", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(body),
        })
    } catch {
        return {ok: false, error: "The server cannot be reached."}
    }
    if (res.ok) {
        const data = (await res.json()) as {token: string}
        setToken(data.token)
        return {ok: true}
    }
    if (res.status === 429) {
        const seconds = Math.ceil(Number(res.headers.get("Retry-After")))
        const amount = seconds >= 60 ? Math.ceil(seconds / 60) : seconds
        const unit = seconds >= 60 ? "minute" : "second"
        const wait = Number.isFinite(amount) && amount > 0
            ? `${amount} ${unit}${amount === 1 ? "" : "s"}`
            : "a while"
        return {ok: false, error: `Too many attempts. Wait ${wait} and try again.`}
    }
    return {ok: false, error: refused}
}

/** Exchange the one-time token of `jailbee remote web open` for a session. */
export function loginWithLink(token: string): Promise<LoginResult> {
    return login(
        {method: "link", token},
        "This login link was already used or has expired. Run `jailbee remote web open` again.",
    )
}

/** Whether the server runs without login (`remote.web.auth.enabled: false`). */
export async function authIsDisabled(): Promise<boolean> {
    try {
        const res = await fetch("/api/auth/methods")
        if (!res.ok) return false
        return ((await res.json()) as {auth_disabled?: boolean}).auth_disabled === true
    } catch {
        return false
    }
}

/** Stands in for a token when the server asks for none; the server ignores it. */
export const NO_LOGIN_TOKEN = "no-login"

export function loginWithPin(pin: string): Promise<LoginResult> {
    return login(
        {method: "pin", pin},
        "Wrong or used PIN. The server printed a new one.",
    )
}

export function loginWithTotp(code: string): Promise<LoginResult> {
    return login(
        {method: "totp", code},
        "Wrong or already used code. Wait for the next one and try again.",
    )
}

/** Sign the server's challenge with a security key or passkey (WebAuthn). */
export async function loginWithSecurityKey(
    credentials: CredentialsContainer = navigator.credentials,
): Promise<LoginResult> {
    let begin: {challenge_id: string; options: Record<string, unknown>}
    try {
        const res = await fetch("/api/auth/fido2/begin", {method: "POST"})
        if (!res.ok) return {ok: false, error: "No security key is registered for this address."}
        begin = await res.json()
    } catch {
        return {ok: false, error: "The server cannot be reached."}
    }
    let credential: Credential | null
    try {
        credential = await credentials.get({publicKey: requestOptionsFromJSON(begin.options)})
    } catch (e) {
        return {ok: false, error: webauthnError(e)}
    }
    if (!credential) return {ok: false, error: "The security key request was cancelled."}
    return login(
        {
            method: "fido2",
            challenge_id: begin.challenge_id,
            credential: assertionToJSON(credential as PublicKeyCredential),
        },
        "The security key was not accepted.",
    )
}

/** The caller's session; the server names it from the browser until it is renamed. */
export function fetchSession(): Promise<{device_name: string}> {
    return apiFetch("/api/session")
}

/** Rename the caller's session; resolves to the stored (trimmed) name. */
export async function renameSession(deviceName: string): Promise<string> {
    const data = await apiFetch<{device_name: string}>("/api/session", {
        method: "PUT",
        body: JSON.stringify({device_name: deviceName}),
    })
    return data.device_name
}

export async function logout(): Promise<void> {
    try {
        await apiFetch("/api/logout", {method: "POST"})
    } catch {
        // the token is dropped regardless
    }
    clearToken()
}
