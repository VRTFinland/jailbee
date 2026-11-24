# Everyday use

How do I create, enter, stop, snapshot and destroy a branch container day to
day, and how do I give it the network access and ports it needs?

This page assumes you have finished [Getting started](getting-started.md): the
repo has a `.jailbee/config.yaml`, `jailbee init` has run and the golden image
is built. Every command below runs **on the host** in the repository (or with
`jailbee --repo <path>`), and every `<name>` is the container's *short name*,
the slugified branch that `jailbee ls` shows. Leave `<name>` out and JailBee
picks the repo's only container, or opens a picker on a terminal. The
destructive commands (`destroy`, `snapshot restore`, `snapshot delete`) always
show the picker, even with one candidate.

## Create a container for a branch

```bash
jailbee new feat/my-feature            # branch off the default branch
jailbee new feat/my-feature feat/base  # branch off another base branch
jailbee new --current                  # use the host's checked-out branch
jailbee new feat/my-feature -b         # background: get the shell back at once
jailbee ls                             # watch it come up
```

Incus names cannot contain `/`, so `feat/my-feature` becomes the short name
`feat-my-feature`. In the default clone mode the container gets its own clone
of the repo and the branch lives inside it. Pointing `new` at an existing
branch checks it out for review instead of creating it.

What you see: `jailbee ls` lists the container with its state, network mode,
IP and the agent and git columns. With `--background` the `JOB` column shows
the live phase (`creating`, `cloning`, `autostart`); a failed job leaves the
container in place for inspection, and `jailbee job log` and
`jailbee job clear` read and acknowledge it.

