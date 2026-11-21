// Setup mode (`jailbee remote web auth fido2 setup`): register a security key.
// Only the setup server answers these endpoints; the dashboard answers 404.
import {attestationToJSON, creationOptionsFromJSON, webauthnError} from "./webauthn"

export type RegisterResult = {ok: true; name: string} | {ok: false; error: string}

const NOT_SETUP =
    "This server is not in setup mode. Run `jailbee remote web auth fido2 setup` on the host, then reload."

async function post(path: string, body: unknown): Promise<Response> {
    return fetch(path, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
    })
}

export async function registerSecurityKey(
    pin: string,
    name: string,
    credentials: CredentialsContainer = navigator.credentials,
): Promise<RegisterResult> {
    let begin: {challenge_id: string; options: Record<string, unknown>}
    try {
        const res = await post("/api/setup/fido2/begin", {pin})
        if (res.status === 404 || res.status === 405) return {ok: false, error: NOT_SETUP}
        if (res.status === 401) return {ok: false, error: "Wrong or used setup PIN. The server printed a new one."}
        if (res.status === 429) return {ok: false, error: "Too many attempts. Wait a minute and try again."}
        if (!res.ok) return {ok: false, error: `Setup failed (${res.status}).`}
        begin = await res.json()
    } catch {
        return {ok: false, error: "The server cannot be reached."}
    }
    let credential: Credential | null
    try {
        credential = await credentials.create({publicKey: creationOptionsFromJSON(begin.options)})
    } catch (e) {
        return {ok: false, error: webauthnError(e)}
    }
    if (!credential) return {ok: false, error: "The security key request was cancelled."}
    try {
        const res = await post("/api/setup/fido2/complete", {
            challenge_id: begin.challenge_id,
            name,
            credential: attestationToJSON(credential as PublicKeyCredential),
        })
        const data = (await res.json()) as {name?: string; detail?: string}
        if (!res.ok) return {ok: false, error: data.detail ?? `Registration failed (${res.status}).`}
        return {ok: true, name: data.name ?? name}
    } catch {
        return {ok: false, error: "The server cannot be reached."}
    }
}
