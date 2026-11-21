// WebAuthn JSON <-> browser objects. The server (python-fido2) speaks JSON with
// base64url binary fields; `navigator.credentials` wants and returns buffers.
// Ported from codereader's LoginView/AuthSetupView helpers.

export function base64urlToBuffer(value: string): ArrayBuffer {
    const base64 = value.replace(/-/g, "+").replace(/_/g, "/")
    const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4)
    const binary = atob(padded)
    const out = new Uint8Array(binary.length)
    for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i)
    return out.buffer
}

export function bufferToBase64url(buffer: ArrayBuffer): string {
    let binary = ""
    for (const b of new Uint8Array(buffer)) binary += String.fromCharCode(b)
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
}

type JsonDescriptor = {type: string; id: string; transports?: string[]}

function descriptors(list: JsonDescriptor[] | undefined): PublicKeyCredentialDescriptor[] {
    return (list ?? []).map((c) => ({
        type: c.type as PublicKeyCredentialType,
        id: base64urlToBuffer(c.id),
        transports: c.transports as AuthenticatorTransport[] | undefined,
    }))
}

/** `publicKey` options for `navigator.credentials.get` from the server's JSON. */
export function requestOptionsFromJSON(json: Record<string, unknown>): PublicKeyCredentialRequestOptions {
    return {
        ...(json as object),
        challenge: base64urlToBuffer(json.challenge as string),
        allowCredentials: descriptors(json.allowCredentials as JsonDescriptor[] | undefined),
    } as PublicKeyCredentialRequestOptions
}

/** `publicKey` options for `navigator.credentials.create` from the server's JSON. */
export function creationOptionsFromJSON(json: Record<string, unknown>): PublicKeyCredentialCreationOptions {
    const user = json.user as {id: string; name: string; displayName: string}
    return {
        ...(json as object),
        challenge: base64urlToBuffer(json.challenge as string),
        user: {...user, id: base64urlToBuffer(user.id)},
        excludeCredentials: descriptors(json.excludeCredentials as JsonDescriptor[] | undefined),
    } as PublicKeyCredentialCreationOptions
}

type BrowserCredential = {id: string; rawId: ArrayBuffer; type: string; response: unknown}

/** An assertion from `navigator.credentials.get`, as the server's JSON. */
export function assertionToJSON(credential: BrowserCredential): Record<string, unknown> {
    const r = credential.response as AuthenticatorAssertionResponse
    return {
        id: credential.id,
        rawId: bufferToBase64url(credential.rawId),
        type: credential.type,
        response: {
            clientDataJSON: bufferToBase64url(r.clientDataJSON),
            authenticatorData: bufferToBase64url(r.authenticatorData),
            signature: bufferToBase64url(r.signature),
            userHandle: r.userHandle ? bufferToBase64url(r.userHandle) : null,
        },
    }
}

/** A new credential from `navigator.credentials.create`, as the server's JSON. */
export function attestationToJSON(credential: BrowserCredential): Record<string, unknown> {
    const r = credential.response as AuthenticatorAttestationResponse
    return {
        id: credential.id,
        rawId: bufferToBase64url(credential.rawId),
        type: credential.type,
        response: {
            clientDataJSON: bufferToBase64url(r.clientDataJSON),
            attestationObject: bufferToBase64url(r.attestationObject),
        },
    }
}

/** Why a WebAuthn call failed, in words a user can act on. */
export function webauthnError(e: unknown): string {
    if (e instanceof DOMException && e.name === "NotAllowedError") {
        return "The security key request was cancelled or timed out."
    }
    if (typeof window !== "undefined" && !window.isSecureContext) {
        return "Security keys need HTTPS (or localhost)."
    }
    return e instanceof Error ? e.message : "The security key request failed."
}
