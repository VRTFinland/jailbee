// REST layer and session token. Ported from codereader's api.ts.
import {readItem, removeItem, writeItem} from "./storage"
import {assertionToJSON, requestOptionsFromJSON, webauthnError} from "./webauthn"

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

const LINK_DEVICE = "Local browser"

/** Exchange the one-time token of `jailbee remote web open` for a session. */
export function loginWithLink(token: string): Promise<LoginResult> {
    return login(
        {method: "link", token},
        rememberedDeviceName() || LINK_DEVICE,
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

/** Sign the server's challenge with a security key or passkey (WebAuthn). */
export async function loginWithSecurityKey(
    deviceName: string,
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
        deviceName,
        "The security key was not accepted.",
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
