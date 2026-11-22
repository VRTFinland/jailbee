# Logins and credential groups

How do I manage agent logins and share them between containers?

The short version: an agent's login lives on the **host**, in a *holder*
directory that JailBee mounts into your containers. Either every container of
one repo shares a holder, or several repos share one through a **credential
group**. Logins you are not using sit in a host-wide **parked** store. The
`jailbee account` commands move logins between those places. The
[account pool](#the-account-pool) is the set of all of them.

```text
host                                              containers
────                                              ──────────
claude-credentials/work/        ◄── group "work" ──►  repo A: all containers
                                                       repo B: all containers
~/.claude (repo C's own home)   ◄── own login ────►  repo C: all containers
parked store (not mounted)      ◄── park / use ───►  any holder
```

JailBee never talks to the agent's vendor. A login enters the pool only when
you sign in inside a container; the commands below move, store and delete
the resulting files.

> **Today only Claude Code has an account pool.** Other agents (see
> [Generic agent support](agents.md)) keep their login in the container's
> config home as that page describes. The pool commands cover every agent with
> an account-pool adapter, so they will pick up more as they are added.

## Prerequisites

- The agent is enabled for the repo (`agents.claude.enabled: true`, which the
  generated `global.yaml` already sets; see
  [Agents in containers](agents-guide.md)). With no pooled agent enabled,
  every `jailbee account` listing is empty and says so.
- A container exists and has been built with `jailbee apply`.

## Which containers share a login

One question decides it: which **credential group**, if any, does the
container resolve to? From strongest to weakest:

1. the container's own override (`jailbee account group use`, or
   `jailbee new --credential-group`),
