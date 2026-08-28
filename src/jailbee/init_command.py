"""Implementation of `jailbee init` — sets up profiles, ACLs, shared dirs."""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from jailbee.config import Config, ConfigError
from jailbee.constants import SHARED_SUBDIRS
from jailbee.egress import EgressEntry, build_egress_entries
from jailbee.incus import Incus, IncusError
from jailbee.network import acl_name, allowlist_acl_yaml
from jailbee.profiles import (
    CLAUDE_CREDS_DEVICE,
    base_profile_yaml,
    binds_profile_yaml,
    claude_config_dir_env,
    claude_securestorage_dir_env,
    net_profile_yaml,
    profile_names,
)
from jailbee.ssh_seed import seed_ssh_dir
from jailbee.tui import (
    ChooseCredentialFn,
    choose_shared_credential,
    info,
    success,
    warn,
)

BRIDGE_NETWORK = "incusbr0"
LOOSE_BRIDGE = "jailbee-loose"


def run_init(
    cfg: Config,
    incus: Incus,
    *,
    mirror_endpoint: tuple[str, int] | None = None,
) -> None:
    """Set up Incus profiles, network ACL, and shared directory structure.

    First-time setup only. If any profile or ACL already exists, raises
    ``RuntimeError`` pointing the user at ``jailbee apply``. The other
    side effects (shared dirs, Claude seed, bridge create, bridge ACL
    attach) are idempotent and safe to re-run after a partial init.

    `mirror_endpoint`, when provided, is forwarded to the allowlist ACL so
    that strict-mode containers can reach the host Docker registry mirror.
    """
    assert cfg.shared_dir is not None  # set by load_config
    _ensure_shared_owner(cfg.shared_dir, cfg.repo_root)
    _ensure_shared_dirs(cfg.shared_dir)
    _ensure_user_shared_dirs(cfg)
    success(f"Shared directory tree present at {cfg.shared_dir}")

    # SSH refuses to use ~/.ssh with group/world bits. Force 0700 on
    # the shared dir whenever SSH is enabled — the bind-mount target
    # perms must satisfy SSH's check inside the container regardless of
    # seed_from_host or whether the host ~/.ssh exists.
    if cfg.ssh.enabled:
        (cfg.shared_dir / "ssh").chmod(0o700)

    # Claude + JetBrains integration mounts. Shared with `apply` via this
    # helper so the two can't drift (issue: apply once forgot claude-install).
    _ensure_integration_shared_dirs(cfg)
    if not cfg.claude.enabled:
        info("Claude integration disabled (claude.enabled=false) — skipping subdir")

    # `init` gets the same offer `apply` does — it is equally a terminal
    # command — but keeps its strict semantics: a fresh repo whose pool root
    # is polluted and stays that way is a situation to stop on, not to warn
    # past. `cli.init` renders the `PoolError` as a plain error line.
    from jailbee.lifecycle import _stdin_is_interactive
    from jailbee.pool import PoolError, preflight_pools
    from jailbee.tui import default_confirm

    unresolved = preflight_pools(
        cfg, confirm=default_confirm if _stdin_is_interactive() else None
    )
    if unresolved:
        raise PoolError(
            f"cache pool {', '.join(unresolved)} holds both pool slots and loose "
            f"cache content. Move the loose entries out of the pool root, then re-run."
        )

    if cfg.ssh.enabled and cfg.ssh.seed_from_host:
        ssh_target = cfg.shared_dir / "ssh"
        host_ssh = Path.home() / ".ssh"
        n = seed_ssh_dir(ssh_target, host_ssh)
        if n > 0:
            success(f"Seeded {n} items from {host_ssh} into {ssh_target}")
        elif not host_ssh.is_dir():
            info("Host ~/.ssh not present — skipping SSH seed")
        # target non-empty: silent (expected on re-init)
    elif not cfg.ssh.enabled:
        info("SSH disabled in config — skipping seed and chmod")
    else:
        info("SSH seed disabled in config — skipping")

    names = profile_names(cfg)
    _apply_profile_strict(incus, names.base, base_profile_yaml(cfg))
    _apply_profile_strict(incus, names.binds, binds_profile_yaml(cfg))

    # ACL must exist before the strict net profile, which references it via
    # `security.acls: <repo>-allowlist`. Otherwise Incus rejects the eth0
    # device with "Network ACL ... does not exist".
    apply_allowlist_acl(cfg, incus, mirror_endpoint=mirror_endpoint)

    # Attach the ACL also at the bridge network level.
    ensure_acl_attached_to_bridge(cfg, incus)

    # Shared loose bridge — `<prefix>-net-loose` references it.
    ensure_loose_bridge(incus)

    for mode, name in names.net_by_mode.items():
        _apply_profile_strict(incus, name, net_profile_yaml(cfg, mode))

    success("All profiles and ACL applied")


