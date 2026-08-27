"""Incus profile YAML generation from Config."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path

import yaml

from jailbee.config import CONTAINER_USERNAME, NET_DESCRIPTIONS, Config
from jailbee.network import acl_name

LOOSE_PROFILE_SUFFIX = "-net-loose"
"""Suffix of the per-repo loose network profile (``<prefix>-net-loose``).

Named so the migrator can recognise a loose profile from its Incus name
alone, without loading the repo's Config.
"""

LOOSE_BRIDGE = "jailbee-loose"
"""Shared bridge the loose profile's NIC attaches to.

Duplicated from `init_command.LOOSE_BRIDGE` (which owns creating it) because
`init_command` imports this module — importing back would be circular.
"""


@dataclass(frozen=True)
class ProfileNames:
    base: str
    binds: str
    net_strict: str
    net_loose: str

    @property
    def net_by_mode(self) -> dict[str, str]:
        return {
            "strict": self.net_strict,
            "loose": self.net_loose,
        }


def profile_names(cfg: Config) -> ProfileNames:
    p = cfg.container_prefix
    return ProfileNames(
        base=f"{p}-base",
        binds=f"{p}-binds",
        net_strict=f"{p}-net-strict",
        net_loose=f"{p}{LOOSE_PROFILE_SUFFIX}",
    )


def _host_render_nodes() -> list[Path]:
    """Sorted list of DRI render nodes on the host (``/dev/dri/renderD*``).

    Used to build the minimum GPU device surface for the container: each
    render node is passed in via its own ``unix-char`` device, while the
    KMS ``card*`` nodes and any ``/dev/nvidia*`` chardevs stay on the
    host. Render nodes are the only DRI surface Chrome (and Mesa-based
    apps) need for GPU-accelerated rendering when the host compositor
    owns the display.
    """
    return sorted(Path("/dev/dri").glob("renderD*"))


def claude_config_dir_env(cfg: Config) -> tuple[str, str]:
    """The `(key, value)` `<prefix>-base` carries so Claude Code reads its
    global config from the shared `~/.claude` mount.

    Single source of truth for the two writers of that key: this module's
    full profile render (`jailbee apply`) and `init_command`'s one-key repair
    on the `jailbee new` path. They must agree on both halves or the repair
    writes a value the next `apply` silently changes.

    `container.env` wins, mirroring the render order below — the repo may
    point Claude Code somewhere else on purpose.
    """
    default = f"/home/{CONTAINER_USERNAME}/.claude"
    return (
        "environment.CLAUDE_CONFIG_DIR",
        cfg.container.env.get("CLAUDE_CONFIG_DIR", default),
    )


CLAUDE_CREDS_DIRNAME = ".claude-creds"
"""Container-side directory name for a shared Claude credential.

