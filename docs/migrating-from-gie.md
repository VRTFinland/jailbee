# Migrating from `gie`

JailBee was called `gie` (`gisgro-incus-env`) before 1.0. If you have an
installation from before the rename — a `gie` binary on your `PATH`, a
`~/.config/gie/` directory, containers carrying `user.gie.*` labels — this
page is for you. `jailbee doctor` flags this state as "pre-1.0 gie state",
lists what it found, and points at this file.

The short version, run once per machine:

```bash
uv tool install --force git+https://github.com/VRTFinland/jailbee
uv tool uninstall gisgro-incus-env
jailbee migrate
jailbee apply          # in each repo — see "After the migration" below
```

Installing from a checkout instead? `make install` works the same way. It
additionally runs `jailbee net install`, which starts the new refresh timer
against state that has not been migrated yet — harmless, because `jailbee
migrate` stops both refresh timers before it plans anything and reinstalls
the replacement as it goes. See ["A target directory that already
exists"](#a-target-directory-that-already-exists) for what happens to the
state directory that timer creates.

Then, at your leisure, in each application repo that still has `.gie/`:

```bash
git mv .gie .jailbee
```

The rest of this page explains what each step does, why the last one isn't
automated, and the compatibility surface you're relying on until you've done
all of them.

## Upgrading the CLI

```bash
uv tool install --force git+https://github.com/VRTFinland/jailbee
```

JailBee is not on PyPI yet, so install it from the git repository; once the
first release is published, `uv tool install --force jailbee` is the same
thing and shorter. `--force` matters either way: `uv tool install` alone
won't overwrite an existing tool install. The old `gie` console script keeps
working after this — see [Deprecations](#deprecations-and-their-removal) —
so nothing breaks the moment you upgrade.

The pre-1.0 distribution stays registered as a separate uv tool with its own
virtualenv on disk. Remove it:

```bash
uv tool uninstall gisgro-incus-env
```

## What `jailbee migrate` does

`jailbee migrate` is host-level: it takes no repo config and walks every
repo whose containers this host knows about. Run it once after upgrading.
`--dry-run` prints the plan without changing anything; without `--yes` it
asks for confirmation before applying.

It moves seven kinds of pre-1.0 state into the `jailbee` namespace:

1. **Host directories.** `~/.config/gie` → `~/.config/jailbee`,
   `~/.local/share/gie` → `~/.local/share/jailbee`, and
   `~/.local/state/gie` → `~/.local/state/jailbee` (each respecting
   `XDG_CONFIG_HOME`/`XDG_DATA_HOME`/`XDG_STATE_HOME` if set). Each move is
   a single `shutil.move`. The data directory keeps a
   `~/.local/share/gie` → `~/.local/share/jailbee` symlink behind it,
   because your existing containers have absolute paths under the old
   location baked into their Incus disk devices — see
   [After the migration](#after-the-migration). If a *target* directory
   already exists, the migrator refuses to run rather than skip the move:
   see [What it refuses to do](#what-it-refuses-to-do-and-why).
2. **Container labels.** Every container still carrying `user.gie.*`
   config keys (`user.gie.base_branch`, `user.gie.pr_branch`, …) gets each
   key copied to `user.jailbee.*` and the old key unset. The
   `environment.GIE_BRANCH` container env var becomes
   `environment.JAILBEE_BRANCH`.
3. **Git refs.** In every repo it can find — from container labels and from
   the registered-repo table in the pre-1.0 state database —
   `refs/gie/*` and `refs/gie-sub/*` (the fetch-proof base anchors and
   submodule anchors) are recreated as `refs/jailbee/*` /
   `refs/jailbee-sub/*` at the same object id, and the old ref is deleted.
   If the new ref can't be created (a name conflict, a stale lock, a
   read-only repo), the old one is kept and a warning names it.
4. **systemd units.** The pre-1.0 `gie-net-refresh.timer` and
   `gie-net-refresh.service` user units are disabled and removed, and the
   current `jailbee-net-refresh` units are installed in their place. If
   `jailbee` isn't on your `PATH` the migrator stops here rather than
   leaving you with no refresh timer at all.
5. **The registry mirror.** The old `gie-registry-mirror` container and
   `gie-registry-mirror-profile` profile are deleted outright rather than
   renamed — mirror state is a cache, not data worth carrying forward. Run
   `jailbee registry up` afterwards if you use the shared Docker registry
   mirror; it rebuilds the container fresh under the new name. Containers
   that trusted the old mirror's CA get that anchor (and its Java keystore
   alias) removed the next time `jailbee apply`/`jailbee new` rewrites their
   Docker proxy configuration.
6. **The shared bridge.** `gie-loose` is *not* renamed — Incus refuses to
   rename a network while any instance **or profile** uses it, and every
   initialised repo has a `<prefix>-net-loose` profile pointing at it.
   Instead the migrator ensures `jailbee-loose` exists, rewrites every loose
   profile that references `gie-loose` to point at the new bridge, and then
   deletes `gie-loose`. If something else still holds it, the migrator says
   what — by name — and exits non-zero with everything else already
   migrated; fix those and re-run.
7. **Bundled Claude skills.** Stale `gie-usage`/`gie-repo-setup` skill
   directories and the `.gie-skills.lock` file are deleted from every
   repo's `<shared_dir>/claude/skills/`. The current `jailbee-usage`/
   `jailbee-repo-setup` skills are (re)installed the normal way, by
   `jailbee new`/`jailbee apply`, not by the migrator.

Each of these is independently idempotent: state already in the `jailbee`
namespace is left alone, so re-running `jailbee migrate` after a partial or
interrupted run only picks up what's left.

## After the migration

**Update your host firewall rules if you have any.** This one bites hardest
because nothing reports it: the migrator renames the shared bridge from
`gie-loose` to `jailbee-loose`, and every firewall rule you added at install
time still names the old, now-deleted interface. Containers on the new
bridge then get **no IPv4 address at all** — UFW ships a silent DROP for
DHCP destined to the host, so `dnsmasq` never sees the request. The
symptoms are indirect and none of them points at the firewall: `jailbee
registry up` hangs for ten minutes and dies with `Temporary failure
resolving 'archive.ubuntu.com'`, `jailbee base build` does the same, and a
container on the bridge has a working IPv6 address (the kernel autoconfigures
that from router advertisements, which need nothing inbound) beside an empty
IPv4 one. `jailbee doctor` reports the bridge as present, because it is —
it just carries no traffic.

Reproduce whatever you have for `gie-loose` under the new name. For UFW that
is a route rule plus three lines in `/etc/ufw/before.rules`:

```bash
sudo ufw route allow in on jailbee-loose
```

```
# in /etc/ufw/before.rules, inside *filter, before COMMIT
-A ufw-before-input -i jailbee-loose -p udp --dport 67 -j ACCEPT
-A ufw-before-input -i jailbee-loose -p udp --dport 53 -j ACCEPT
-A ufw-before-input -i jailbee-loose -p tcp --dport 53 -j ACCEPT
```

```bash
sudo ufw reload
```

The `ufw route` rule alone is not enough — it covers forwarding, while DHCP
is addressed to the host itself. For firewalld it is
`sudo firewall-cmd --permanent --zone=trusted --add-interface=jailbee-loose`
followed by `sudo firewall-cmd --reload`. Either way, see
[Installation: Host networking](installation.md#host-networking-only-if-you-use-a-firewall)
for the full set, then drop the leftover `gie-loose` rules.

Verify with a throwaway container — this is the check that would have caught
it:

```bash
incus launch images:ubuntu/26.04 fwtest --network jailbee-loose -c security.nesting=true
sleep 15
incus list fwtest          # the IPv4 column must not be empty
incus delete -f fwtest
```

**Run `jailbee apply` in each repo.** Incus disk devices store *absolute*
host paths, and moving the data directory changes them. `jailbee apply`
rewrites each repo's profile devices to the new `~/.local/share/jailbee/…`
paths. The compatibility symlink the migrator leaves behind keeps existing
containers — including their per-container devices, which `apply` does not
touch — starting in the meantime, but it disappears in 1.1.0.

**Rebuild the golden image when convenient** (`jailbee base build`). Images
built before the rename ship a `.bashrc` that reads `$GIE_BRANCH`, while
migrated containers export `JAILBEE_BRANCH` — so the branch segment quietly
drops out of the shell prompt in containers created from the old image until
the image is rebuilt. Nothing else depends on it.

**In-container `refs/gie/*` are left alone.** The migrator only rewrites
refs in host repos. Inside an existing container, the base-ref lookup falls
back to `origin/<base>` and the next push rewrites the host-side ref, so
nothing breaks — you just lose the fetch-proof guarantee for that one
container until then. Recreating the container clears it.

## What it refuses to do, and why

`jailbee migrate` never touches `.gie/config.yaml` inside an application
repo. That file is usually committed and shared — renaming it is a commit
in *your* repo, on *your* branch, that every teammate and CI job needs to
pick up in lockstep with their own tool upgrade. A host-level migrator has
no business making that commit for you. Instead, once it has relabeled the
containers backed by a repo, it prints the exact command to run by hand:

```
Still to do by hand, in /home/you/dev/SampleApp: git mv .gie .jailbee
```

Run that `git mv`, commit it, and push it through your normal review
process, same as any other change to that repo. Until you do, `jailbee`
keeps accepting `.gie/config.yaml` as a fallback (see
[Deprecations](#deprecations-and-their-removal) below) — nothing breaks in
the meantime, but every load prints a one-time warning naming the exact
`git mv` to run.

### A target directory that already exists

`~/.local/state/jailbee` being there while `~/.local/state/gie` still holds
your real state is the normal case, not an exotic one: `jailbee doctor`,
`jailbee net install`, `jailbee net refresh` and either refresh timer all
create the new state directory on sight. Installing from a checkout with
`make install` creates it too, because that target runs `jailbee net
install`.

You do not have to do anything about it. `jailbee migrate` stops both
refresh timers before it plans anything, then:

- **If the directory holds no state** — empty, or nothing but a
  freshly-bootstrapped `state.sqlite` with no rows — it is deleted and the
  move proceeds. The plan shows this as a `clear` line.
- **If it holds anything else**, the plan marks it `CLEAR?` and you are
  asked, by path and with its contents listed, before anything is deleted.
  Decline and nothing is touched, so you can merge the two directories by
  hand instead. `--yes` deliberately does *not* answer this question: it
  skips the confirmation prompt, not the decision to lose state, and an
  unattended run refuses rather than deleting.

The same existence check runs again immediately before each move, so a
directory that reappears between the plan and your confirmation stops the
migration rather than nesting your old state inside the new directory.

`jailbee migrate` refuses to run at all — printing `BLOCKED` and changing
nothing — under two conditions:

- **Background jobs are pending.** If the pre-1.0 state database still has
  in-flight or unacknowledged jobs recorded, migrating could lose track of
  them. Let them finish, then re-run. Note that `jailbee job clear` reads
  the *new* database and will not find them; the blocker prints the old
  database's path and the exact row to delete.
- **A container is still attached to the old loose bridge.** Run
  `jailbee net strict <name>` on each container the plan lists — from that
  container's own repo, since the command loads a repo config — then re-run
  `jailbee migrate`; switch them back to loose afterwards. This keeps the
  migration from rewiring a live container's NIC. The old registry mirror
  holds the bridge too, but never appears in this blocker: the plan deletes
  it outright (step 5 above), so there is nothing for you to do about it.

`jailbee migrate --dry-run` shows you the plan (including any blockers)
without changing anything, so you can see what's blocking before deciding
how to unblock it.

## Deprecations and their removal

Six pieces of pre-1.0 compatibility exist purely to make this migration
gradual. All six are removed in **1.1.0** — don't build anything new on
top of them:

- **The `gie` console script.** `pip`/`uv` installs `jailbee`, `jb`, and
  `gie` as three entry points to the same CLI today; `gie` is the pre-1.0
  name kept as an alias. Scripts, aliases, and muscle memory using `gie
  ...` keep working until 1.1.0, when the `gie` entry point is removed
  from `pyproject.toml` entirely.
- **The `.gie/config.yaml` fallback.** `jailbee` prefers
  `<repo>/.jailbee/config.yaml` but still reads `<repo>/.gie/config.yaml`
  if that's all a repo has, with a one-time warning per process telling
  you which `git mv` to run. In 1.1.0 only `.jailbee/config.yaml` is
  recognized — a repo that hasn't run the `git mv` by then stops loading.
- **`claude.install_gie_skills` as a config alias.** The config key is now
  `claude.install_jailbee_skills`; `install_gie_skills` is accepted as an
  alias for the same field, with a deprecation warning, so an existing
  `global.yaml` or `.gie/config.yaml` written before the rename still
  validates. In 1.1.0 the alias is dropped and only
  `install_jailbee_skills` is recognized.
- **The legacy `/etc/hosts` sentinel.** Strict-mode containers pin
  resolved egress IPs into `/etc/hosts` between a pair of sentinel
  comments; pre-1.0 containers carry the old
  `# BEGIN gie-managed allowlist` / `# END gie-managed allowlist` markers
  instead of the current `jailbee-managed` ones. `jailbee net refresh`
  recognizes both forms so it replaces the old block instead of leaving it
  behind and appending a second one. Once every container has been
  through `jailbee migrate` and at least one `net refresh`, the old marker
  never appears again; 1.1.0 stops recognizing it.
- **The `<data>/gie` compatibility symlink.** `jailbee migrate` leaves
  `~/.local/share/gie` behind as a symlink to `~/.local/share/jailbee`, so
  that the absolute disk-device sources baked into existing profiles and
  containers keep resolving after the move. `jailbee apply` rewrites the
  profile-level ones; per-container devices are attached once at creation
  and never refreshed, so a container created before the rename depends on
  this symlink for as long as it lives. 1.1.0 removes it — recreate any
  surviving pre-rename container before then.
- **`jailbee migrate` itself.** The migrator only exists to walk installs
  that predate the rename to a clean slate. Once your fleet is migrated,
  there is nothing left for it to do — `jailbee doctor` reports "pre-1.0
  gie state: none". The whole `migrate` command and its module are
  removed in 1.1.0, together with the rest of this compatibility surface;
  from then on a `gie`-era install must be brought current on 1.0.x first.

Run `jailbee doctor` any time to see whether any of this still applies to
your machine — its "pre-1.0 gie state" check inspects the old directories,
containers, bridge, units, skills and refs directly and names what it
finds, reporting `none` once nothing is left and every repo's config
directory has been `git mv`'d.
