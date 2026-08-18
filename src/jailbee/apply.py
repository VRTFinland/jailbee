"""`jailbee apply` — re-apply config to profiles, ACL, and live container state.

Orchestrates the steps that take a repo from "config edited" to "every
container reflects the new config", or makes clear which manual step is
needed. Replaces `jailbee init --reapply` and `jailbee net refresh`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import yaml
from sqlmodel import Session

from jailbee.config import Config
from jailbee.db import get_engine
from jailbee.egress_pool import refresh_pool, register_repo
from jailbee.global_config import GlobalConfig
from jailbee.incus import Incus
from jailbee.profiles import (
    base_profile_yaml,
    binds_profile_yaml,
    net_profile_yaml,
    profile_names,
)
from jailbee.tui import ConfirmFn, default_confirm

if TYPE_CHECKING:
    from jailbee.lifecycle import ContainerInfo


@dataclass(frozen=True)
class ApplyResult:
    profiles_changed: list[str]
    profiles_unchanged: list[str]
    acl_changed: bool
    hosts_repinned: list[str]
    docker_proxy_reapplied: list[str]
    restarted: list[str]
    restart_failures: list[tuple[str, str]]
    # Containers moved off the removed `<prefix>-net-offline` profile by
    # this run. Empty on every apply after the first one.
    offline_migrated: list[str] = field(default_factory=list)
    # Containers whose port forwards this run added, replaced or removed.
    ports_changed: list[str] = field(default_factory=list)

    @property
    def fully_successful(self) -> bool:
        return not self.restart_failures


def _profile_differs(incus: Incus, name: str, new_yaml: str) -> bool:
    """True if the rendered YAML differs semantically from Incus's stored profile.

    Compares ``config`` and ``devices`` keys as parsed dicts. Ignores any
    other keys Incus surfaces in `profile show` (e.g. project, used_by).
    """
    return _yaml_subset_differs(incus.profile_show(name), new_yaml, keys=("config", "devices"))


def _acl_differs(incus: Incus, name: str, new_yaml: str) -> bool:
    """True if the rendered ACL YAML differs from Incus's stored ACL."""
    return _yaml_subset_differs(
        incus.network_acl_show(name),
        new_yaml,
        keys=("egress", "ingress", "config"),
    )


def _yaml_subset_differs(existing: str, candidate: str, *, keys: tuple[str, ...]) -> bool:
    existing_parsed = yaml.safe_load(existing) or {}
    candidate_parsed = yaml.safe_load(candidate) or {}
    for key in keys:
        if existing_parsed.get(key) != candidate_parsed.get(key):
            return True
    return False


# The `offline` network mode was removed. Containers created before that
# still carry `<prefix>-net-offline`; `jailbee apply` moves them to strict and
# deletes the profile. Spelled as a suffix rather than read from
# `ProfileNames` because the attribute no longer exists.
_STALE_NET_OFFLINE_SUFFIX = "-net-offline"


def _drop_offline_net_profile(cfg: Config, incus: Incus) -> list[str]:
    """Move any container off the removed offline net profile, then delete it.

    Returns the migrated container names.

    Deliberately not via ``switch_network``: that helper requires a
    *recognised* net profile to be attached, and `<prefix>-net-offline` is no
    longer in ``net_by_mode`` — it would raise "has no network profile
    attached — cannot switch" on exactly the containers being migrated.

    ``/etc/hosts`` is not re-pinned here either. The migrated containers then
    report ``network == "strict"``, so ``run_apply``'s running-container sweep
    re-pins them a few lines further down.
    """
    from jailbee.incus import IncusError
    from jailbee.tui import info, warn

    names = profile_names(cfg)
    stale = f"{cfg.container_prefix}{_STALE_NET_OFFLINE_SUFFIX}"

    migrated: list[str] = []
    for raw in incus.list_containers():
        # A container mid-destroy can be reported with "profiles": null,
        # which bypasses a `.get(..., [])` default (key present, value None).
        profiles = raw.get("profiles") or []
        if stale not in profiles:
            continue
        info(f"  Migrating {raw['name']} off {stale} → {names.net_strict}...")
        incus.profile_assign(
            raw["name"],
            [names.net_strict if p == stale else p for p in profiles],
        )
        migrated.append(raw["name"])

    if incus.profile_exists(stale):
        try:
            incus.profile_delete(stale)
            info(f"  Deleted stale profile {stale}")
        except IncusError as e:
            warn(f"Could not delete stale profile {stale}: {e}")

    return migrated