def _ensure_shared_dirs(shared_dir: Path) -> None:
    for sub in SHARED_SUBDIRS:
        (shared_dir / sub).mkdir(parents=True, exist_ok=True)


def _ensure_user_shared_dirs(cfg: Config) -> None:
    """Create each user-defined `shared_caches` host_subpath as a directory.

    Without this, `incus profile assign` fails validation with a missing
    disk source path for any custom `shared_caches` entry whose
    host_subpath isn't one of the built-in `SHARED_SUBDIRS`.

    Only the user's `shared_caches` list is iterated (not
    `effective_shared_caches()`): the agent/jetbrains auto-adds are handled
    separately, and a user-declared `type: file` mount is a *file*-level disk
    that must not be created as a directory. File-type binds the user wants
    must be pre-created by hand — see `_ensure_integration_shared_dirs`.
    """
    assert cfg.shared_dir is not None  # set by load_config
    for cache in cfg.shared_caches:
        (cfg.shared_dir / cache.host_subpath).mkdir(parents=True, exist_ok=True)


def _relocate_claude_json(cfg: Config) -> None:
    """Move a legacy `<shared_dir>/claude.json` into `claude/.claude.json`.

    One-time upgrade step for repos initialised before Claude Code's global
    config moved inside the `claude` directory mount (the golden image now
    exports `CLAUDE_CONFIG_DIR=$HOME/.claude`, and Claude Code reads
    `(CLAUDE_CONFIG_DIR || $HOME)/.claude.json`).

    Idempotent and never destructive: with the destination already present the
    source is a leftover from before the move, so it is left exactly as it is
    rather than overwriting live state or deleting the user's copy.

    Caller ordering note: moving the source of a disk device a live binds
    profile still declares makes every `incus start` / `profile assign` for
    the repo fail with "Missing source path ...". `apply.run_apply` may call
    this directly because it rewrites the binds profile on the same run —
    and if it aborts in between, the next `apply` re-runs this (idempotent)
    and self-heals. **Every other caller must go through
    `migrate_claude_json`**, which pairs the move with that profile write;
    nothing else on those paths ever rewrites the profile, so there the
    breakage would be permanent.
    """
    assert cfg.shared_dir is not None  # set by load_config
    source = cfg.shared_dir / "claude.json"
    target = cfg.shared_dir / "claude" / ".claude.json"
    if source.is_symlink():
        warn(
            f"{source} is a symlink — leaving it in place, not relocating to {target}. "
            "Move it into place by hand if it should back Claude Code's global config."
        )
        return
    if not source.is_file() or target.exists():
        if source.is_file() and target.exists():
            warn(
                f"Legacy {source} left in place — {target} already exists. "
                "If the legacy file holds real Claude Code state, merge it "
                "into the destination by hand; jailbee never overwrites an "
                "existing destination."
            )
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)
    success(f"Moved {source} → {target}")


