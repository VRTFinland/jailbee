# Web dashboard

The web dashboard is a browser view of the same host state the terminal
dashboard shows: the front page of repositories and containers, per-repo and
per-container session lists, and a readable, live-following transcript of an
agent session. It is read-only for now, runs on the host as the current user,
and is installed as an optional extra.

Which container ran a session is recorded by the shared state service, which
runs only while some dashboard (terminal, Qt or web) is connected. A session
that starts and ends with no dashboard open is still listed under its
repository, but not attributed to a container.

Session titles use the first typed prompt, skipping agent wrapper records,
local command output, caveats and injected context. A session with only slash
commands uses its first command (for example, `/model`) instead of raw markup.
Once a command fallback is available, title lookup stops after 32 candidate user
records rather than parsing a command-only transcript to the end, and it never
reads past a transcript's first 4 MiB. Task
notifications, `!`-mode input/output and prompt hooks are agent metadata too.
A leading wrapper followed by typed text is split in the transcript: the wrapper
is shown as metadata, and only the typed text is shown as the prompt. Unknown
XML-like tags, such as `<example>`, remain part of the typed prompt.

Session lists stay fast on large transcripts. A list row reads only the
opening of a transcript (title and start time) and its last 256 KiB (last
activity and recent model), so a model used only in the middle of a long
session is not named in the list; the transcript itself shows every entry's
model. What was read is kept in the state database (`session_info_cache`),
keyed by the file's path, size and modification time, so a restarted or
upgraded server rereads only transcripts that changed. The server also reads
every visible repository's sessions in the background when it starts; no
request waits for it.

A container page shows its live sessions as soon as they are known (they come
from the state service's snapshot) and appends the sessions it ran before when
that separate request answers. A repository lists only the sessions started in
its own containers' checkout (each container's repository directory, or below
it): repositories that share one agent config home by mistake do not list each
other's sessions. Claude names a session's project directory after its working
directory with every character but letters and digits turned into `-`, so
`/home/dev/app-api` cannot be told from a directory `api` below
`/home/dev/app`; a repository `app` then lists `app-api`'s sessions too.

Rows on the front page are styled like the terminal dashboard's: a container
booting, in autostart, running another background job or in a failed job gets a
coloured edge and a phase label, and a Stopped container is dimmed. The e-ink
theme has no colour, so phases differ by edge style (dashed, dotted, double,
heavy) and the label, and a Stopped row is drawn without top, right and bottom edges instead of a fade.

Children in the same repository appear indented under their orchestrator on
the front page and in repository container lists. Child counts and container
pages include children across all visible repositories; excluded repositories
never contribute to counts. Each child shows its state and runner when known.
A `Child of` label links to the orchestrator even across repositories;
`orchestrator not listed` means it is absent from the entire visible snapshot,
not just the current repository. An orchestrator's repository and short name
come from its snapshot row, with prefix matching only when that row is absent.
An orchestrator in an excluded repository is labelled `hidden orchestrator`,
without its name or a link.

Container pages link to their children or back to their orchestrator, resetting
the page turn and scroll position when following a related-container link.
These relationships update with the live snapshot. Indentation and the
`Child of` label distinguish nesting without colour; phase edge styles remain
reserved for booting, autostart, background jobs and failures, also on nested
rows in the e-ink theme.

## Install

The web dashboard comes with every JailBee install: its dependencies are part
of the package, and released wheels carry the built browser app. The old
`jailbee[web]` extra still installs, and adds nothing.

From a source checkout the browser app must be built (needs Node.js 22.22+ or
24.15+, and npm) before the package is installed. `make install` builds it
when npm is available, and `make install-web` fails without npm instead:

```bash
make install-web                    # builds frontend/, then installs
make web && uv sync                 # editable checkout: build once, in place
```

Without the built app, `jb remote web serve` exits with a message saying so.

## Testing from a checkout

Run `make web-test` for the browser app's Vitest suite and `make web-e2e` to
build the app and exercise the real server in the system Chrome. The latter
installs the separate `e2e` dependency group; it needs Node.js, npm and Chrome,
but no Incus or live agent. Browser tests stay outside the ordinary pytest
suite. Failed browser tests retain Playwright traces and screenshots under
`_build/e2e-results/`.

The **Web dashboard** GitHub Actions workflow runs both targets for pull
requests and pushes to `main` or `dev` that change the frontend, web server,
transcript reader, browser tests or their shared fixtures, dependency
manifests, Makefile or workflow. It can also be started with
`workflow_dispatch`. It uses the runner's Chrome and uploads browser failure
evidence as the `web-e2e-failure` artifact. Unrelated changes do not trigger
this workflow; the ordinary CI gate stays separate.

