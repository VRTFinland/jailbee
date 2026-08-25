"""Implementation of `jailbee init` — sets up profiles, ACLs, shared dirs."""

from __future__ import annotations

from pathlib import Path

from jailbee.config import Config, ConfigError
from jailbee.constants import SHARED_SUBDIRS
from jailbee.egress import EgressEntry
from jailbee.incus import Incus, IncusError
from jailbee.network import acl_name, allowlist_acl_yaml
from jailbee.profiles import (
    base_profile_yaml,
    binds_profile_yaml,
    net_profile_yaml,
    profile_names,
)
from jailbee.ssh_seed import seed_ssh_dir
from jailbee.tui import info, success, warn

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
    """
    assert cfg.shared_dir is not None  # set by load_config
    source = cfg.shared_dir / "claude.json"
    target = cfg.shared_dir / "claude" / ".claude.json"
    if not source.is_file() or target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)


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


def _ensure_integration_shared_dirs(cfg: Config) -> None:
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
    DNS answers are used for both ACL and `/etc/hosts`. Pass
    `mirror_endpoint=(ip, port)` to auto-include the host Docker registry
    mirror rule.
    """
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

    units_dir = Path.home() / ".config" / "systemd" / "user"
    units_dir.mkdir(parents=True, exist_ok=True)

    templates = files("jailbee.templates.systemd")
    service_template = (templates / "jailbee-net-refresh.service").read_text()
    timer_template = (templates / "jailbee-net-refresh.timer").read_text()

    service_rendered = service_template.replace("{jailbee_bin}", shlex.quote(jailbee_bin))

    changed = _write_if_changed(
        units_dir / "jailbee-net-refresh.service", service_rendered
    ) | _write_if_changed(units_dir / "jailbee-net-refresh.timer", timer_template)
    if changed:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)

    subprocess.run(
        ["systemctl", "--user", "enable", "--now", "jailbee-net-refresh.timer"],
        check=True,
    )
    info("Enabled refresh timer: jailbee-net-refresh.timer")


def _write_if_changed(path: Path, content: str) -> bool:
    """Write ``content`` to ``path`` only if different. Returns True if written."""
    if path.exists() and path.read_text() == content:
        return False
    path.write_text(content)
    return True