def migrate_claude_json(cfg: Config, incus: Incus) -> None:
    """Retire the `shared-claude-json` disk device, then relocate its source.

    The two halves are inseparable, and only `run_apply` gets them for free:
    it rewrites `<prefix>-binds` on every run anyway, so it calls
    `_relocate_claude_json` directly. Every other caller must pair them —
    a pre-1.2.0 binds profile still declares a disk device whose source is
    `<shared_dir>/claude.json`, and Incus refuses every `start` and
    `profile assign` for the repo once a disk device's source path is
    missing. Moving the file without rewriting the profile therefore breaks
    the repo until the user happens to run `jailbee apply`, and it does not
    self-heal: the relocation is a no-op on every later run.

    The profile is rewritten **before** the move, not after. The rendered
    YAML no longer declares the device, so the write is valid while the file
    is still in place, and a failure leaves the repo exactly as it was.

    Only `<prefix>-binds` is refreshed, not the whole profile set `apply`
    writes: the device is what makes the move unsafe, and `jailbee new` has
    no business re-applying the rest of the config.

    Idempotent: once the legacy path is gone this returns immediately.
    """
    assert cfg.shared_dir is not None  # set by load_config
    source = cfg.shared_dir / "claude.json"
    if not source.is_file() and not source.is_symlink():
        return
    names = profile_names(cfg)
    if incus.profile_exists(names.binds):
        incus.profile_set_yaml(names.binds, binds_profile_yaml(cfg))
    _relocate_claude_json(cfg)


def ensure_claude_config_dir(cfg: Config, incus: Incus) -> None:
    """Put `CLAUDE_CONFIG_DIR` on `<prefix>-base` when nothing declares it yet.

    The relocation has two halves, and only one of them is on the
    `jailbee new` path. `migrate_claude_json` retires the
    `shared-claude-json` device for the whole repo — which is what used to
    make `.claude.json` shared — while the replacement, Claude Code being
    pointed at the shared `~/.claude` mount, is written only by
    `jailbee apply` (this profile key) and `jailbee base build`
    (`/etc/profile.d/jailbee-env.sh`). A user who upgrades jailbee and just
    keeps running `jailbee new` therefore ends up with neither: Claude Code
    resolves the container-local `$HOME/.claude.json`, finds nothing, and
    onboards from scratch in every new container — and in the repo's
    existing ones too, since the device left their expanded config with the
    profile rewrite. The upgrade hint is advice, not a gate, so nothing else
    stops that.

    The profile key alone is enough to restore the old experience: Incus
    injects `environment.*` into every `incus exec` whatever the image holds
    (see `docs/manual-testing.md`), so no rebuild is needed. `base build`
    stays the belt-and-suspenders half, for shells jailbee does not spawn.

    Surgical on purpose — one key, not a `base_profile_yaml` rewrite:
    `jailbee new` has no business applying the rest of a config the user has
    not `apply`ed. An already-present value is left untouched, so a
    `container.env` override stays authoritative and a later `apply` is
    still the thing that renders the profile.
    """
    names = profile_names(cfg)
    if not incus.profile_exists(names.base):
        return
    key, value = claude_config_dir_env(cfg)
    if incus.profile_config_get(names.base, key) is not None:
        return
    incus.profile_config_set(names.base, key, value)
    success(f"Set {key}={value} on '{names.base}' — Claude Code's global config is shared again")


def _claude_creds_device_present(cfg: Config, incus: Incus) -> bool:
    """True if `<prefix>-binds` already carries the `claude-creds` disk
    device, i.e. a container created right now would actually find the
    shared credential mounted at `~/.claude-creds`.

    This, not the host group directory's existence, is the right gate for
    `ensure_claude_credentials_env`: the group directory can already exist
    because *another* member repo has run `jailbee apply`, while this repo's
    own `<prefix>-binds` still lacks the device — the host directory check
    would pass while the container it repairs still finds nothing. The
    device is only ever attached by `jailbee apply` rendering
    `binds_profile_yaml`, so its presence is the direct fact this function
    needs.
    """
    names = profile_names(cfg)
    if not incus.profile_exists(names.binds):
        return False
    parsed = yaml.safe_load(incus.profile_show(names.binds)) or {}
    devices = parsed.get("devices") or {}
    return CLAUDE_CREDS_DEVICE in devices