### Writing browser tests

Each test's `server` has a fresh database and rate limits. `logged_in` performs
a real PIN login against that test's server (including a fixture override),
so logout and revocation cannot invalidate another test's token.

The security-key tests use Chrome's virtual WebAuthn authenticator, with no
physical key needed. `make_server(setup=True)` serves the real registration-only
`create_setup_app(SetupDeps)`: the dashboard itself has no setup API. Stopping
that server and calling `start()` serves `create_app(WebDeps)` at the same
origin over the same database. Tests register at `http://localhost` (a secure
WebAuthn context with a domain RP ID), log in with the key, and check that a
removed key is visibly refused. Console, page and CSP checks stay enabled.

The automatic `browser_errors` collector watches all pages and popups in
function-scoped contexts created through the synchronous Playwright API:
`browser.new_context()` and `browser.new_page()`, including on a second
launched browser. Close these pages/contexts in the test or its function-scoped
fixture teardown; closing them first settles pending reports. Creation from
module/session-scoped fixtures fails loudly rather than sharing a collector
between tests. `launch_persistent_context()` is rejected before launch; use
`browser.new_context()` instead. `page.set_content()` is also rejected: it
replaces the document without running init scripts and removes CSP listeners;
use `page.goto()` to navigate. The harness supports the sync API, not async
Playwright or direct Playwright internals.

It checks at the end of the test body and again at teardown after settling
pending browser events. Declare only intentional errors, by category, for
example:

```python
browser_errors.expect("console", "intentional message")
browser_errors.expect("page", re.compile(r"Error: exact message"), times=2)
browser_errors.expect("csp", "script-src", at_least=1)
```

Strings match literal substrings; compiled regexes must match the whole
message. Each declaration consumes one distinct hit by default; `times` sets
an exact count, while `at_least` sets a minimum with no upper limit. Overlapping
patterns are matched jointly, not greedily in declaration or arrival order.
Extra hits and unmet expectations fail the test.

The restart test in `tests_e2e/test_front_page_live.py` deliberately waits for a
refused WebSocket reconnect. It
allows only `WebSocket connection to 'ws://127.0.0.1:<server-port>/api/ws'
failed: Error in connection establishment: net::ERR_CONNECTION_REFUSED`,
matched as a whole message for that server's actual port. No other connection
failure shapes are excused, and ordinary tests do not suppress reconnect errors.
A failed stop is reported directly; the test starts the server again only
once stopping and observing the expected reconnect have succeeded.

`tests_e2e/test_sessions.py` exercises the hand-written fixture sessions at
both desktop and phone widths: repository/container lists, Markdown and code,
folded tools, subagent navigation, thinking settings, live appends and
restart catch-up without lost or duplicated replies. Reuse
`tests_e2e.sessions.open_session(page)` from a session list and
`append_entry(server, path, text)` to write a fixture entry and fire its fake
watch. Copy the fixture home to a temporary directory before appending; with
the server stopped the helper only writes, leaving reconnect to catch up.

`test_front_page_live.py` covers snapshot additions, changes and removals without
navigation; phase labels and computed monochrome edges in the e-ink theme;
the connecting notice during downtime; and current state after a real server
restart. Its protocol-upgrade test forwards traffic to the real server but
rewrites the reconnect's auth version to simulate an outdated tab. The real
server's 4002 rejection must trigger one document reload and a fresh connection,
then render a subsequent state push before the reload count is checked.
`test_protocol_reload.py` keeps the client stale across that reload: a tab
reloads automatically at most once within a minute, then shows
"This page is out of date; reload" with a manual Reload button. If session
storage is unavailable, it offers manual reload immediately rather than risk
a loop.

## Run it

```bash
jb serve                            # alias of `jb remote web serve`
jb remote web serve --port 8919     # one-run overrides: --listen, --port
```

`serve` stays in the foreground and prints the URL and a one-time login PIN
to stdout. Under the systemd service that output goes to the user journal, so
anyone who can read that journal (for example members of `systemd-journal` or
`adm`) can read the current PIN; turn PIN login off with
`jb remote web auth pin off` once a TOTP key exists.
The default address is `127.0.0.1:8910`. The flags apply to that run only and
are never written to `global.yaml`.

For a permanent service use the systemd user unit:

```bash
jb remote web enable                # install, enable and start jailbee-web.service
jb remote web status                # unit state and address
jb remote web open                  # open the browser already logged in
jb remote web disable
```

