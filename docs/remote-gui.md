# Remote GUI over SSH

## What it is

With the [SSH service](installation.md#optional-ssh-service) a remote session
cannot open a window on the host's screen, so by default the GUI launchers
(`jb ide`, `jb chrome`, `jb firefox`, `jb browser`, `jb apps run`) are
host-only. With `remote.ssh.gui` on, they instead draw on a **shared RDP
display**: a small `jailbee-display` container running the weston compositor
(RDP backend, software rendering). You tunnel its port over the existing SSH
connection and view it in any RDP client.

There is one screen for every container. Apps from different containers appear
side by side on it.

## Turn it on

1. Set `remote.ssh.gui: true` in `global.yaml` (see
   [`remote.ssh`](config.md#remotessh)).
2. `jb display up`. The first run pulls an image and installs weston, which
   takes a few minutes; later runs are instant.

The flag is re-read for every new session and every forward request, so no
`jb remote ssh restart` is needed to turn it on or off. Containers mount the
display directory at their next start (or on the first GUI launch) only while
it is on; `jb display up` warns if it is still off.

`jb display up` prints the connection recipe; `jb display status` prints it
again.

## Connect

On your own computer, in two steps:

```bash
ssh -N -L 3389:127.0.0.1:13389 -p <ssh port> jailbee@<host>
```

then point an RDP client at `localhost:3389`. Any client works: `mstsc` on
Windows, Windows App (formerly Microsoft Remote Desktop) on macOS, `xfreerdp` or
Remmina on Linux.
If the client asks for a login, any user name and password will do: the
display speaks TLS without Network Level Authentication and checks no
credentials (see [Security](security.md#remote-gui)). Accept the self-signed
certificate the client warns about.

From a Mac, [Using JailBee from a Mac](macos.md) has a ready `~/.ssh/config`
entry and the Windows App steps.

Already in an SSH session? Add the forward to it with `~C`, then
`-L 3389:127.0.0.1:13389`. To have every session carry it, put
`LocalForward 3389 127.0.0.1:13389` in the host's entry in `~/.ssh/config`.

The server accepts the forward from any authorized key while `remote.ssh.gui`
is on, and only to `127.0.0.1:13389`. You can connect before or after
launching anything.

## Launch

Run `jb chrome`, `jb ide` or `jb apps run` from an SSH session, or choose a
launch in the remote dashboard. To launch an arbitrary command, use
`jb exec <name> -d --gui -- <cmd>`; a plain `jb exec -d` never touches the
shared display, so detached builds and servers are unaffected. If no RDP client is connected yet, the command
prints the recipe and waits up to 120 seconds for one. Once a client appears it
waits a few seconds more for the client's input seat to settle, then launches.
If nobody connects in time it fails with an error telling you to connect first;
run it again after connecting.

Autostart apps started from a GUI-enabled SSH session also go to the shared
display, so they can wait for the RDP client in the same way. If the display
cannot be prepared, the first failure is reported and the remaining autostart
apps are skipped, so `jb new` waits at most once.

## Native windows with waypipe

On a Linux laptop with a Wayland session and
[waypipe](https://gitlab.freedesktop.org/mstoeckl/waypipe) installed, a
`waypipe ssh` session carries the apps' windows to your own desktop as native
windows. They close with the session. A Mac can do the same with the
Cocoa-Way compositor; see
[Using JailBee from a Mac](macos.md#4-on-the-mac-native-windows-optional).
Choose RDP instead on Windows, and for windows that must survive a disconnect.

Needs `remote.ssh.gui: true` (a waypipe session is refused without it) and a
stock waypipe client; the SSH server needs nothing else. Open the dashboard
and launch from its menu:

```bash
waypipe ssh -t -p <ssh port> jailbee@<host> dashboard
```

The `-t` is needed for the dashboard. A launch from the dashboard that fails
shows only the exit-code notice, not the launcher's message. To run one app
directly:

```bash
waypipe ssh -p <ssh port> jailbee@<host> --repo <prefix> chrome <container>
```

The direct command waits until the app exits, and the session ends with it.
`--repo <prefix>` is required for it.

- The direct form is for the GUI launchers (`chrome`, `firefox`, `browser`,
  `ide`, `apps run`). `exec --gui` detaches, so its window closes as soon as
  the session ends; start it from the dashboard instead.
- If Chrome already runs in that container, a new attached `chrome` launch
  returns at once (Chrome hands the request to the running instance), which
  ends the session and closes the window.
- OpenSSH connection sharing breaks waypipe: only the first `waypipe ssh` over
  a `ControlMaster`/`ControlPersist` connection works. Add
  `-o ControlMaster=no` (or `-o ControlPath=none`) to the command.

- Window titles carry a `[<container>] ` prefix, so windows from different
  containers can be told apart.
- Each session runs one waypipe server per container, as a transient unit in
  `jailbee-display`; stopping the session stops them, and with them every
  window.
- The client's `--compress` is honoured (`waypipe --compress zstd ssh ...`).
  The other waypipe options (`--threads`, `--video`, `--no-gpu`) are the
  laptop's own; the server does not use them.
- Not supported: `--oneshot`, `--xwls` and `--remote-bin`. The error names the
  supported form.
- The first session after upgrading re-provisions `jailbee-display` (it
  installs waypipe), which takes about a minute.

The command parser accepts what waypipe 0.11 sends; 0.8 and 0.9 clients are
untested.

## An app already open on another display

Chrome and Firefox run once per profile: a second launch hands its URL to
the running browser, which can only draw where it started. When `jb chrome`
(or `jb firefox`, `jb browser`, `jb apps run`) finds the browser open on a
different display — the host, the shared RDP display, a waypipe session — it
closes it there and starts it again here, with the same profile. On a
terminal it asks first; without one (a `waypipe ssh` command, a dashboard
action) it moves. `--move` and `--no-move` skip the question.

Chrome reopens its tabs. Firefox does so only with *Open previous windows
and tabs* (`browser.startup.page = 3`) set in its settings. A browser that
does not close within 15 seconds is left alone and the launch fails; close
it on the other display and run the command again.

## What it does not do

- No RDP authentication. Any login is accepted; access is limited by network
  reachability and the SSH tunnel (see [Security](security.md#remote-gui)).
- On the RDP display, window titles do not name the container; two Chrome
  windows from two containers look alike. (Waypipe sessions prefix them.)
- No GPU. Rendering is in software.
- One screen for all containers, and one shared clipboard.
- `jb gui` (the Qt dashboard) stays a host command.
- When SSH repository exclusions are active (`remote.ssh.excluded_repos` is
  non-empty), the GUI launchers (`ide`, the browsers, `apps run`) are refused
  over SSH even with `gui` on, because the scoped command allow-list does not
  include them. `jb exec -d --gui` is in that list and still works.

## Security

See [Remote GUI in the security model](security.md#remote-gui).

## Troubleshooting

- `jb display status` shows whether the container and its service are running.
- `jb display up --recreate` rebuilds the display container from scratch.
- `jb display down` stops it; the tunnel still opens, but the RDP client then
  finds nothing listening.
- A refused forward (`administratively prohibited` in `ssh -v`) means
  `remote.ssh.gui` was off when it was requested, or the forward named a
  destination other than `127.0.0.1:13389` (`localhost` does not count).
- "No seats available" from an app means no RDP client was connected when it
  started. Connect a client, then launch again.
- A container that was already running when the feature was enabled gets the
  display directory mounted at its next start, or on the first GUI launch.