def ensure_claude_credentials_env(cfg: Config, incus: Incus) -> None:
    """Put `CLAUDE_SECURESTORAGE_CONFIG_DIR` on `<prefix>-base` when absent.

    The `jailbee new` twin of `ensure_claude_config_dir`, and needed for the
    same reason: `new` renders no profile, so a container created after this
    repo joined a credential group would quietly keep using the repo's own
    credential — logged in as a different account than every other member.

    Conservative on purpose (Finding 2 of the 2026-08-27 review): the repair
    is skipped entirely until `<prefix>-binds` already carries the
    `claude-creds` device (`_claude_creds_device_present`). The one
    reachable case where the device is missing is the half-joined
    state — `claude_credentials` configured in `global.yaml`, but `jailbee
    apply` not yet run — and writing the env key there would point a `jb
    new` container's Claude Code at a directory nothing mounts, logging out
    *every* container in the repo, while the still-valid credential sits
    untouched at `<shared_dir>/claude`. Skipping leaves that half-joined repo
    behaving exactly as it did before sharing existed, and `jailbee doctor`'s
    existing half-join check is what tells the user to run `jailbee apply`.

    Surgical on purpose otherwise — one key, not a `base_profile_yaml`
    rewrite. An already-present value is left untouched, so a
    `container.env` override stays authoritative.

    Only ever *adds* the key. Leaving a group removes it on the next
    `jailbee apply`, which rewrites the whole profile; `new` is not the place
    to undo configuration.
    """
    env = claude_securestorage_dir_env(cfg)
    if env is None:
        return
    if not _claude_creds_device_present(cfg, incus):
        return
    names = profile_names(cfg)
    if not incus.profile_exists(names.base):
        return
    key, value = env
    if incus.profile_config_get(names.base, key) is not None:
        return
    incus.profile_config_set(names.base, key, value)
    success(f"Set {key}={value} on '{names.base}' — this repo shares a Claude credential")


def _seed_claude_json(cfg: Config) -> None:
    """Write `<shared_dir>/claude/.claude.json` as `{}` when absent.

    Incus no longer requires this file to exist (there is no file-level disk
    device to give a source path to), but the seed is kept deliberately: an
    *empty* file fails Claude Code's parse with `Unexpected EOF`, and a valid
    `{}` is the known-good pre-first-run state this repo has always shipped.
    Claude Code rewrites it on first run.
    """
    assert cfg.shared_dir is not None  # set by load_config
    target = cfg.shared_dir / "claude" / ".claude.json"
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n")


def _ensure_claude_credentials_dir(
    cfg: Config,
    *,
    choose_fn: ChooseCredentialFn | None = None,
) -> None:
    """Create the shared credential directory and seed it once.

    Runs on both `jailbee init` and `jailbee apply` via
    `_ensure_integration_shared_dirs`, for the reason stated there: the binds
    profile names this directory as a disk source, and Incus rejects every
    `profile edit`/`profile assign` when a source path is missing.

    Four cases, and the two-credential one is the interesting one:

    * group directory empty, repo has a credential → **move** it in. A copy
      would give one refresh-token lineage two refreshers, and the first
      rotation silently logs one side out.
    * both hold one → **ask** (`choose_fn`, default
      `tui.choose_shared_credential`). Exactly one login can be shared and
      the other becomes unused, so the answer is the user's; the loser is
      deleted rather than kept, since nothing would ever read it again and a
      stale grant left in the shared tree only invites confusion. Cancelling
      — or having no TTY to ask on — raises the original `ConfigError`, which
      still names the `claude_credentials.repos` opt-out for a user who wants
      neither shared login. Deleting a credential is safe here precisely
      because the two are *independent* grants: two `/login`s to one account
      each mint their own refresh-token lineage, so deleting one leaves the
      survivor's untouched. (Copying a credential blob to two places is the
      operation that logs one side out; deleting one of two grants is not.)
    * only the group holds one → nothing to do; the mount does the rest.
    * neither → nothing to do; the first `/login` in any member lands here.

    Mode 0700: unlike the rest of the shared tree this directory holds a live
    credential, and it lives outside every repo. The container's dev user is
    idmapped to the host user, so 0700 is still readable inside.

    No `.owner` stamp (see `_ensure_shared_owner`): being shared by several
    repos is the entire point here.
    """
    group_dir = cfg.claude_credentials_dir
    if group_dir is None:
        return
    assert cfg.shared_dir is not None  # set by load_config

    group_cred = group_dir / ".credentials.json"
    repo_cred = cfg.shared_dir / "claude" / ".credentials.json"

    if group_cred.exists() and repo_cred.exists():
        keep = (choose_fn or choose_shared_credential)(group_dir, repo_cred, cfg.container_prefix)
        if keep is None:
            raise ConfigError(
                f"{group_dir} already holds a credential, and so does this repo "
                f"({repo_cred}). Sharing one account means one of the two logins "
                f"becomes unused, and jailbee will not choose for you. Either "
                f"delete this repo's copy to adopt the group's login, or point "
                f"this repo at another group (or `null`) under "
                f"`claude_credentials.repos` in ~/.config/jailbee/global.yaml."
            )
        if keep == "group":
            repo_cred.unlink()
            success(f"Adopted the group's Claude login; deleted this repo's copy: {repo_cred}")
        else:
            group_cred.unlink()
            success(f"Replaced the group's Claude login with this repo's: {group_dir}")

    group_dir.mkdir(parents=True, exist_ok=True)
    group_dir.chmod(0o700)

    if not group_cred.exists() and repo_cred.exists():
        # shutil.move, not Path.rename: `shared_dir` can be overridden to
        # another filesystem, where rename fails with EXDEV.
        shutil.move(str(repo_cred), str(group_cred))
        success(f"Moved this repo's Claude credential into the shared group dir: {group_dir}")


