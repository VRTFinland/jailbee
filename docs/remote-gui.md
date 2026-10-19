# Remote GUI over SSH

## What it is

With the [SSH service](installation.md#optional-ssh-service) a remote session
cannot open a window on the host's screen, so by default the GUI launchers
(`jb ide`, `jb chrome`, `jb firefox`, `jb browser`, `jb apps run`) are
host-only. With `remote.ssh.rdp` on, they instead draw on a **shared RDP
display**: a small `jailbee-display` container running the weston compositor
(RDP backend, software rendering). You tunnel its port over the existing SSH
connection and view it in a compatible RDP client.

There is one shared screen for all containers, not a separate screen per
container. Apps from different containers appear side by side on it.

This page covers two ways to see the windows. The RDP display needs
`remote.ssh.rdp: true` and is off by default. Native windows over `waypipe ssh`
need no configuration: `remote.ssh.waypipe` is on by default (see
[below](#native-windows-with-waypipe)).

## Turn it on

On the Linux host, with the [SSH service](installation.md#optional-ssh-service)
already installed and enabled:

1. Set `remote.ssh.rdp: true` in `~/.config/jailbee/global.yaml` (see
   [`remote.ssh`](config.md#remotessh)).
2. Run `jb display up`. The first run downloads an image and installs the
   display packages, which can take a few minutes. Later runs reuse the
   container, starting it or completing provisioning if needed.

The flag is re-read for new SSH sessions and forwarded connections, so no
`jb remote ssh restart` is needed for new requests to see a change. Existing
sessions retain their GUI setting. Disabling the flag does not disconnect an
existing RDP connection, close apps, or remove an already attached display
mount. With RDP enabled, containers acquire the display mount at
their next start or first GUI launch; `jb display up` warns if `rdp` is off.

`jb display up` prints the connection recipe; `jb display status` prints it
when the display container and its service are running.

## Connect

On your own computer, open a tunnel to JailBee's SSH service (replace the
placeholders):

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:3389:127.0.0.1:13389 -p <ssh-port> jailbee@<host>
```

Keep this command running, then point an RDP client at `127.0.0.1:3389`. If
local port 3389 is already in use, change the first port and use that port in
the client. The explicit loopback bind keeps the local tunnel off your LAN.
`ExitOnForwardFailure` catches failure to open the local listener; it does not
prove the RDP service at the far end is ready.

Use an RDP client that supports TLS without Network Level Authentication
(NLA), such as `mstsc` on Windows, Windows App (formerly Microsoft Remote
Desktop) on macOS, or `xfreerdp` / Remmina on Linux. Client settings or policy
that require NLA can prevent a connection. The display checks no credentials;
if the client asks for a login, use dummy values, not a real password (see
[Security](security.md#remote-gui)). It presents a self-signed certificate;
accept it only for the display you intended to reach through the SSH tunnel.

The SSH service listens on `127.0.0.1:8022` by default, so a remote machine
cannot reach it directly at `<host>:8022`. Use the host's own `sshd` as a jump
host rather than exposing a new listener just for this tunnel.
[Using JailBee from a Mac](macos.md#2-on-the-mac-one-ssh-config-entry) has a ready
`~/.ssh/config` entry using `ProxyJump`; that SSH configuration also works on
Linux and Windows with OpenSSH. The Mac page includes the Windows App steps.

In an interactive OpenSSH session with a pseudo-terminal and
`EnableEscapeCommandline yes` enabled on the client, type `~C` at the start of
a line, then `-L 127.0.0.1:3389:127.0.0.1:13389`. The escape command line is
disabled by default in current OpenSSH; otherwise open the separate tunnel
above. To request the forward whenever you connect through a particular
`~/.ssh/config` host entry, add
`LocalForward 127.0.0.1:3389 127.0.0.1:13389` there. A second independent
connection cannot bind the same local port.

The server accepts this TCP forward from any authenticated SSH key while
`remote.ssh.rdp` is on, and only to `127.0.0.1:13389`. Command permissions and
repository exclusions do not restrict access to the shared RDP screen. You
can connect before or after launching anything.

## Launch

Run `jb chrome`, `jb ide` or `jb apps run` from an SSH session, or choose a
launch in the remote dashboard. To launch an arbitrary command, use
`jb exec <name> -d --gui -- <cmd>`; a plain `jb exec -d` never touches the
shared display, so detached builds and servers are unaffected. After preparing
the display, the launcher waits about two minutes for an RDP client, printing
the recipe if none is connected yet. It checks for an established TCP
connection and checks again after three seconds before launching. This is a
readiness heuristic, not verification that the RDP handshake or input seat is
ready. Display startup and polling can make the total wait longer. If the wait
fails, connect a client and run the launch again.

Autostart apps launched from an SSH session with `rdp` on target the shared
display too, but never move an already-running browser: it may receive the
request on its existing display. Use an explicit browser launch to move it.
If display preparation fails, the failure is reported and the remaining apps
in that autostart run are skipped. Foreground commands may wait for this;
background commands return first and report the result in the worker log.
GUI autostart does not guarantee that all deferred autostart stages have
finished.

## Native windows with waypipe

On a Linux laptop with a Wayland session and
[waypipe](https://gitlab.freedesktop.org/mstoeckl/waypipe) installed, a
`waypipe ssh` session carries the apps' windows to your own desktop as native
windows. They close with the session. A Mac can do the same with the
Cocoa-Way compositor; see
[Using JailBee from a Mac](macos.md#4-on-the-mac-native-windows-optional).
Choose RDP instead on Windows, and for windows that must survive a disconnect.

On by default (`remote.ssh.waypipe`; a waypipe session is refused if you turn it
off), so it needs no configuration beyond a stock waypipe client. JailBee runs the server-side waypipe in `jailbee-display`,
not as a host-installed binary. Open the dashboard and launch from its menu:

```bash
waypipe ssh -o ControlPath=none -t -p <ssh-port> jailbee@<host> dashboard
```

The `-t` is needed for the dashboard. A launch from the dashboard that fails
shows only the exit-code notice, not the launcher's message. To run one app
directly:

```bash
waypipe ssh -o ControlPath=none -p <ssh-port> \
  jailbee@<host> chrome <container> --repo <prefix>
```

The direct command waits until the app exits, and the session ends with it.
`--repo <prefix>` selects a registered repo explicitly. Without it, a single
visible repo is used automatically; several need `-t` for a picker (otherwise
exit 2 lists them).

- The direct form is for the GUI launchers (`chrome`, `firefox`, `browser`,
  `ide`, `apps run`). `exec --gui` detaches, so its window closes as soon as
  the session ends; start it from the dashboard instead.
- If Chrome hands the request to an existing instance rather than starting a
  new one, the attached command returns at once and its new waypipe session
  ends. An instance on another display normally triggers the move policy
  below; `--no-move` leaves it on that other display.
- Use a separate SSH connection for each waypipe session. The server associates
  waypipe state with the SSH connection, so later sessions sharing a
  `ControlMaster`/`ControlPersist` connection can fail. The examples disable
  sharing with `-o ControlPath=none`; `ControlMaster=no` alone can still reuse
  an existing control socket.

- Window titles carry the full Incus name, for example `[myproject-feat-x] `,
  so windows from different repositories and containers can be told apart.
- Each session runs one waypipe server per container, as a transient unit in
  `jailbee-display`; stopping the session stops them, and with them every
  window. Each container reaches only its own servers (see
  [Security](security.md#remote-gui)).
- The client's `--compress` is honoured (`waypipe --compress zstd ssh ...`).
  The other waypipe options (`--threads`, `--video`, `--no-gpu`) are the
  laptop's own; the server does not use them.
- Not supported: `--oneshot`, `--xwls` and `--remote-bin`. The error names the
  supported form.
- An allowed waypipe session automatically prepares `jailbee-display`, including
  installing waypipe if an older display lacks it. Provisioning can take a few
  minutes; a complete, running display is reused. No RDP client is needed.

The command parser accepts what waypipe 0.11 sends; 0.8 and 0.9 clients are
untested.

## An app already open on another display

Chrome and Firefox run once per profile: a second launch hands its URL to
the running browser, which can only draw where it started. When `jb chrome`
(or `jb firefox`, `jb browser`, `jb apps run`) finds the browser open on a
different display — the host, the shared RDP display, a waypipe session — it
closes it there and starts it again here, with the same profile. On an
interactive terminal it asks first; without interactive prompting (for example,
a `waypipe ssh` command without `-t` or a dashboard action) it moves by default.
`--move` requests the move without asking. `--no-move` leaves the browser on
its old display and sends the launch request there; it does not cancel the
launch. This browser-specific check does not apply to arbitrary `exec --gui`
commands, IDEs or custom apps.

When moving Chrome, JailBee adds `--restore-last-session` to request tab
restoration. Firefox relies on its own session settings, such as *Open previous
windows and tabs* (`browser.startup.page = 3`); restoration is browser behavior,
not a backup of unsaved work. JailBee sends SIGTERM and waits with a 15-second
budget for both the browser process and its profile lock to disappear. If
shutdown does not finish, it does not force-kill the browser and the new launch
fails; close it on the other display and retry.

## What it does not do

- No RDP authentication. Any login is accepted; access is limited by network
  reachability and the SSH tunnel (see [Security](security.md#remote-gui)).
- On the RDP display, window titles do not name the container; two Chrome
  windows from two containers look alike. (Waypipe sessions prefix them.)
- No GPU. Rendering is in software.
- One RDP screen for all containers, and one shared clipboard. Neither SSH
  command permissions nor repository exclusions isolate its windows.
- `jb gui` (the Qt dashboard) stays a host command.
- When SSH repository exclusions are active (`remote.ssh.excluded_repos` is
  non-empty), the GUI launchers (`ide`, the browsers, `apps run`) are refused
  over SSH even with `rdp` or `waypipe` on, because the scoped command allow-list does not
  include them. `jb exec -d --gui` is in that list and remains available for
  visible repositories if the command policy permits `exec`. This command
  restriction does not prevent forwarding to the shared RDP screen.

## Security

See [Remote GUI in the security model](security.md#remote-gui).

## Troubleshooting

- `jb display status` shows whether the container and its service are running.
- On the host, `jb display up --recreate` deletes and rebuilds the shared
  display container. Save work first: this interrupts apps using either its
  RDP compositor or its waypipe servers, across containers and sessions.
- `jb display down` stops the display container and interrupts those display
  connections too. The SSH connection and local forwarding listener may stay
  open, but RDP connections cannot reach the stopped display. A later GUI
  launch can start it again while `rdp` or `waypipe` remains enabled.
- For a refused forward (`administratively prohibited` in `ssh -v`), check
  `remote.ssh.rdp` and the destination `127.0.0.1:13389` (`localhost` does not
  count). The server also refuses forwarding if it cannot reload the GUI
  setting; check the SSH service log for configuration errors.
- "No seats available" from an app means the compositor's input seat was not
  ready. Connect an RDP client fully, then launch again; the TCP readiness
  check cannot prove that the seat exists.
- A container that was already running when the feature was enabled gets the
  display directory mounted at its next start, or on the first GUI launch.
