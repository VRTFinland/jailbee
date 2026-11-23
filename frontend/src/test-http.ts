// View-only tests keep their endpoint responses separate from router discovery.
export function withDashboardMode(fetch: typeof globalThis.fetch): typeof globalThis.fetch {
    return (input, init) => input === "/api/mode"
        ? Promise.resolve(Response.json({mode: "dashboard"}))
        : fetch(input, init)
}