def _ensure_integration_shared_dirs(
    cfg: Config,
    *,
    choose_fn: ChooseCredentialFn | None = None,
) -> None:
    """Create the shared subdirs/files each enabled agent bind-mounts, plus
    JetBrains' subdirs.

    Single source of truth for `run_init` and `apply`: both must create the
    exact same set, or a repo initialised before a given integration's mounts
    were added ends up with a binds profile referencing a non-existent disk
    source path. Incus then rejects every `profile edit`/`profile assign`
    touching that profile with "Missing source path ... for disk ...".

    Previously init and apply each hand-rolled this block and drifted: apply
    forgot `claude-install`, so `jailbee apply`/`jailbee new` failed on any repo whose
    `claude-install` dir wasn't already on disk. Keep them sharing this helper.

    Directory-type mounts (`spec.dir_subpaths`) are `mkdir`'d; file-type
    mounts (`spec.seed_files`) are seeded with their configured content only
    if they don't already exist — required because Incus rejects the
    container start if a file-level disk device's source is missing. Claude
    Code's global config, `.claude.json`, is no longer one of these: it lives
    inside the `claude` directory mount (the golden image exports
    `CLAUDE_CONFIG_DIR=$HOME/.claude`), seeded by `_seed_claude_json` and
    migrated from its old file-mount location by `_relocate_claude_json`. An
    empty/zero-byte `.claude.json` still fails Claude Code's parse
    (`Unexpected EOF`), which under `ensure-claude.sh`'s `pipefail` aborts the
    binary install before the shared store is populated, hard-failing the
    first `jailbee new` for the repo — hence the seed.
    """
    from jailbee.agents import enabled_agent_specs

    assert cfg.shared_dir is not None  # set by load_config
    for spec in enabled_agent_specs(cfg):
        for subpath in spec.dir_subpaths:
            (cfg.shared_dir / subpath).mkdir(parents=True, exist_ok=True)
        for subpath, seed in spec.seed_files:
            target = cfg.shared_dir / subpath
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(seed)
    if cfg.claude.enabled:
        # Order matters: relocate first, seed second. Reversed, the seed
        # creates `{}` at the destination, the relocation then no-ops on an
        # existing target, and a real pre-move `.claude.json` is orphaned.
        _relocate_claude_json(cfg)
        _seed_claude_json(cfg)
        _ensure_claude_credentials_dir(cfg, choose_fn=choose_fn)
    if cfg.jetbrains.enabled:
        (cfg.shared_dir / "jetbrains-config").mkdir(parents=True, exist_ok=True)
        (cfg.shared_dir / "jetbrains-data").mkdir(parents=True, exist_ok=True)
        if cfg.jetbrains.share_idea:
            (cfg.shared_dir / "jetbrains-idea").mkdir(parents=True, exist_ok=True)