Deliberately not `.claude`: only the credential is shared, and the config home
stays per-repo. Claude Code resolves `.credentials.json` *and*
`.oauth_refresh.lock` from `CLAUDE_SECURESTORAGE_CONFIG_DIR`, so the rotation
lock travels with the credential into this directory — which is what keeps
containers of different repos mutually excluded.
"""

CLAUDE_CREDS_DEVICE = "claude-creds"
"""Name of the `<prefix>-binds` disk device that mounts the shared credential
directory. Its presence on that profile is what `init_command`'s `jailbee
new` repair checks before writing the env key — see
`ensure_claude_credentials_env`.
"""


def claude_securestorage_dir_env(cfg: Config) -> tuple[str, str] | None:
    """The `(key, value)` that points Claude Code at a shared credential.

    `None` when this repo shares no credential, when Claude is disabled, or
    when the resolved value is empty. That last case is not paranoia: an empty
    `CLAUDE_SECURESTORAGE_CONFIG_DIR` is *not* the same as an unset one —
    Claude Code falls back to `~/.claude` for it, silently sending credential
    lookup back into the per-repo config home.

    Single source of truth for the two writers of the key, mirroring
    `claude_config_dir_env`: this module's full render (`jailbee apply`) and
    `init_command`'s one-key repair on the `jailbee new` path.
    """
    if not cfg.claude.enabled or cfg.claude_credentials_dir is None:
        return None
    default = f"/home/{CONTAINER_USERNAME}/{CLAUDE_CREDS_DIRNAME}"
    value = cfg.container.env.get("CLAUDE_SECURESTORAGE_CONFIG_DIR", default)
    if not value:
        return None
    return ("environment.CLAUDE_SECURESTORAGE_CONFIG_DIR", value)


def base_profile_yaml(cfg: Config) -> str:
    """Generate <repo>-base profile YAML.

    Includes security flags, UID/GID mapping, GPU passthrough, fonts,
    and environment variables for Wayland/X11/SSH-via-GPG.

    NOTE: The four ``/run/user/<uid>/*`` socket bind mounts (wayland-0,
    pulse, bus, gnupg) are intentionally NOT in this profile. Profile-
    level disk devices get mounted before PID 1, which forces Incus to
    auto-create ``/run/user/<uid>`` as a root-owned tmpfs that then gets
    shadowed by systemd-logind's user-owned tmpfs. Those
    devices are attached per-container *after* boot via
    ``runtime_mounts.attach_runtime_devices``.
    """
    uid = cfg.container_user.uid
    gid = cfg.container_user.gid
    runtime = f"/run/user/{uid}"

    devices: dict[str, dict[str, str]] = {
        "fonts": {
            "type": "disk",
            "source": "/usr/share/fonts",
            "path": "/usr/share/fonts",
            "readonly": "true",
        },
    }

    # Pass each host render node as an explicit unix-char device — the
    # minimum surface for GPU-accelerated Chrome/Mesa. Mode 0666 because
    # the dev user's UID is host-namespace via raw.idmap, but its
    # supplementary `render` group GID isn't translated; without 0666
    # the node lands as root:root mode 660 inside the container and
    # Chrome falls back to software rendering. KMS card*
    # nodes and /dev/nvidia* chardevs are intentionally NOT passed.
    for node in _host_render_nodes():
        devices[f"dri-{node.name}"] = {
            "type": "unix-char",
            "source": str(node),
            "path": str(node),
            "mode": "0666",
        }

    # Pass declared host_devices (config `host_devices:`) as unix-char /
    # unix-block devices — the same mechanism as the render nodes above,
    # but user-driven. A device whose host source is absent is skipped
    # (validate_runtime surfaces an advisory); this keeps a team-shared
    # config working on hosts that lack the device (e.g. no /dev/kvm).
    for dev in cfg.host_devices:
        src = dev.effective_source
        if not Path(src).exists():
            continue
        entry: dict[str, str] = {"type": dev.type, "source": src, "path": dev.path}
        if dev.mode:
            entry["mode"] = dev.mode
        if dev.gid is not None:
            entry["gid"] = str(dev.gid)
        if dev.uid is not None:
            entry["uid"] = str(dev.uid)
        devices[_hostdev_device_name(dev.path)] = entry

    profile_config: dict[str, str] = {
        "security.nesting": "true",
        "security.syscalls.intercept.mknod": "true",
        "raw.idmap": f"uid {uid} {uid}\ngid {gid} {gid}",
        "environment.WAYLAND_DISPLAY": "wayland-0",
        "environment.XDG_RUNTIME_DIR": runtime,
        "environment.DISPLAY": ":0",
    }
    if cfg.gpg.enabled:
        profile_config["environment.SSH_AUTH_SOCK"] = f"{runtime}/gnupg/S.gpg-agent.ssh"
    if cfg.claude.enabled:
        # Belt-and-suspenders for `/etc/profile.d/jailbee-env.sh`: Incus
        # injects `environment.*` into every `incus exec`, login shell or
        # not, so this also covers `claude` launched from a non-login shell
        # jailbee didn't spawn itself (a JetBrains IDE's integrated
        # terminal, a Claude Code IDE extension). Also reaches existing
        # containers via `jailbee apply` with no image rebuild required.
        key, value = claude_config_dir_env(cfg)
        profile_config[key] = value
        # The credential directory has no `profile.d` half at all: its value is
        # per-repo, so the golden image cannot carry it. The profile route
        # covers every way into the container that jailbee itself opens.
        creds_env = claude_securestorage_dir_env(cfg)
        if creds_env is not None:
            profile_config[creds_env[0]] = creds_env[1]

    # Repo-defined env vars, applied last so they can override the
    # GUI/SSH defaults above when the user really means to (e.g. point
    # SSH_AUTH_SOCK at a different agent).
    for key, value in cfg.container.env.items():
        profile_config[f"environment.{key}"] = value

    # CLAUDE_SECURESTORAGE_CONFIG_DIR is special-cased: unlike DISPLAY or
    # SSH_AUTH_SOCK, an empty value here is NOT equivalent to unset — Claude
    # Code falls back to `~/.claude` for it, silently sending credential
    # lookup back into the per-repo config home. The loop above can (re-)set
    # it to "" via a `container.env` override even when
    # `claude_securestorage_dir_env` already decided to omit the key, so drop
    # it here if it ended up empty. Scoped to this one key on purpose: other
    # env vars keep their existing "repo override always wins, even empty"
    # behaviour.
    if not profile_config.get("environment.CLAUDE_SECURESTORAGE_CONFIG_DIR"):
        profile_config.pop("environment.CLAUDE_SECURESTORAGE_CONFIG_DIR", None)

    profile = {
        "name": profile_names(cfg).base,
        "description": "GPU, security flags, env vars for jailbee containers",
        "config": profile_config,
        "devices": devices,
    }
    return yaml.safe_dump(profile, sort_keys=False)


def _device_name_from_path(path: Path) -> str:
    """Derive a stable device name from a host path. e.g. ~/.gnupg → 'gnupg'."""
    name = str(path).rstrip("/").rsplit("/", 1)[-1]
    return name.lstrip(".").replace(".", "-").replace("_", "-")


def _hostdev_device_name(path: str) -> str:
    """Collision-free Incus device name for a host_devices entry.

    Derived from the full in-container path so paths sharing a final
    segment (e.g. /dev/bus/usb/001/004 and .../002/004) don't collide.
    """
    return "hostdev-" + path.strip("/").replace("/", "-").replace("_", "-")


def container_repo_dir_for(cfg: Config) -> str:
    """In-container repo path for host-side decisions (profile generation,
    mount-routing). Mirrors ``lifecycle.container_repo_dir`` for new
    containers; pre-feature containers may use a different per-container
    path persisted in the ``user.jailbee.repo_dir`` Incus label.

    Duplicated here so ``profiles`` does not depend on ``lifecycle``.
    """
    return f"/home/{CONTAINER_USERNAME}/{cfg.container_prefix}"


def is_under_repo(container_path: str, cfg: Config) -> bool:
    """True if ``container_path`` is strictly under the container's repo dir.

    Mounts that land here can't be attached via the shared ``<prefix>-binds``
    profile, because Incus pre-creates the mount target at ``incus start``
    and that pre-populates the clone destination — making
    ``git clone /mnt/host-source /home/<user>/<repo>`` fail with
    "destination path already exists and is not an empty directory".
    The caller attaches these as per-container devices *after* clone.
    """
    norm = posixpath.normpath(container_path)
    repo_dir = container_repo_dir_for(cfg)
    return norm.startswith(repo_dir + "/")


def _host_has_timezone_file() -> bool:
    return Path("/etc/timezone").exists()


def _host_tmux_paths() -> list[tuple[str, Path, str]]:
    """Existing host tmux config/plugin paths to RO-bind into the container.

    Returns (device_name, host_path, container_subpath) triples for each
    path that exists on the host. ``container_subpath`` is appended to
    ``/home/<dev>`` by the caller.

    Only ``~/.tmux/plugins`` is mounted (not the whole ``~/.tmux``) so
    plugin data dirs like ``~/.tmux/resurrect`` can still be written in
    the container.
    """
    home = Path.home()
    candidates: list[tuple[str, Path, str]] = [
        ("host-tmux-conf", home / ".tmux.conf", ".tmux.conf"),
        ("host-tmux-plugins", home / ".tmux" / "plugins", ".tmux/plugins"),
        ("host-tmux-xdg", home / ".config" / "tmux", ".config/tmux"),
    ]
    return [c for c in candidates if c[1].exists()]


def binds_profile_yaml(cfg: Config) -> str:
    """Generate <repo>-binds profile YAML.

    Includes host bind mounts (RO), the source repo bind (RO, used as
    git-clone source), and shared cache/config bind mounts (RW, between
    containers but not with host).
    """
    devices: dict[str, dict[str, str]] = {}

    # Host bind mounts from config — manual entries plus auto-additions
    # driven by gpg / jetbrains blocks (see Config.effective_host_mounts).
    # Under-repo mounts (container path under /home/<user>/<repo>/) are
    # skipped: see ``is_under_repo`` for why. ``lifecycle.new_container``
    # attaches them per-container after the clone.
    for mount in cfg.effective_host_mounts():
        if is_under_repo(mount.container, cfg):
            continue
        device_name = "host-" + _device_name_from_path(mount.host)
        device: dict[str, str] = {
            "type": "disk",
            "source": str(mount.host),
            "path": mount.container,
        }
        if mount.readonly:
            device["readonly"] = "true"
        devices[device_name] = device

    # Track host's timezone so log timestamps and IDE clocks match the
    # host. /etc/localtime exists on every modern Linux;
    # /etc/timezone is Debian/Ubuntu-specific so we add it only when
    # present on the host.
    devices["host-localtime"] = {
        "type": "disk",
        "source": "/etc/localtime",
        "path": "/etc/localtime",
        "readonly": "true",
    }
    if _host_has_timezone_file():
        devices["host-timezone"] = {
            "type": "disk",
            "source": "/etc/timezone",
            "path": "/etc/timezone",
            "readonly": "true",
        }

    # Shared dir bind mounts — driven by cfg.effective_shared_caches()
    # (user's shared_caches + claude/jetbrains auto-adds). Under-repo
    # entries (container path under /home/<user>/<repo>/) are skipped
    # for the same reason as host_mounts: profile-level disks get
    # pre-created at `incus start` which breaks `git clone`. The caller
    # attaches those as per-container devices after the clone — see
    # lifecycle._attach_under_repo_shared_caches.
    home = f"/home/{CONTAINER_USERNAME}"
    shared = str(cfg.shared_dir)

    for cache in cfg.effective_shared_caches():
        container_path = (
            cache.container_path.replace("~", home, 1)
            if cache.container_path.startswith("~")
            else cache.container_path
        )
        if is_under_repo(container_path, cfg):
            continue
        devices[f"shared-{cache.name}"] = {
            "type": "disk",
            "source": f"{shared}/{cache.host_subpath}",
            "path": container_path,
        }

    # Shared Claude credential — one directory, several repos. Rendered by its
    # own branch rather than through `effective_shared_caches()` because its
    # source is outside `shared_dir`; named without the `shared-` prefix for
    # the same reason, since that prefix means "derived from a SharedCache"
    # everywhere else in this profile.
    if cfg.claude.enabled and cfg.claude_credentials_dir is not None:
        devices[CLAUDE_CREDS_DEVICE] = {
            "type": "disk",
            "source": str(cfg.claude_credentials_dir),
            "path": f"{home}/{CLAUDE_CREDS_DIRNAME}",
        }

    # Auto-detect tmux config/plugins on host and RO-bind into the dev
    # user's home. Only paths that exist on the host are
    # mounted, so hosts without tmux config get the container's default.
    for device_name, host_path, container_subpath in _host_tmux_paths():
        devices[device_name] = {
            "type": "disk",
            "source": str(host_path),
            "path": f"{home}/{container_subpath}",
            "readonly": "true",
        }

    profile = {
        "name": profile_names(cfg).binds,
        "description": "Host RO mounts and shared RW mounts for jailbee containers",
        "config": {},
        "devices": devices,
    }
    return yaml.safe_dump(profile, sort_keys=False)


def loose_net_profile_yaml(prefix: str) -> str:
    """Generate `<prefix>-net-loose` profile YAML from the prefix alone.

    Split out of `net_profile_yaml` because the loose profile's content is
    fully determined by the container prefix — no other Config field enters
    it. The caller that needed a config-free path here was `jailbee migrate`,
    removed in 1.1.0; the split stays because it keeps that independence
    checkable, and `net_profile_yaml(cfg, "loose")` delegates here, so the
    two cannot drift.
    """
    profile = {
        "name": f"{prefix}{LOOSE_PROFILE_SUFFIX}",
        "description": NET_DESCRIPTIONS["loose"],
        "config": {},
        "devices": {
            "eth0": {
                "type": "nic",
                # Dedicated bridge with no ACL attached.
                # `jailbee init` creates it via ensure_loose_bridge().
                "network": LOOSE_BRIDGE,
            }
        },
    }
    return yaml.safe_dump(profile, sort_keys=False)


def net_profile_yaml(cfg: Config, mode: str) -> str:
    """Generate <repo>-net-{strict,loose} profile YAML."""
    if mode == "loose":
        return loose_net_profile_yaml(cfg.container_prefix)
    if mode != "strict":
        raise ValueError(f"Unknown network mode: {mode}")

    eth0: dict[str, str] = {
        "type": "nic",
        "network": "incusbr0",
        "security.acls": acl_name(cfg),
    }
    names = profile_names(cfg)
    profile = {
        "name": names.net_by_mode[mode],
        "description": NET_DESCRIPTIONS[mode],
        "config": {},
        "devices": {"eth0": eth0},
    }
    return yaml.safe_dump(profile, sort_keys=False)
