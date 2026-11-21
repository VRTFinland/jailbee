"""A software FIDO2 authenticator for the web suite: what a browser + security key send.

Holds one P-256 key per credential and answers the `options` JSON the server
hands the browser with the JSON `PublicKeyCredential` a browser would post
back (base64url fields, as `frontend/src/webauthn.ts` encodes them). The
origin is the page origin the browser would put in `clientDataJSON`.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fido2.cose import ES256
from fido2.utils import websafe_decode, websafe_encode
from fido2.webauthn import (
    AttestationObject,
    AttestedCredentialData,
    AuthenticatorData,
    CollectedClientData,
)


@dataclass
class SoftAuthenticator:
    origin: str
    counter: int = 0
    keys: dict[bytes, ec.EllipticCurvePrivateKey] = field(default_factory=dict)

    def create(self, options: dict[str, Any], *, rp_id: str | None = None) -> dict[str, Any]:
        """Answer `navigator.credentials.create({publicKey: options})`."""
        rp = rp_id or options["rp"]["id"]
        key = ec.generate_private_key(ec.SECP256R1())
        cred_id = os.urandom(16)
        self.keys[cred_id] = key
        acd = AttestedCredentialData.create(
            b"\0" * 16, cred_id, ES256.from_cryptography_key(key.public_key())
        )
        auth_data = AuthenticatorData.create(
            hashlib.sha256(rp.encode()).digest(),
            AuthenticatorData.FLAG.UP | AuthenticatorData.FLAG.AT,
            0,
            acd,
        )
        client = CollectedClientData.create(
            type=CollectedClientData.TYPE.CREATE,
            challenge=websafe_decode(options["challenge"]),
            origin=self.origin,
        )
        attestation = AttestationObject.create("none", auth_data, {})
        return {
            "id": websafe_encode(cred_id),
            "rawId": websafe_encode(cred_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": websafe_encode(client),
                "attestationObject": websafe_encode(attestation),
            },
        }

    def get(
        self, options: dict[str, Any], *, rp_id: str | None = None, counter: int | None = None
    ) -> dict[str, Any]:
        """Answer `navigator.credentials.get({publicKey: options})` with the first allowed key."""
        allowed = [websafe_decode(c["id"]) for c in options.get("allowCredentials", [])]
        cred_id = next(c for c in allowed if c in self.keys)
        rp = rp_id or options["rpId"]
        if counter is None:
            self.counter += 1
            counter = self.counter
        auth_data = AuthenticatorData.create(
            hashlib.sha256(rp.encode()).digest(), AuthenticatorData.FLAG.UP, counter
        )
        client = CollectedClientData.create(
            type=CollectedClientData.TYPE.GET,
            challenge=websafe_decode(options["challenge"]),
            origin=self.origin,
        )
        signature = self.keys[cred_id].sign(
            bytes(auth_data) + client.hash, ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": websafe_encode(cred_id),
            "rawId": websafe_encode(cred_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": websafe_encode(client),
                "authenticatorData": websafe_encode(auth_data),
                "signature": websafe_encode(signature),
            },
        }