def _ensure_shared_owner(shared_dir: Path, repo_root: Path) -> None:
    """Write/verify the .owner stamp file for the shared dir.

    First call creates `<shared_dir>/.owner` with the absolute repo root path.
    Subsequent calls: if the existing owner doesn't match, raise — two repos
    cannot share the same shared_dir. Override by setting an explicit
    `shared_dir` in .jailbee/config.yaml.
    """
    shared_dir.mkdir(parents=True, exist_ok=True)
    owner_file = shared_dir / ".owner"
    if owner_file.exists():
        existing = owner_file.read_text().strip()
        if existing != str(repo_root):
            raise ConfigError(
                f"shared_dir collision: {shared_dir} is owned by {existing}, "
                f"but you're running jailbee from {repo_root}. "
                f"Override `shared_dir` in .jailbee/config.yaml to a different path."
            )
    else:
        owner_file.write_text(str(repo_root) + "\n")


def _apply_profile_strict(incus: Incus, name: str, yaml_content: str) -> None:
    """Create the profile if absent. Raise if it already exists.

    `jailbee init` is first-time setup only; existing profiles indicate the
    repo has already been initialized and the user should use
    `jailbee apply` to update them instead.
    """
    if incus.profile_exists(name):
        raise RuntimeError(
            f"Profile '{name}' already exists. "
            f"Use `jailbee apply` to update profiles from current config."
        )
    incus.profile_create(name)
    incus.profile_set_yaml(name, yaml_content)
    success(f"Created profile: {name}")


def apply_allowlist_acl(
    cfg: Config,
    incus: Incus,
    *,
    entries: list[EgressEntry] | None = None,
    mirror_endpoint: tuple[str, int] | None = None,
) -> None:
    """Generate and apply the strict-profile allowlist ACL (first-time only).

    Raises ``RuntimeError`` if the ACL already exists — use `jailbee apply` to
    update it. Pass `entries` to reuse already-resolved entries so the same
    DNS answers are used for both ACL and `/etc/hosts`. When omitted,
    resolves hostnames in `cfg.effective_egress_allow()` to IPv4 here (the
    caller — `run_init` — has no pre-resolved entries of its own).
    Pass `mirror_endpoint=(ip, port)` to auto-include the host Docker
    registry mirror rule.
    """
    if entries is None:
        # Deliberately NOT egress_scope.effective_repo_entries: run_init has no
        # DB session to read host-local repo overrides from, so the very first
        # ACL jailbee writes can lag one added before `jailbee init` ran. The
        # window closes on the first refresh — `refresh_pool` (which both
        # `jb new` and `jb apply` run before a container can observe the ACL)
        # is wired to `effective_repo_entries` and rewrites this same ACL.
        entries = build_egress_entries(cfg.effective_egress_allow())
    _apply_acl_strict(
        incus,
        acl_name(cfg),
        allowlist_acl_yaml(cfg, entries, mirror_endpoint=mirror_endpoint),
    )


def ensure_acl_attached_to_bridge(cfg: Config, incus: Incus) -> None:
    """Ensure `<repo>-allowlist` is in `incusbr0`'s `security.acls` list.

    Without this, Incus 6.0.x generates the bridge-family allow rules
    per-NIC but leaves the inet-family `acl.<bridge>` chain with only
    the implicit reject. `bridge-nf-call-iptables=1` (kernel default,
    required for Docker) then rejects bridge-forwarded SYNs before the
    bridge-family allow can match.

    Idempotent. Preserves entries from other jailbee-managed repos that
    share `incusbr0`.
    """
    name = acl_name(cfg)
    current = incus.network_get(BRIDGE_NETWORK, "security.acls")
    attached = [a for a in current.split(",") if a]
    if name in attached:
        info(f"ACL {name} already attached to {BRIDGE_NETWORK}")
        return
    attached.append(name)
    incus.network_set(BRIDGE_NETWORK, "security.acls", ",".join(attached))
    success(f"ACL {name} attached to {BRIDGE_NETWORK}")