More: [Choosing the starting point](git-bridge.md#choosing-the-starting-point-for-jailbee-new),
[Background creation](git-bridge.md#background-creation),
[Clone mode or mount mode?](faq.md#clone-mode-or-mount-mode)

## Get into a container

```bash
jailbee shell feat-my-feature                 # interactive shell in the clone
jailbee tmux feat-my-feature                  # attach to the autostart tmux session
jailbee exec feat-my-feature -- git status    # run one command as the dev user
jailbee exec feat-my-feature -d --gui -- firefox   # detached GUI app on your display
jailbee ide feat-my-feature                   # JetBrains IDE on the host display
```

`shell` lands in the in-container clone (`/home/dev/<repo>`). Add
`--cwd home` to `exec` to run from the home directory instead, and `-u root`
to `shell` for a root shell. Commits you make in the container reach the host
over the git bridge; see [Getting code in and out](git-bridge.md).

More: [How do I get into a container?](faq.md#how-do-i-get-into-a-container),
[Desktop apps](faq.md#can-i-run-the-ide-and-a-browser-from-inside-the-container),
[The terminal dashboard](dashboard.md), which does all of the above from a menu.

## Stop, start and restart

```bash
jailbee stop feat-my-feature       # clean shutdown
jailbee start feat-my-feature      # boot it and run autostart again
jailbee start feat-my-feature -b   # boot in the background
jailbee restart feat-my-feature    # stop + start
```

A stopped container keeps its disk and costs no CPU or memory. `start` and
`restart` re-run the repo's `autostart` commands; skip them with
`--no-autostart`. `stop` refuses while a detached autostart stage is still
running. `--force` skips that check and pulls the plug instead of shutting
down cleanly, so unsaved work is lost and a running autostart step is cut off;
use it only when a clean stop hangs.

More: [Commands](commands.md), [Autostart](faq.md#how-do-i-run-setup-steps-or-start-services-automatically)

## Before and after a host reboot

```bash
jailbee stop --all          # list and confirm every running development container
# Reboot the host, then:
jailbee resume
```

These commands cover all repositories, including mount-mode containers and
orchestrated children. `stop --all --yes` skips the one confirmation; without
`--yes`, a non-interactive invocation exits 2. Containers with a live background
job are skipped with a warning: wait for the job, then repeat the command before
rebooting. JailBee's registry mirror, LiteLLM, display and egress proxy containers
are left to their own Incus `boot.autostart` settings.

Successful clean stops, including agent-session capture, are saved in
`${XDG_STATE_HOME:-~/.local/state}/jailbee/resume-set.json`. Repeating the stop
preserves earlier entries. `resume` starts that set and repairs running
containers whose `autostart` tmux session is missing after Incus restarted them.
Healthy running sessions are left alone. It runs the normal start setup and
`on_start` autostart (services, agent windows and session restoration), waiting
for all stages. Each container is reported independently; failures and live-job
skips on resume exit non-zero, retain retry state, and do not block other
containers. Repeat `resume` after fixing the failure. Nothing to do exits 0.
Missing containers or unavailable repository configurations are reported rather
than silently forgotten. Restricted remote sessions cannot use either
host-wide operation; ordinary `stop NAME` remains available.

## Snapshots and rollback

```bash
jailbee snapshot create feat-my-feature before-migration
jailbee snapshot ls feat-my-feature
jailbee stop feat-my-feature
jailbee snapshot restore feat-my-feature before-migration
jailbee start feat-my-feature
jailbee snapshot delete feat-my-feature before-migration
```

Take a snapshot before a risky change or a long unattended agent run. Leave
out the tag and `create` names it `snap-YYYY-MM-DD-HHMMSSZ`. `restore` and
`delete` ask for the snapshot when you leave the tag out; with both container
and tag given they run without another confirmation.

A snapshot covers the container's own root filesystem. It does **not** cover
host-mounted directories, shared caches, credentials or a mount-mode working
tree. Stop the container before restoring, and bring newer commits to the host
first: restoring discards everything since the snapshot and rebuilds the
container's egress ACL. Quiesce a database
before snapshotting if you need a consistent backup.

More: [How do I take a snapshot before something risky?](faq.md#how-do-i-take-a-snapshot-before-something-risky)

## Services inside the container

If the repo defines services (databases, dev servers, emulators), you manage
them from the host without entering the container:

```bash
jailbee service ls feat-my-feature                     # state and health
jailbee service start feat-my-feature api              # start with dependencies
jailbee service restart feat-my-feature api
jailbee service logs feat-my-feature api -n 50         # last 50 lines
jailbee service logs feat-my-feature api --follow
jailbee service stop feat-my-feature api
```

Start includes the service's dependencies and waits for readiness; stop does
not stop dependents. Definitions always come from the container's worktree,
not the host checkout. Logs are tmux scrollback, not a durable log.

Inside the container the same commands work without the container argument
(`jailbee service ls`, `jailbee service logs api`); that is also what an
in-container agent uses. `jailbee new --services …` and `autostart.on_start`
start services automatically.

More: [Services](commands.md#services),
[Placing service startup](config.md#placing-service-startup)

## Network: strict and loose

Containers start in **strict** mode: default-deny, with only the configured
allowlist (`egress_allow`, enabled integrations, your overrides) reachable.
**Loose** removes JailBee's egress restrictions.

```bash
jailbee net loose feat-my-feature --for 2h   # full network for two hours
jailbee net strict feat-my-feature           # back to the allowlist now
jailbee net status                           # timer health, repos, pools
jailbee ls --fields name,network,ttl         # who is loose, and until when
```

Loose reverts to strict by itself. The default is 15 minutes
(`loose_auto_revert.after`), enforced by the host's refresh timer. Choose the time per switch with `--for 30m`, or keep
it with `--for never` / `--no-revert`. With neither flag on a terminal JailBee
asks; without a terminal it uses the configured default. Loose mode fixes a blocked connection, but not missing credentials.

More: [strict vs loose](faq.md#what-is-the-difference-between-strict-and-loose),
[Does loose stay on?](faq.md#does-loose-stay-on-until-i-remember-to-switch-back),
[Networks](config.md#networks)

## Allow one more host in strict mode

When something is blocked, prefer widening the allowlist for just the host you
need over switching to loose:

```bash
jailbee net egress add pypi.org:443 feat-my-feature          # this container, at once
jailbee net egress add registry.example.com:443 --repo       # every container of the repo
jailbee net egress ls feat-my-feature                        # entries and where they came from
jailbee net egress rm pypi.org:443 feat-my-feature
jailbee net egress export                                    # config key with overrides folded in
```

Use `host:port`: an entry without a port allows every port. A
container override dies with the container; `add --for 30m` expires it sooner.
A `--repo` override is stored in your host-local config, not in git. Run
`jailbee apply` to push it to running containers now, or let the refresh timer
pick it up. When an entry has earned a place in the repo, paste the output of
`export` over the whole `egress_allow:` key in `.jailbee/config.yaml`.

The short alias `jailbee egress …` works too. A container override added to a
loose container is stored and takes effect when it returns to strict.

More: [How do I allow a host through in strict mode?](faq.md#how-do-i-allow-a-host-through-in-strict-mode),
[`egress_allow`](config.md#egress_allow),
[Egress overrides](security.md#egress-overrides)

## Ports in both directions

Two directions, two commands. In both, `PORT` is the **container-side** port.

```bash
# A container service on your host, e.g. a dev server on 3000:
jailbee port to-host 3000 feat-my-feature                  # host 127.0.0.1:3000
jailbee port to-host 3000 feat-my-feature --host-port auto # pick a free host port

# A host service inside the container, e.g. the adb server on 5037:
jailbee port to-container 5037 feat-my-feature

jailbee port ls feat-my-feature
jailbee port rm 3000 feat-my-feature
```

`to-host` makes the host listen and connect into the container; `to-container`
makes the container listen and connect out to the host. Both bind
`127.0.0.1` by default (`--host-address` and `--container-address` change
that; a wildcard host address exposes the service to other machines), use TCP
unless you pass `--proto udp`, and name the host side with `--host-port`.

A forward to the container is a deliberate hole through the strict-mode
boundary: it bypasses the ACL. To give every container of the repo the same
host service, declare it under `host_ports:` in the config and run
`jailbee apply`. `to-host` has no config form, because a host port is
machine-wide.

More: [Reaching a host service from a container](faq.md#how-do-i-reach-a-host-service-eg-adb-from-inside-a-container),
[Port forwards](security.md#port-forwards)

## Destroy a container

```bash
jailbee git checkout feat-my-feature   # bring the commits to the host first
jailbee destroy feat-my-feature        # asks, after checking for unsaved work
jailbee destroy --stopped              # every stopped container of this repo
```

Before the confirmation (default No) JailBee checks git status where it can
and warns about uncommitted or unpushed work. A stopped clone cannot be fully
inspected, so start it first if the work matters. `--force` skips both the
confirmation and the check, and `--all` takes every container in the repo.
`jailbee destroy -b` runs the teardown in the background.

`jailbee prune` is different: it offers containers stopped for more than 30 days,
across repos, and does **not** run the same work-loss check.

More: [How do I tear a container down without losing work?](faq.md#how-do-i-tear-a-container-down-without-losing-work)

## More

- [Getting started](getting-started.md#a-typical-day), the first-container walkthrough
- [Getting code in and out](git-bridge.md)
- [The terminal dashboard](dashboard.md)
- [Commands](commands.md), the full command and flag reference
- [FAQ](faq.md)
- [Configuration reference](config.md)
- [Troubleshooting](troubleshooting.md)
