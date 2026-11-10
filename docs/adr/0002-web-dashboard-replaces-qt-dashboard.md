# The web dashboard replaces the Qt dashboard

JailBee is getting a browser counterpart to the terminal dashboard. The motivating case is reading agent transcripts on an e-ink tablet, a phone or another machine. Long-term, the browser version reaches parity with the terminal dashboard, including actions behind a remote policy. Once it does, a third, desktop-only frontend earns nothing. The Qt dashboard therefore gets fixes only, no new features. It is deprecated when the web dashboard reaches parity and removed a release later. See #108.

## Considered options

- **Keep three frontends.** Rejected. Every view and every prompt would be built three times. The Qt dashboard already carries its own copy of the CLI's questions (pre-ask dialogs that pass flags), and that copy drifts from the CLI.
- **Grow the Qt dashboard into the remote surface.** Rejected. It cannot run on a tablet or phone, and over SSH it is refused outright.

## Consequences

- New dashboard features target the terminal and web dashboards only.
- The prompt channel (ADR 0003) is not wired into the Qt dashboard.