def run_apply(
    cfg: Config,
    incus: Incus,
    gcfg: GlobalConfig,
    *,
    assume_yes: bool = False,
    no_restart: bool = False,
    confirm_fn: ConfirmFn | None = None,
) -> ApplyResult:
    """Apply current config to profiles, ACL, and live container state."""
    from jailbee.lifecycle import short_name
    from jailbee.tui import info

    info("Applying configuration...")

    # Resolve everything that could fail outside Incus before we mutate
    # anything. Either of these raises and apply aborts cleanly with no
    # partial profile / ACL update.
    info("Refreshing egress pool + ACL + /etc/hosts...")
    mirror_endpoint = _compute_mirror_endpoint_or_abort(incus, gcfg)
    mirror_ca_pem = _read_mirror_ca_or_warn(gcfg) if mirror_endpoint else None

    with Session(get_engine()) as session:
        register_repo(session, cfg)
        refresh_result = refresh_pool(
            cfg,
            gcfg,
            incus,
            session,
            now=datetime.now(UTC),
        )

    if refresh_result.status == "dns_error":
        from jailbee.egress import NetworkResolveError

        raise NetworkResolveError(
            "egress refresh",
            Exception(refresh_result.error or "DNS failure"),
        )
    if refresh_result.status == "acl_error":
        from jailbee.incus import IncusError

        raise IncusError(refresh_result.error or "ACL write failed")
    if refresh_result.status == "partial":
        from jailbee.tui import warn

        warn(f"Some hostnames failed to resolve: {refresh_result.error}")

    if refresh_result.added or refresh_result.removed:
        info(f"  Pool: +{len(refresh_result.added)} added, -{len(refresh_result.removed)} evicted")

    # refresh_pool wrote the ACL whenever there was anything to resolve.
    # The added/removed counters are the closest "did anything change"
    # signal we have from the new pipeline.
    acl_changed_flag = bool(refresh_result.added or refresh_result.removed)

    # Ensure shared-dir tree exists. `jailbee init` would have created it,
    # but a user enabling claude/jetbrains after the initial init (or a repo
    # initialised before a given integration's mounts were added) needs the
    # integration subdirs to exist before the binds profile mounts them —
    # otherwise Incus rejects the profile edit/assign with "Missing source
    # path ...". `_ensure_integration_shared_dirs` is the shared source of
    # truth with `jailbee init` so the two can't drift. Idempotent — cheap to re-run.
    from jailbee.init_command import (
        _ensure_integration_shared_dirs,
        _ensure_shared_dirs,
        _ensure_user_shared_dirs,
    )

    assert cfg.shared_dir is not None  # set by load_config
    _ensure_shared_dirs(cfg.shared_dir)
    _ensure_user_shared_dirs(cfg)
    _ensure_integration_shared_dirs(cfg)

    # Refresh jailbee's bundled skills in the shared ~/.claude/skills so existing
    # containers pick up a newer jailbee without recreation. Non-fatal.
    from jailbee.claude_skills import sync_jailbee_skills
    from jailbee.tui import warn

    try:
        sync_jailbee_skills(cfg)
    except Exception as e:  # non-fatal
        warn(f"jailbee-skills sync failed (continuing): {e}")

    names = profile_names(cfg)
    profile_yamls = {
        names.base: base_profile_yaml(cfg),
        names.binds: binds_profile_yaml(cfg),
        names.net_strict: net_profile_yaml(cfg, "strict"),
        names.net_loose: net_profile_yaml(cfg, "loose"),
    }
    offline_migrated = _drop_offline_net_profile(cfg, incus)

    info("Checking profiles...")
    profiles_changed: list[str] = []
    profiles_unchanged: list[str] = []
    for name, new_yaml in profile_yamls.items():
        if _profile_differs(incus, name, new_yaml):
            info(f"  Updating profile {name}...")
            incus.profile_set_yaml(name, new_yaml)
            profiles_changed.append(name)
        else:
            profiles_unchanged.append(name)

    _ensure_acl_attached_to_bridge(cfg, incus)

    # Push the repo's extra upstream registries into the mirror once,
    # before re-applying per-container dockerd proxy. apply_mirror_registries
    # is idempotent and a no-op when the list is empty or already covered.
    if mirror_endpoint is not None and cfg.docker_registry_mirror.extra_registries:
        info("Syncing extra registries into mirror...")
        from jailbee.registry import apply_mirror_registries

        apply_mirror_registries(incus, cfg.docker_registry_mirror.extra_registries)

    info("Listing running containers...")
    hosts_repinned: list[str] = []
    docker_proxy_reapplied: list[str] = []
    mirror_port = mirror_endpoint[1] if mirror_endpoint else None

    running_names: list[str] = []
    ports_changed: list[str] = []
    for ci in _list_containers(cfg, incus):
        # Reconcile forwards first, and for stopped containers too: a proxy
        # device on a stopped container takes effect on its next boot, so
        # skipping it would leave drift that only shows up later. This is
        # unconditional — even an empty `host_ports` must still clean up a
        # stale `port-cfg-*` device left behind after an entry is deleted.
        from jailbee.ports import reconcile_config_ports

        port_result = reconcile_config_ports(cfg, incus, ci.name)
        if port_result.changed:
            info(
                f"  Port forwards on {short_name(cfg, ci.name)}: "
                f"+{len(port_result.added)} ~{len(port_result.replaced)} "
                f"-{len(port_result.removed)}"
            )
            ports_changed.append(ci.name)

        if ci.state != "Running":
            continue
        running_names.append(ci.name)
        short = short_name(cfg, ci.name)
        if cfg.host_devices:
            # Re-ensure dev is in each host_devices group (e.g. kvm). Idempotent;
            # picks up entries added since the container was created. New shells
            # see the group; an already-open `jailbee shell` must be reopened.
            from jailbee.device_groups import ensure_device_groups

            ensure_device_groups(cfg, incus, ci.name)
        if ci.network == "strict":
            info(f"  Re-pinning /etc/hosts on {short}...")
            from jailbee.hosts import apply_hosts

            apply_hosts(cfg, incus, ci.name, mirror_endpoint=mirror_endpoint)
            hosts_repinned.append(ci.name)
        if mirror_endpoint is not None and mirror_ca_pem is not None and mirror_port is not None:
            info(f"  Re-applying dockerd HTTPS_PROXY on {short}...")
            from jailbee.docker_daemon import apply_docker_proxy

            # apply_docker_proxy reads the mirror endpoint via MIRROR_DNS_NAME
            # internally; we only need to pass CA + port.
            apply_docker_proxy(incus, ci.name, mirror_ca_pem, mirror_port)
            docker_proxy_reapplied.append(ci.name)

    restarted: list[str] = []
    restart_failures: list[tuple[str, str]] = []
    should_restart = bool(profiles_changed) and bool(running_names) and not no_restart
    if should_restart and not assume_yes:
        prompt = (
            f"\n{len(running_names)} running container(s) need restart "
            f"to pick up profile changes:\n  "
            f"{', '.join(short_name(cfg, n) for n in running_names)}\nRestart now?"
        )
        fn = confirm_fn or default_confirm
        if not fn(prompt):
            should_restart = False

    if should_restart:
        for name in running_names:
            info(f"  Restarting {short_name(cfg, name)}...")
            try:
                _restart_one(cfg, incus, name, mirror_endpoint=mirror_endpoint)
                restarted.append(name)
            except Exception as e:
                restart_failures.append((name, str(e)))

    return ApplyResult(
        profiles_changed=profiles_changed,
        profiles_unchanged=profiles_unchanged,
        acl_changed=acl_changed_flag,
        hosts_repinned=hosts_repinned,
        docker_proxy_reapplied=docker_proxy_reapplied,
        restarted=restarted,
        restart_failures=restart_failures,
        offline_migrated=offline_migrated,
        ports_changed=ports_changed,
    )