def ensure_loose_bridge(incus: Incus) -> None:
    """Ensure the shared `jailbee-loose` bridge exists. Idempotent.

    Shared across all jailbee-managed repos on the host. Carries no ACL —
    containers on this bridge are exempt from per-repo allowlist
    filtering.

    Skipped if a network with this name already exists. Does not verify
    the existing network's type or config — user-managed differences are
    respected.
    """
    if incus.network_exists(LOOSE_BRIDGE):
        info(f"Network {LOOSE_BRIDGE} already exists")
        return
    incus.network_create(LOOSE_BRIDGE)
    success(f"Created network {LOOSE_BRIDGE}")


def _apply_acl_strict(incus: Incus, name: str, yaml_content: str) -> None:
    """First-time-only ACL creation. Raises if the ACL already exists.

    The `acl edit` step tolerates the Incus ≤6.18 nft-flush-chain quirk:
    on a fresh host the chain doesn't exist yet and Incus's live nftables
    sync fails even though the ACL is persisted in its database.
    """
    if incus.network_acl_exists(name):
        raise RuntimeError(
            f"ACL '{name}' already exists. Use `jailbee apply` to update from current config."
        )
    incus.network_acl_create(name)
    info(f"Created ACL: {name}")
    try:
        incus.network_acl_set_yaml(name, yaml_content)
    except IncusError as e:
        if _is_nft_flush_chain_missing(str(e)):
            warn(
                f"ACL {name} updated in Incus, but nftables sync "
                f"failed (chain not yet present). Cosmetic — next "
                f"container start will refresh the chain."
            )
            return
        raise
    success(f"ACL {name} applied")


def _is_nft_flush_chain_missing(message: str) -> bool:
    """Detect Incus ≤6.18 nftables backend quirk: `nft flush chain` against
    a chain that hasn't been created yet.

    Sample stderr::

        `incus network acl edit <repo>-allowlist` failed:
        Error: Failed to run: nft -f -: exit status 1
        (/dev/stdin:2:24-35: Error: No such file or directory;
         did you mean chain 'fwd.incusbr0' in table inet 'incus'?
        flush chain inet incus acl.incusbr0
    """
    markers = ("nft", "flush chain", "No such file or directory")
    return all(m in message for m in markers)


NET_REFRESH_TIMER = "jailbee-net-refresh.timer"
NET_REFRESH_SERVICE = "jailbee-net-refresh.service"


def systemd_user_dir() -> Path:
    """Where the user's systemd units live.

    Deliberately `~/.config`, not `$XDG_CONFIG_HOME`: systemd --user reads
    the former unless *it* was started with the variable set, and jailbee
    cannot know that. Shared with `setup_command`, whose probe must look
    exactly where `install_systemd_units` writes.
    """
    return Path.home() / ".config" / "systemd" / "user"


def install_systemd_units() -> None:
    """Install the singleton jailbee-net-refresh timer + service.

    Idempotent: only rewrites units when contents change, daemon-reloads
    only on change; ``enable --now`` is cheap and is always called.
    Skipped (with a warning) when ``jailbee`` is not on PATH.
    """
    import shlex
    import shutil
    import subprocess
    from importlib.resources import files

    jailbee_bin = shutil.which("jailbee")
    if jailbee_bin is None:
        warn("jailbee not on PATH — systemd unit refresh disabled.")
        warn("  Re-run `jailbee init` after installing jailbee.")
        return

    units_dir = systemd_user_dir()
    units_dir.mkdir(parents=True, exist_ok=True)

    templates = files("jailbee.templates.systemd")
    service_template = (templates / "jailbee-net-refresh.service").read_text()
    timer_template = (templates / "jailbee-net-refresh.timer").read_text()

    service_rendered = service_template.replace("{jailbee_bin}", shlex.quote(jailbee_bin))

    changed = _write_if_changed(
        units_dir / NET_REFRESH_SERVICE, service_rendered
    ) | _write_if_changed(units_dir / NET_REFRESH_TIMER, timer_template)
    if changed:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)

    subprocess.run(
        ["systemctl", "--user", "enable", "--now", NET_REFRESH_TIMER],
        check=True,
    )
    info(f"Enabled refresh timer: {NET_REFRESH_TIMER}")


def _write_if_changed(path: Path, content: str) -> bool:
    """Write ``content`` to ``path`` only if different. Returns True if written."""
    if path.exists() and path.read_text() == content:
        return False
    path.write_text(content)
    return True
