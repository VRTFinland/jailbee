# Web dashboard auth is ported from codereader, and source addresses are never trusted

The web dashboard's login is ported from the author's codereader project:

- a one-time PIN;
- TOTP;
- FIDO2, with the relying-party ID taken from the request `Origin`;
- 30-day sliding session tokens.

It is ported as code adapted to JailBee's rules (no module state, mypy strict, JailBee's database), not shared as a library. Codereader stays a separate project.

Auth can be disabled for local-only use. It is never relaxed based on the request's source address. Behind a reverse proxy or an SSH tunnel every request comes from localhost, so a "trust localhost" rule would open the dashboard to the internet.

The safe ways to make local use easier are these:

- a one-time localhost login link from the CLI;
- disabling auth outright, in which case the server refuses a non-loopback bind unless explicitly allowed.

See #109.

## Considered options

- **A shared auth package used by both projects.** Rejected for now. It would mean a third repository to version and release for roughly a thousand lines that need adapting anyway.
- **Skip auth when the request comes from loopback.** Rejected for the proxy and tunnel reason above.