The service exits and is restarted by systemd when the installed JailBee
version changes, so an upgrade needs no manual restart.

`open` creates a one-time link that works only from the same machine and
logs that browser in without a PIN. It is the easy way to use the dashboard
locally without turning authentication off.

## Logging in

Every request needs a login unless authentication is disabled (below). Methods
can be combined:

| Method | Set up with | Notes |
|---|---|---|
| PIN | on by default | A new one-time PIN is printed by `serve` (to the journal under systemd). `jb remote web auth pin off` refuses PIN logins; at least one TOTP key must exist first. |
| TOTP | `jb remote web auth totp add [--name NAME]` | Prints the secret and a QR code for an authenticator app. `totp ls` and `totp rm` manage keys. |
| FIDO2 security key or passkey | `jb remote web auth fido2 setup` | See below. `fido2 ls` and `fido2 rm` manage keys. |
| One-time link | `jb remote web open` | Localhost only, single use. |

A successful login gives the browser a session that slides for 30 days.
PIN and TOTP attempts are limited to five a minute, and after five failures
in a row each further failure doubles the wait before the next attempt:
2 seconds after the fifth, 4 after the sixth, up to 30 minutes. A successful login by any method (PIN, TOTP, security key or link) resets
every method's wait. The waits are global, not per source, so an attacker who
reaches the login can keep both the PIN and TOTP at the 30-minute cap with a
few requests an hour, and the owner's own PIN or TOTP attempts are refused
until that lifts. The ways in that the waits do not touch are a security key,
`jb remote web open` (a localhost link, for example over SSH) and restarting
`serve`, which clears the waits; any successful login lifts the PIN and TOTP
waits at once. Links and security keys are not limited. An attempt during the
wait is refused unchecked with HTTP 429 and a `Retry-After` header; the login
page shows that remaining wait in seconds below a minute, otherwise in minutes
rounded up. It buys no guess, and an attempt
still being checked holds back a parallel one. An
owner who mistypes up to four times waits not at all, a fifth or sixth time a
few seconds. The waits are kept in memory, so a restart of `serve` clears
them; under systemd that happens only on an upgrade or a crash, nothing a
remote client can trigger.

A successful PIN login resets the count, which gives a patient attacker a
fresh run: an owner who logs in by PIN daily gives them about 30 % more
guesses a month than the table below.

Each wrong guess is spent: a wrong PIN is replaced by a new one, and a TOTP
code changes every 30 seconds, so every guess has the same small chance. At
the 30-minute cap an attacker who can reach the login gets about 1,450
guesses a month per method and can expect:

| Method on | One guess hits | Even odds after | Within 30 days |
|---|---|---|---|
| PIN (6 digits) | 1 in 1,000,000 | about 40 years | about 0.15 % |
| TOTP, one key | 3 in 1,000,000 (codes ±30 s accepted) | about 13 years | about 0.44 % |
| PIN and TOTP together | both waits run side by side | about 10 years | about 0.58 % |

Without the backoff, five guesses a minute gave even odds in about 32 days
for TOTP and 96 days for the PIN.

Each extra TOTP key adds another three codes to hit. The PIN is printed anew
after every failed attempt, and failed logins show in `jb remote web auth log`
and the journal, so a guessing run is visible long before it is likely to
succeed, but only if someone looks. For a dashboard reachable from the
internet, log in with FIDO2 security keys and turn the PIN off, or better,
do not expose the server at all and reach it over an SSH tunnel or a VPN.

```bash
jb remote web auth status           # methods on, devices logged in
jb remote web auth sessions ls      # logged-in devices
jb remote web auth sessions revoke ID
jb remote web auth log [-n 50]      # logins, failures, backoffs, logouts, revocations
```

Revoking a session refuses the device's next request and closes its open live
view.

Each lengthening of a PIN or TOTP wait is logged once as a `backoff` entry;
refused attempts during a wait go to the journal only, so a flood of them
cannot churn the audit log. The audit log keeps the newest 1000 entries.
Failed one-time-link and FIDO2
logins, which no attempt limit throttles, are kept apart (the newest 100), so
a flood of them cannot push earlier PIN or TOTP failures out of the log.

Logins, TOTP secrets and sessions live in the JailBee state database
(`~/.local/state/jailbee/state.sqlite`). Its directory is kept `0700` and the
database `0600` (each JailBee process tightens them when it first opens
the database), so other local users cannot read
the TOTP secrets.

### FIDO2 setup mode

