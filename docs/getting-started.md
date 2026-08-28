# Getting started

This assumes `jailbee` is installed and `jailbee setup` has been run — see
[Installation](installation.md), whose last step is that command (shell
completions, the egress-refresh timer, the Claude skills).

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
- **One shared state layer per repo.** Package-manager caches, the JetBrains
  config, `~/.ssh` and Claude's login live in a shared dir outside the
  containers. Most of it is bind-mounted into every container at once, so
  branches running *in parallel* share one warm pnpm cache and one set of
  tool settings — configure something in one container and the others have
  it. A few caches that a tool locks — Gradle, Maven, and the Chrome profile
  — instead give each container its **own** private copy, seeded from the
  warmest existing one, so concurrent builds don't contend on one lock file.
  Either way the state survives `jailbee destroy` / `jailbee new` cycles and
  is a layer of its own, so nothing a container does leaks into your host's
  own dotfiles. See [`shared_caches`](config.md#shared_caches) and
  [`pooled_caches`](config.md#pooled_caches).

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

**Per-developer settings:** `.jailbee/config.yaml` is checked into the repo
and shared with your team, so anything personal lives in
`~/.config/jailbee/global.yaml` instead:

```bash
jailbee config init --global    # writes a fully-commented ~/.config/jailbee/global.yaml
```

That file carries your host mounts (`~/.gitconfig`, `~/.gnupg`), your IDE
preference, your GitHub token, the Docker registry mirror port (default
3128, rpardini's default), and the **Claude Code** opt-in — see
[Claude Code in the container](#claude-code-in-the-container) below. The
two files are deep-merged at load time: the repo config wins on scalar
collisions, lists are appended, and a few blocks (like
`github.api_tokens`) are permitted *only* in the global file so tokens
can't leak via git.

## Initialize and build

```bash
jailbee config validate         # verify your config is sane
jailbee init                    # creates Incus profiles, ACL, jailbee-loose bridge, shared dirs
jailbee registry up             # Docker users only — see below
jailbee base build              # builds the golden image (~10-15 min, one time)
```

`jailbee registry up` is only for repos whose image contains Docker
(`golden.stacks.docker`). Run it before your first strict-mode `jailbee new`:
the strict egress allowlist has no registry hosts, so the mirror is the
container's only route to Docker Hub — and a strict-mode `jailbee new` refuses
to create the container until the mirror is up (in loose mode it warns and
proceeds, since there the mirror is only a pull cache). Repos without Docker
skip this step — `docker_registry_mirror.enabled` defaults to `auto` and never
asks for the mirror container to exist.

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

## Claude Code in the container

Running a coding agent unattended is what the per-branch container and the
strict egress allowlist are *for*. The practical payoff is that you can
stop answering permission prompts: run Claude with
`--dangerously-skip-permissions` and let the container be the boundary
instead of the agent's own judgement. You size the blast radius once, in
the container's config — read-only mounts for what the build needs,
sensitive `optional_mounts` left detached, `strict` egress — rather than
adjudicating it one prompt at a time. JailBee has first-class support for
[Claude Code](https://claude.com/claude-code) — one of six shipped agent
presets, and the only one exercised in production. The same `agents:`
mechanism wires in any other terminal coding agent the same way; see
[Generic agent support](agents.md) for the full list and how to add your
own. Turn Claude Code on in `~/.config/jailbee/global.yaml`:

```yaml
agents:
  claude:
    enabled: true
    plugins_enabled: true
    # autostart: true                                # launch claude on every container start
    # command: claude --dangerously-skip-permissions # what the autostart window runs
```

The template written by `jailbee config init --global` already contains
this `agents.claude` block with `enabled: true`, so if you took that path
you have it already. Run `jailbee apply` after editing, then `jailbee new`
a container.

A top-level `claude:` block is still accepted and means the same thing, but
defining both spellings at once is a `ConfigError` — so if you have an older
`claude:` block, rename it rather than adding `agents.claude` alongside it.

What turning it on gets you:

- **Claude Code installed in every container**, from a version store shared
  across the repo's containers — no per-container download.
- **One login, all containers.** `<shared_dir>/claude` is mounted as
  `~/.claude`, so settings, MCP servers, agents and credentials are shared
  and survive `jailbee destroy` / `jailbee new` cycles. Claude Code's global
  config (`.claude.json`) lives **inside** that mount: the golden image
  exports `CLAUDE_CONFIG_DIR=$HOME/.claude`, and Claude Code reads
  `(CLAUDE_CONFIG_DIR || $HOME)/.claude.json`. Your *host* `~/.claude` is
  never read — Claude runs its onboarding once, inside the first container.
- **Strict mode still works.** The Anthropic API and CLI-update hosts are
  added to the egress allowlist automatically; `plugins_enabled` adds the
  GitHub + npm hosts the plugin marketplace and skills reach. You don't
  list them by hand.
- **Claude knows JailBee.** JailBee's own `jailbee-usage` and
  `jailbee-repo-setup` skills are copied into the shared skills directory,
  so the in-container Claude can drive `jailbee` commands for you.
- **AI-written PRs.** `jailbee pr <name>` asks the in-container Claude for
  the title, body, and branch name (`--no-ai` opts out per call). It runs on
  Sonnet by default (`claude.ai_pr_model`), and follows your project's own
  PR-writing rules if you state them in `claude.pr_prompt`.

With `autostart: true`, every container comes up with Claude already
running in a tmux window; `jailbee tmux <name>` drops you straight into it.
That plus `jailbee net strict <name>` is the intended shape for unattended
runs. Before leaning on it, read
[Running an agent without prompts](security.md#running-an-agent-without-prompts)
— it spells out exactly what an agent in that mode can and cannot reach,
including the parts of the shared state layer it *can*. See
[`config.md`](config.md#claude) for every `claude.*` key.

## Next steps

- [Commands](commands.md) — full command + flag reference
- [Git bridge and branch workflows](git-bridge.md) — moving commits between host and containers, stacked PRs, mount vs clone
- [Setting up JailBee in your own project](project-config.md) — adapting `jailbee` to your own repo and stack
- [Configuration reference](config.md) — every `.jailbee/config.yaml` key
