"""Browser hardening headers on every HTTP response, as one ASGI middleware.

The session token lives in `localStorage` and transcripts render through
`v-html`, so the policy allows only what the built app ships: scripts,
styles, the manifest, icons and the `/api/ws` socket, all from the page's
own origin (CSP3 `'self'` covers same-origin `ws:`/`wss:`). The Vite build
inlines no script or style and the server-rendered HTML carries classes, not
`style` attributes, so no `'unsafe-inline'`. Reka UI's SelectViewport injects one
fixed scrollbar stylesheet, allowed by its exact hash (the Chrome overlay tests
catch changes to that stylesheet when upgrading Reka UI). `frame-ancestors` (and the older
`X-Frame-Options`) stop framing, `nosniff` stops type sniffing, and
`no-referrer` keeps URLs out of other sites' logs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        # Reka UI SelectViewport's fixed scrollbar CSS (reka-ui 2.11.0).
        # A hash permits exactly that stylesheet, not arbitrary inline styles.
        "style-src 'self' 'sha256-60LHlRjW/B3CtzIoE/Lf1/NEDvko9efWMFaGVhHu/cs='",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)

HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
)


class SecurityHeaders:
    """Add `HEADERS` to every HTTP response of the wrapped app."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {name for name, _ in HEADERS}
                kept = [(k, v) for k, v in message.get("headers", []) if k.lower() not in names]
                message = {**message, "headers": [*kept, *HEADERS]}
            await send(message)

        await self.app(scope, receive, send_with_headers)