A security key works only for the domain it was registered at, and the domain
is taken from the browser's `Origin` header. Registration therefore has to
happen through the same HTTPS address you will log in at. `fido2 setup` runs a
temporary server that serves a registration page instead of the dashboard
(stop the dashboard first if it uses that port; `--listen` and `--port`
apply). Register the key in the browser, then press Ctrl-C and start the
dashboard again. The setup PIN keeps only the five-a-minute limit, without
the backoff: setup mode runs only while you register a key, so do not leave it
running.

### Authentication disabled

```yaml
# ~/.config/jailbee/global.yaml
remote:
  web:
    auth:
      enabled: false
```

With `auth.enabled: false` the server serves everything without a login. It
refuses to bind a non-loopback address unless
`remote.web.auth.allow_unauthenticated_remote: true` is also set, because then
anyone who can reach the port sees the dashboard. Without that opt-in the
server also answers only requests whose `Host` header is `localhost`, a
loopback address or the `listen` address, so a web page cannot reach the
dashboard by pointing its own domain at `127.0.0.1` (DNS rebinding).
Authentication is never
skipped based on where a request comes from; see
[Web dashboard authentication](security.md#web-dashboard-authentication).

## Configuration

`remote.web` lives in the host-global `global.yaml`; the full reference is
[`remote.web`](config.md#remoteweb).

```yaml
remote:
  web:
    listen: 127.0.0.1               # IP address to bind
    port: 8910
    auth:
      enabled: true
      allow_unauthenticated_remote: false
    excluded_repos: []              # repos the web never shows, by container_prefix
```

Everything else under `remote.web` is the shared
[remote policy](security.md#remote-policy). The web starts with no commands
allowed, so the current dashboard only reads.

## Themes and display profile

Open the settings page (the Settings link on the front page) to choose
a theme (light, dark, e-ink), body and code font sizes, and whether thinking
blocks are shown. These choices are stored in the browser on that device only.

The display profile **E-ink** is for e-paper readers and slow-refresh
screens: no animation, content moves in page turns instead of scrolling, live
transcript entries are held back until you turn the page rather than moving
the text you are reading, and the front page refreshes slowly.

Session transcripts keep their title, agent/model context and connection notice
in a compact header while you read. **Latest answer** appears only when the
newest assistant reply is outside the readable area (below the header and above
the bottom controls, including the device's safe-area inset). A truncated title
keeps its full text as a tooltip and accessible heading name. Page turns use the
actual readable height even on short landscape screens; a partial keyboard or
system scroll stays on its current page so Next never skips unread text.
Jumping aligns the newest reply's start just below the measured header, even
for a short final reply, with trailing room to scroll it there. Jumping or
scrolling to the reply hides the button; moving
away or receiving a newer reply out of view shows it again. In the e-ink display
profile the same rule applies: the button starts a page at the reply, with
subsequent page turns continuing from there, and live entries remain behind
the new-messages notice until tapped.

## Install as an app

The dashboard serves a web app manifest, so a browser can install it
("Install" or "Add to Home screen") and open it in its own window on a phone
or desktop. Installing needs the page to be served over HTTPS or from
`localhost`.

## Behind a reverse proxy

The server is meant to sit at the **root of a domain** (`https://jb.example.com/`),
not under a path prefix. Put TLS and the public name in the proxy and keep
`remote.web.listen` on `127.0.0.1`. The proxy must

- upgrade WebSocket connections for `/api/ws` (the live view depends on it),
- pass the original `Host` and `Origin` headers (FIDO2 derives its domain from
  them, and a WebSocket whose `Origin` does not match `Host` is refused),
- keep the connection open long enough for a live view.

```nginx
server {
    listen 443 ssl;
    server_name jb.example.com;
    # ssl_certificate / ssl_certificate_key ...

    location / {
        proxy_pass http://127.0.0.1:8910;
        proxy_set_header Host $host;
        proxy_set_header Origin $http_origin;
    }

    location /api/ws {
        proxy_pass http://127.0.0.1:8910;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header Origin $http_origin;
        proxy_read_timeout 1h;
    }
}
```

Every response carries a `Content-Security-Policy` (`default-src 'self'`,
`frame-ancestors 'none'`, no inline scripts or styles), `X-Frame-Options: DENY`,
`X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. A proxy
that adds its own security headers should not loosen these; the dashboard
cannot be embedded in another page.

Keep authentication on behind a proxy. Every request the server sees comes
from the proxy, which is why [source addresses are never trusted](security.md#web-dashboard-authentication).
An SSH tunnel (`ssh -L 8910:127.0.0.1:8910 host`) works the same way.
