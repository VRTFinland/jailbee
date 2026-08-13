# Getting started

This assumes `jailbee` is installed (see [Installation](installation.md)).

## Concepts

A quick mental model before the commands:

- **One container per branch.** `jailbee new <name>` spins up a full-stack
  system container dedicated to one branch — its own services, Docker
  daemon, ports, and database — so several branches run in parallel without
  colliding. Think of `<name>` as the container's name: it's what `jailbee shell`,
  `jailbee ide`, `jailbee destroy`, and the rest take. Since Incus names can't
  contain `/`, a name like `feat/my-feature` is slugified to the container
  name `feat-my-feature`. (Under the hood that same value is also the git
  branch checked out inside the container's clone, but day-to-day you can
  treat it as just the container name. Override the derived name with
  `--name`.)
- **Golden image, cloned copy-on-write.** `jailbee base build` provisions one
  image once (~10–15 min); every `jailbee new` clones it copy-on-write, so new
  containers are cheap and fast.
- **Clone mode vs mount mode.** By default the container gets its **own**
  `git clone` of the repo (clone mode), isolated from the host tree. With
  `--mount` it shares the host working tree directly instead.
- **Base branch + the git bridge.** A clone-mode container remembers the
  **base branch** it forked from and acts as a **git remote**: commits move
  between host and container over a local bridge (`jailbee git checkout` /
  `pull` / `push`) rather than through GitHub.
- **Network modes.** Each container runs `strict` (kernel egress allowlist)
  or `loose` (open egress, auto-reverts) — switch with `jailbee net`.
- **Shared state persists.** Caches, the JetBrains config, Chrome profile,
  and Claude state live in a shared dir, so they survive `jailbee destroy` /
  `jailbee new` cycles.

## Configure

`jailbee` is run from the root of the repo it manages. The repo must contain a
`.jailbee/config.yaml` file:

```bash
cd ~/path/to/your/repo
jailbee config init        # creates a fully-populated template
jailbee doctor             # sanity-check
```

All schema keys are optional — the defaults are stack-neutral and work for
any repo; add the language toolchains your project needs with
`golden.stacks`. See [`config.md`](config.md) for the full field
reference.

**Multi-repo:** Each `jailbee`-owned Incus resource (container, profile, ACL)
is prefixed with `<container_prefix>-`. The prefix defaults to your repo
directory name (`repo_root.name`) and must match `[a-z0-9][a-z0-9-]*`. If
your directory name contains underscores, dots, or capital letters, set
`container_prefix:` explicitly in `.jailbee/config.yaml`. Two repos on the
same host with distinct prefixes coexist without colliding. Use
`jailbee ls --all` to list containers from every jailbee-managed repo.

A small optional global file at `~/.config/jailbee/global.yaml` carries
host-level settings (Docker registry mirror port, data directory, image
pin, enable flag). Default port is 3128 (rpardini's default).

## Initialize and build

```bash
jailbee config validate         # verify your config is sane
jailbee init                    # creates Incus profiles, ACL, jailbee-loose bridge, shared dirs
jailbee registry up             # starts the host-level Docker registry mirror
jailbee base build              # builds the golden image (~10-15 min, one time)
```

## A typical day

A clone-mode branch from creation to teardown. The host stays the hub: you
bring the container's commits back to the host and push from there, so the
container never needs network access for day-to-day work.

```bash
# 1. Spin up a container for a new branch (off the default branch).
jailbee new feat/my-feature                  # → container "feat-my-feature" (the / is slugified to -)
jailbee ls                                   # watch it come up

# 2. Work inside it — a shell, or launch the IDE onto your desktop.
jailbee shell feat-my-feature                # lands in the in-container clone
#   … edit, run services, commit inside the container …
jailbee ide feat-my-feature                  # optional: JetBrains IDE on the host display

# 3. Bring the container's commits back to the host (local bridge, no network).
jailbee git checkout feat-my-feature         # fast-forwards / creates the host branch

# 4. Push and open a PR from the host, as usual.
git push -u origin feat/my-feature
gh pr create                             # or your normal PR flow

# 5. Iterate: more commits in the container → checkout again → push.
jailbee git checkout feat-my-feature && git push

# 6. Snapshot before something risky; roll back if needed.
jailbee snapshot create feat-my-feature before-migration
jailbee snapshot restore feat-my-feature before-migration

# 7. Tear down when the branch is merged.
jailbee destroy feat-my-feature --force
```

Prefer to open the PR *from* the container (e.g. for an AI agent running
inside it)? `jailbee pr feat-my-feature` does that — but the container needs
network, so switch it out of strict first with
`jailbee net loose feat-my-feature`. See
[Git bridge and branch workflows](git-bridge.md) for pushing host→container,
stacked PRs, and reviewing existing PRs.

## Next steps

- [Commands](commands.md) — full command + flag reference
- [Git bridge and branch workflows](git-bridge.md) — moving commits between host and containers, stacked PRs, mount vs clone
- [Setting up jailbee in your own project](project-config.md) — adapting `jailbee` to your own repo and stack
- [Configuration reference](config.md) — every `.jailbee/config.yaml` key