def _apply_acl_with_nft_quirk(incus: Incus, name: str, acl_yaml: str) -> None:
    """Thin delegator to egress_pool._apply_acl_with_nft_quirk.

    Logic was moved to egress_pool so the refresh timer and `jailbee apply`
    share one nft-quirk-aware ACL writer. This wrapper stays for the
    apply.py code path's clarity (and for tests that may want to patch
    on apply rather than egress_pool).
    """
    from jailbee.egress_pool import _apply_acl_with_nft_quirk as impl

    impl(incus, name, acl_yaml)


def _ensure_acl_attached_to_bridge(cfg: Config, incus: Incus) -> None:
    """Idempotent. Wraps ``init_command.ensure_acl_attached_to_bridge``."""
    from jailbee.init_command import ensure_acl_attached_to_bridge

    ensure_acl_attached_to_bridge(cfg, incus)


def _compute_mirror_endpoint_or_abort(incus: Incus, gcfg: GlobalConfig) -> tuple[str, int] | None:
    """Resolve mirror endpoint, return None if disabled. Aborts on ValueError."""
    if not gcfg.docker_registry_mirror.enabled:
        return None
    from jailbee.docker_daemon import compute_mirror_endpoint

    return compute_mirror_endpoint(incus, gcfg)


def _read_mirror_ca_or_warn(gcfg: GlobalConfig) -> str | None:
    """Return CA cert PEM string, or None if not present (with a warning)."""
    from jailbee.tui import warn

    ca_path = gcfg.docker_registry_mirror.data_dir / "ca" / "ca.crt"
    if ca_path.is_file():
        return ca_path.read_text()
    warn(f"Mirror CA cert not found at {ca_path}; skipping per-container docker-proxy reapply.")
    return None