2. the repo's `credentials.group` in its host-local file
   (`jailbee account group set` writes it; `none` means "this repo keeps its
   own login"),
3. the host default `credentials.group` in `global.yaml`.

A fresh `jailbee config init --global` sets the default group to `default`,
so on a new host every repo shares one login out of the box. A `global.yaml`
written before that key existed keeps every repo on its own login until you
choose a group.

The group name is `[a-z0-9][a-z0-9-]*` and is shared by every pooled agent,
but each agent keeps its own login in it, and one login per agent can be
live there at a time. The full rules and the `apply` flow are in
[`credentials` in the configuration reference](config.md#credentials).

## How a login gets into a container

You do not copy a credential into a container. `jailbee apply` mounts the
holder's directory into every container of the repo (for Claude it appears at
`~/.claude-creds` and the agent is pointed at it), so a login that lands in the
holder is visible to all of them at once. In practice:

1. Run the agent in any container of the holder and sign in (`/login`).
2. The credential is written into the holder directory on the host.
3. Every other container of that holder, in this repo or another one in the
   same group, is already signed in. A running session picks up a refreshed
   token because they share the same file.

The mount mechanism and what is and is not shared (history, MCP config and
sessions stay per repo) are in
[Shared credential groups](agents.md#shared-credential-groups-credentials).

## The account pool

Every command below acts on **this repo's holder**: its credential group, or
its own login when the repo has none. Run them from the repo directory, or
pick the repo through the dashboard. `-g <group>` points a command at another
group's holder instead.

### See every login: `jailbee account ls`

```console
$ jailbee account ls
```

You get one row per login file on the host, not per container:

| Column | Meaning |
|---|---|
| `AGENT` | The agent whose pool holds the login |
| `GROUP` | The credential group, or the repo that keeps its own login |
| `ACCOUNT` | The account (email, plus an organisation hint when needed) |
| `STATE` | `live` (in a holder now), `parked` (in the store) or `empty` (a group with no login) |
| `USED BY` | The repos and containers reading that holder |

`-a <agent>` narrows to one agent, `-g <group>` to one group (`-g none` for
the holders that share no group; parked logins always stay listed),
`-o json` prints JSON, and `--fields agent,account,state` picks columns.
`jailbee account group ls` shows the same rows narrowed to the groups.

An empty group, or one used only by a container's temporary override, still
gets a row, so nothing is hidden.

### Park a login: `jailbee account park`

```console
$ jailbee account park
```

This stores the login in use in the parked store and leaves the holder empty.
Afterwards `jailbee account ls` shows that account as `parked` and the holder
as `empty`. **Parking is also how you add a new account**: there is no
`account add`, because only a browser sign-in creates a credential. Park, then
run the agent in a container of that holder and sign in as the new account.
The new login lands in the empty holder. With several agents live at once,
`-a <agent>` (or the picker, on a terminal) says which one to park.

### Switch to another login: `jailbee account use`

```console
$ jailbee account use alice@example.com
```

Omit the argument to pick from a menu of the stored logins; the live one is
not offered. The reference is the account's email, or the full slot name if two
stored logins share an email (the name `ls` shows for the account). Afterwards
the chosen login is `live` and the previous one is parked, not lost.

The switch is **holder-wide**: every repo in the same credential group moves
with it. A running Claude session picks up the new credential on its next turn
without a restart. The account shown in `/status` can lag until the session
restarts.

### Delete a login: `jailbee account rm`

```console
$ jailbee account rm alice@example.com
```

It asks for confirmation (`--yes` skips it) and deletes the stored credential
permanently. It refuses the live login, so park it first. Because JailBee never
contacts the vendor, nothing is revoked on their side; getting the login back
means signing in again.

### Several agents pooled

When more than one pooled agent is enabled, a reference or an action can be
ambiguous. Pass `-a <agent>` to name the pool. On a terminal an omitted choice
opens a picker that lists the candidates across agents. Without a terminal the
command exits 2 and names the candidates and the `-a` values that would decide
between them.

## Credential groups

### Create, assign and remove

```console
$ jailbee account group create work     # an empty group, ready to use
$ jailbee account group set work        # this repo joins it, permanently
$ jailbee apply                         # mounts it into the repo's containers
$ jailbee account group unset           # back to the host-wide default
$ jailbee account group set none        # this repo keeps its own login
$ jailbee account group rm work         # only when nothing uses it
```

- `create` is optional: `set`, `use` and `account use -g` create the group on
  demand.
- `set` and `unset` write the repo's host-local file, so they never touch
  the repo's committed config. They refuse while a pooled agent is running in
  one of the repo's containers; close the agent first, or pass `--force` and
  restart it afterwards.
- If both the repo and the group already hold a login when the repo joins,
  `jailbee apply` asks which to keep and **deletes the other**. Without a
  terminal it refuses and changes nothing.
- `rm` refuses while a repo resolves to the group, while it is the host
  default, or while a container was moved into it. A login the group still
  holds is parked, not deleted.

### Move one container

```console
$ jailbee account group use personal my-branch   # only this container
$ jailbee account group reset my-branch          # follow the repo again
```

The override lives in the container and ends when the container is destroyed.
Restart the agent in that container afterwards: the command never kills a
session. Naming the repo's own group clears the override rather than writing a
redundant one. `jailbee new --credential-group <name>|none` does the same at
creation time.

## In the dashboard

The terminal dashboard has the same operations:

- `A`, or `Accounts…` in the repo menu, opens the overlay of groups and stored
  logins. Pick one to use, park or delete it; `n` creates a group.
- `Credential group…` in a repo or container menu changes the group that repo
  or that single container follows.

The dashboard runs the real `jailbee account …` commands. A refusal, such as
a running agent, appears as a notice; use `!` and add `--force` to override it.
See [the terminal dashboard](dashboard.md).

## More

- [Configuration: `credentials`](config.md#credentials), including the
  per-container override and its precedence
- [Generic agent support: shared credential groups](agents.md#shared-credential-groups-credentials)
- [Commands: `jailbee account`](commands.md)
- [Agents in containers](agents-guide.md) and [LiteLLM](litellm.md), for
  `claude-jb` and other providers' logins
- [The terminal dashboard](dashboard.md)
