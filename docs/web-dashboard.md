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

## Install

```bash
uv tool install 'jailbee[web]'      # or: pipx install 'jailbee[web]'
```

Released wheels carry the built browser app. From a source checkout the app
must be built once (needs Node.js 22.22+ or 24.15+, and npm), then the package
installed with the extra:

```bash
make web                            # builds frontend/ into src/jailbee/web/static/
uv sync --extra web                 # or: uv tool install -e '.[web]'
```

Without the extra, `jb remote web serve` exits with a message naming it.

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
PIN and TOTP attempts are limited to five a minute, each method on its own,
so guessing one never locks out another; one-time links and security keys are
not limited.

```bash
jb remote web auth status           # methods on, devices logged in
jb remote web auth sessions ls      # logged-in devices
jb remote web auth sessions revoke ID
jb remote web auth log [-n 50]      # logins, failures, logouts, revocations
```

Revoking a session refuses the device's next request and closes its open live
view.

### FIDO2 setup mode

A security key works only for the domain it was registered at, and the domain
is taken from the browser's `Origin` header. Registration therefore has to
happen through the same HTTPS address you will log in at. `fido2 setup` runs a
temporary server that serves a registration page instead of the dashboard
(stop the dashboard first if it uses that port; `--listen` and `--port`
apply). Register the key in the browser, then press Ctrl-C and start the
dashboard again.

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

Keep authentication on behind a proxy. Every request the server sees comes
from the proxy, which is why [source addresses are never trusted](security.md#web-dashboard-authentication).
An SSH tunnel (`ssh -L 8910:127.0.0.1:8910 host`) works the same way.