def _list_containers(cfg: Config, incus: Incus) -> list[ContainerInfo]:
    """Wrap ``lifecycle.list_containers`` so tests can patch one symbol."""
    from jailbee.lifecycle import list_containers

    return list_containers(cfg, incus)


def _restart_one(
    cfg: Config,
    incus: Incus,
    name: str,
    *,
    mirror_endpoint: tuple[str, int] | None = None,
) -> None:
    """Restart one container, re-pin /etc/hosts, and run on_start autostart.

    Wrapper called only by ``run_apply``. The GUI auto-launch part of
    ``cli._post_start_actions`` is intentionally skipped — `jailbee apply`
    should not pop up IDE/Chrome on every container — but the autostart
    shell steps (docker daemon, services, etc.) MUST run, otherwise the
    container comes back from restart with no services and is unusable.
    """
    from jailbee.autostart import (
        AutostartTrigger,
        inject_github_token,
        run_autostart,
    )
    from jailbee.hosts import apply_hosts
    from jailbee.lifecycle import (
        container_repo_dir,
        current_network_mode,
        restart_container,
    )

    restart_container(cfg, incus, name)
    if current_network_mode(cfg, incus, name) == "strict":
        apply_hosts(cfg, incus, name, mirror_endpoint=mirror_endpoint)
    repo_dir = container_repo_dir(cfg, incus, name)
    # Re-inject GH_TOKEN (infrastructure, not a user autostart step) so a
    # rotated PAT is picked up on `jailbee apply`.
    inject_github_token(cfg, incus, name, repo_dir, mirror_endpoint=mirror_endpoint)
    run_autostart(
        cfg,
        incus,
        name,
        AutostartTrigger.ON_START,
        repo_dir=repo_dir,
        mirror_endpoint=mirror_endpoint,
    )
