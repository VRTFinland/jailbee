# Web dashboard stack: FastAPI and a bundled Vue app

The web dashboard is a FastAPI server, shipped as the optional extra `jailbee[web]`, serving a Vue 3 + TypeScript single-page app.

The app is built with Node by `make web` at release time and bundled into the wheel. Built assets are never committed. The app has no CDN assets and no UI component library: themes and the e-ink display profile are its own design tokens. The server is a host-side `StateClient` consumer like the terminal dashboard. It never gathers state itself. See #109.

The stack mirrors the author's codereader project, which already proved this exact shape on e-ink tablets.

## Considered options

- **Server-rendered HTML with little or no JavaScript.** Rejected. Later phases need a browser terminal, live dialogs over a WebSocket and full terminal-dashboard parity, which favour an SPA.
- **A UI component library such as Vuetify or PrimeVue.** Rejected. Their animations, shadows and small hit targets fight the e-ink profile.

## Consequences

- The release pipeline gains a Node build step.
- An editable install without built assets refuses to serve, and says to run `make web`.
