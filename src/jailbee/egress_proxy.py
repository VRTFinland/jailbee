"""Squid egress proxy — an Incus container that filters outbound HTTP(S) by domain.

One ``jailbee-egress-proxy`` container sits on ``jailbee-loose`` (no bridge ACL,
so its own upstream traffic is unfiltered) and carries one extra NIC per client
bridge (``incusbr0`` and, when it exists, ``jailbee-work``). Clients reach it on
its address on their own bridge, port 3128. The client NICs get an address and
**no route**, so upstream traffic can only leave through ``eth0``.

The container is created and repaired only by ``proxy_up``; the rule-push
helpers never provision anything.
"""

from __future__ import annotations

import ipaddress
import posixpath
import shlex
import time
from enum import StrEnum
from importlib import resources
from typing import TYPE_CHECKING, Any

import yaml
from sqlalchemy.exc import SQLAlchemyError

from jailbee import tui
from jailbee.bridge_ipv4 import free_ipv4
from jailbee.egress import is_wildcard_entry
from jailbee.egress_proxy_render import (
    BASE_SQUID_CONF,
    FRAGMENT_DIR,
    PROXY_PORT,
    ProxyScope,
    proxy_env,
    render_fragment,
)
from jailbee.incus import Incus, IncusError
from jailbee.loose_bridge import loose_bridge_host_ip
from jailbee.network_generation import WORK_BRIDGE
from jailbee.services_acl import EGRESS_PROXY_LABEL, other_service_ips, set_service
from jailbee.work_network import work_network_lock

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlmodel import Session

    from jailbee.config import Config

PROXY_CONTAINER = "jailbee-egress-proxy"
PROXY_PROFILE = "jailbee-egress-proxy-profile"
CLIENT_BRIDGES: tuple[str, ...] = ("incusbr0", WORK_BRIDGE)

_PROXY_BRIDGE = "jailbee-loose"
_PROXY_IMAGE = "images:ubuntu/26.04/cloud"
_SERVICE = "squid"
_SERVICE_WAIT_SECONDS = 60
_BOOT_WAIT_SECONDS = 60
_PROVISION_PKG = "jailbee.provision"
_PROVISION_SUBDIR = "egress-proxy"
_NETPLAN_PATH = "/etc/netplan/60-jailbee-egress-proxy.yaml"
_DEVICE_PREFIX = "cl-"

PROXY_ENV_KEYS: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
)


def _no_steps(_message: str) -> None:
    """Default `on_step`: report nowhere."""


class ProxyStatus(StrEnum):
    """Reported state of the proxy container and its Squid service."""

    RUNNING = "running"
    DEGRADED = "degraded"
    STOPPED = "stopped"
    MISSING = "missing"


def _service_state(incus: Incus) -> str:
    try:
        out = incus.exec(PROXY_CONTAINER, ["systemctl", "is-active", _SERVICE], timeout=10)
    except IncusError as e:
        return str(e)
    return out.strip()


def _container_present(incus: Incus) -> dict[str, Any] | None:
    for c in incus.list_containers():
        if c.get("name") == PROXY_CONTAINER:
            return c
    return None


def proxy_status(incus: Incus) -> ProxyStatus:
    """``running`` needs the container Running and ``squid`` active inside it."""
    info = _container_present(incus)
    if info is None:
        return ProxyStatus.MISSING
    if info.get("status") != "Running":
        return ProxyStatus.STOPPED
    if _service_state(incus) == "active":
        return ProxyStatus.RUNNING
    return ProxyStatus.DEGRADED


def _device_name(bridge: str) -> str:
    return _DEVICE_PREFIX + bridge.removeprefix("jailbee-")


def _nic_name(bridge: str) -> str:
    return f"eth{CLIENT_BRIDGES.index(bridge) + 1}"


def _client_devices(info: dict[str, Any] | None) -> dict[str, str]:
    """``bridge -> address`` of the client NICs in a raw ``incus list`` entry."""
    found: dict[str, str] = {}
    devices = (info or {}).get("devices") or {}
    for bridge in CLIENT_BRIDGES:
        device = devices.get(_device_name(bridge))
        if isinstance(device, dict) and isinstance(device.get("ipv4.address"), str):
            found[bridge] = device["ipv4.address"]
    return found


def client_endpoints(incus: Incus) -> dict[str, str]:
    """Bridge -> the proxy's address on that bridge."""
    return _client_devices(_container_present(incus))


def endpoint_for_bridge(incus: Incus, bridge: str) -> str | None:
    return client_endpoints(incus).get(bridge)


def _read_provision_text(filename: str) -> str:
    return (
        resources.files(_PROVISION_PKG).joinpath(_PROVISION_SUBDIR).joinpath(filename).read_text()
    )


def _ensure_profile(incus: Incus) -> None:
    """Create or refresh the proxy's profile.

    ``security.nesting`` is required for the same reason as on the registry
    mirror: on hosts with ``kernel.apparmor_restrict_unprivileged_userns=1``
    systemd 256+ in the container hangs at ``(sd-mkuserns)``, so the boot
    never finishes and networkd never asks for a DHCPv4 lease.
    """
    if not incus.network_exists(_PROXY_BRIDGE):
        incus.network_create(_PROXY_BRIDGE)
    if not incus.profile_exists(PROXY_PROFILE):
        incus.profile_create(PROXY_PROFILE)
    eth0: dict[str, str] = {"type": "nic", "name": "eth0", "network": _PROXY_BRIDGE}
    address = loose_bridge_host_ip(incus, 2)
    if address is not None:
        eth0["ipv4.address"] = address
    profile = {
        "name": PROXY_PROFILE,
        "description": "network for the jailbee-egress-proxy container",
        "config": {"security.nesting": "true"},
        "devices": {"eth0": eth0},
    }
    incus.profile_set_yaml(PROXY_PROFILE, yaml.safe_dump(profile, sort_keys=False))


def _create(incus: Incus) -> None:
    incus.init(_PROXY_IMAGE, PROXY_CONTAINER)
    incus.profile_assign(PROXY_CONTAINER, ["default", PROXY_PROFILE])
    incus.config_set(PROXY_CONTAINER, "boot.autostart", "true")
    incus.start(PROXY_CONTAINER)


def _ensure_client_nics(incus: Incus) -> dict[str, str]:
    """Add any missing client NIC; return ``bridge -> address`` for all present."""
    have = _client_devices(_container_present(incus))
    for bridge in CLIENT_BRIDGES:
        if bridge in have or not incus.network_exists(bridge):
            continue
        props = {"type": "nic", "network": bridge, "name": _nic_name(bridge)}
        if bridge == WORK_BRIDGE:
            with work_network_lock():
                have[bridge] = _add_nic(incus, bridge, props)
        else:
            have[bridge] = _add_nic(incus, bridge, props)
    return have


def _add_nic(incus: Incus, bridge: str, props: dict[str, str]) -> str:
    address = free_ipv4(incus, bridge)
    incus.config_device_add(
        PROXY_CONTAINER,
        _device_name(bridge),
        "nic",
        {**props, "ipv4.address": address, "security.ipv4_filtering": "true"},
    )
    return address


def _wait_for_boot(incus: Incus, on_step: Callable[[str], None]) -> None:
    """Block until the container's systemd has finished booting.

    ``netplan apply`` talks to systemd over D-Bus, which does not exist for the
    first seconds after ``incus start``. ``running`` and ``degraded`` both mean
    boot is over (``is-system-running`` exits non-zero for ``degraded``).
    """
    deadline = time.monotonic() + _BOOT_WAIT_SECONDS
    while True:
        try:
            state = incus.exec(
                PROXY_CONTAINER, ["systemctl", "is-system-running"], timeout=10
            ).strip()
        except IncusError as e:
            state = "degraded" if "degraded" in str(e) else str(e)
        if state in ("running", "degraded"):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"{PROXY_CONTAINER} did not finish booting within {_BOOT_WAIT_SECONDS}s "
                f"(last state: {state}). Run `jailbee apply` to retry."
            )
        on_step(f"waiting for {PROXY_CONTAINER} to boot")
        time.sleep(2)


def _write_netplan(incus: Incus, addresses: dict[str, str]) -> None:
    """Static, route-less client NICs; only ``eth0`` (dhcp4) gets a default route."""
    ethernets: dict[str, Any] = {"eth0": {"dhcp4": True}}
    for bridge, address in addresses.items():
        prefix = ipaddress.IPv4Interface(
            incus.network_get(bridge, "ipv4.address")
        ).network.prefixlen
        ethernets[_nic_name(bridge)] = {"dhcp4": False, "addresses": [f"{address}/{prefix}"]}
    body = yaml.safe_dump({"network": {"version": 2, "ethernets": ethernets}}, sort_keys=False)
    # Write beside the live file, apply only when the content differs, and put the
    # previous file back when `netplan apply` fails so the next run retries.
    script = f"""\
set -euo pipefail
live={_NETPLAN_PATH}
cat > "$live.new" <<'JAILBEE_NETPLAN_EOF'
{body.rstrip()}
JAILBEE_NETPLAN_EOF
if [ -f "$live" ] && cmp -s "$live.new" "$live"; then
  rm -f "$live.new"
  echo UNCHANGED
  exit 0
fi
chmod 0600 "$live.new"
if [ -f "$live" ]; then mv -f "$live" "$live.bak"; fi
mv -f "$live.new" "$live"
if ! netplan apply; then
  rm -f "$live"
  if [ -f "$live.bak" ]; then mv -f "$live.bak" "$live"; fi
  exit 1
fi
rm -f "$live.bak"
echo APPLIED
"""
    incus.exec_with_input(PROXY_CONTAINER, ["bash", "-s"], script, timeout=120)


def _provision(incus: Incus) -> None:
    install_body = _read_provision_text("install.sh")
    script = f"""\
{install_body.rstrip()}
cat > /etc/squid/squid.conf <<'JAILBEE_SQUID_EOF'
{BASE_SQUID_CONF.rstrip()}
JAILBEE_SQUID_EOF
squid -k parse
systemctl enable --now squid
systemctl restart squid
"""
    incus.exec_with_input(PROXY_CONTAINER, ["bash", "-s"], script, timeout=600)


def _squid_installed(incus: Incus) -> bool:
    try:
        incus.exec(PROXY_CONTAINER, ["test", "-x", "/usr/sbin/squid"], timeout=15)
    except IncusError:
        return False
    return True


def _wait_for_service(incus: Incus, on_step: Callable[[str], None]) -> str | None:
    deadline = time.monotonic() + _SERVICE_WAIT_SECONDS
    while True:
        state = _service_state(incus)
        if state == "active":
            return None
        now = time.monotonic()
        if now >= deadline:
            return f"{_SERVICE} did not become active within {_SERVICE_WAIT_SECONDS}s ({state})"
        on_step(f"waiting for {_SERVICE} - {state}, {int(deadline - now)}s left")
        time.sleep(2)


def _ensure_service(incus: Incus, *, provisioned: bool, on_step: Callable[[str], None]) -> None:
    """Wait for squid; on failure reinstall once (unless just provisioned), then raise."""
    reason = _wait_for_service(incus, on_step)
    if reason is None:
        return
    if provisioned:
        raise RuntimeError(_service_failure(reason))
    on_step("squid did not come up; reinstalling it once")
    try:
        _provision(incus)
    except IncusError as e:
        reason = f"{reason}; reinstalling failed: {e}"
    else:
        second = _wait_for_service(incus, on_step)
        if second is None:
            return
        reason = f"reinstalled once; {second}"
    raise RuntimeError(_service_failure(reason))


def _service_failure(reason: str) -> str:
    return f"{reason}. Run `jailbee apply` to retry, or delete {PROXY_CONTAINER} and apply again."


def proxy_up(incus: Incus, *, on_step: Callable[[str], None] = _no_steps) -> None:
    """Bring the proxy container up. Idempotent; repairs in place."""
    on_step("preparing the proxy profile")
    _ensure_profile(incus)

    info = _container_present(incus)
    provisioned = False
    if info is None:
        on_step(f"creating {PROXY_CONTAINER} from {_PROXY_IMAGE} (first run downloads it)")
        _create(incus)
    elif info.get("status") != "Running":
        on_step(f"starting {PROXY_CONTAINER}")
        incus.start(PROXY_CONTAINER)

    on_step("attaching the client networks")
    addresses = _ensure_client_nics(incus)
    _wait_for_boot(incus, on_step)
    _write_netplan(incus, addresses)

    if not _squid_installed(incus):
        on_step("installing squid in the container (apt, up to 10 min)")
        _provision(incus)
        provisioned = True

    _ensure_service(incus, provisioned=provisioned, on_step=on_step)
    set_service(incus, EGRESS_PROXY_LABEL, (sorted(client_endpoints(incus).values()), [PROXY_PORT]))


def proxy_up_or_warn(incus: Incus, on_step: Callable[[str], None] | None = None) -> bool:
    """``proxy_up`` for callers that have more to finish: a failure is only a warning.

    Returns ``False`` on failure so a caller whose own outcome depends on the
    proxy can withhold its success line. The reason is printed with
    ``warn_plain`` because it can embed square brackets (a failed command's
    argv), which ``warn`` would read as markup.
    """
    try:
        proxy_up(incus, on_step=on_step or (lambda message: tui.info(f"  {message}")))
    except (IncusError, RuntimeError, ValueError) as e:
        tui.warn_plain(f"Could not start the egress proxy: {e}")
        return False
    return True


def _run_fragment_script(incus: Incus, script: str) -> str:
    """Run a fragment script in the proxy; return its last stdout line (the marker).

    A parse failure becomes ``RuntimeError``. Only the last line counts, so stray
    squid output before the marker cannot flip the verdict.
    """
    try:
        out = incus.exec_with_input(PROXY_CONTAINER, ["bash", "-s"], script, timeout=60)
    except IncusError as e:
        raise RuntimeError(f"squid rejected the egress rules: {e}") from e
    lines = out.strip().splitlines()
    return lines[-1].strip() if lines else ""


def _locked_preamble(live: str) -> str:
    """Shell prologue: serialise fragment writers (timer, apply, background ``new``).

    The lock file sits beside the fragment directory so no ``*.conf`` glob sees it.
    Without ``flock`` (not expected in the Ubuntu image) the script runs unlocked.
    """
    lock = shlex.quote(f"{posixpath.dirname(FRAGMENT_DIR)}/.jailbee-fragments.lock")
    return f"""\
set -euo pipefail
live={live}
if command -v flock >/dev/null 2>&1; then
  exec 9>{lock}
  flock 9
fi
"""


def push_fragment(incus: Incus, prefix: str, text: str) -> bool:
    """Install one repo's rule fragment; ``True`` if it changed and squid reloaded.

    The new text is parsed before it replaces the live file and the old file is
    restored on failure, so a bad fragment never reaches the running Squid.
    Returns ``False`` without touching Incus beyond the status probe when the
    proxy is not running: the periodic caller must never provision. Concurrent
    writers are serialised with ``flock`` and each stages into its own temp file.
    """
    if proxy_status(incus) != ProxyStatus.RUNNING:
        return False
    live = shlex.quote(f"{FRAGMENT_DIR}/{prefix}.conf")
    script = (
        _locked_preamble(live)
        + f"""\
new=$(mktemp "$live.XXXXXX")
trap 'rm -f "$new"' EXIT
cat > "$new" <<'JAILBEE_FRAGMENT_EOF'
{text.rstrip()}
JAILBEE_FRAGMENT_EOF
if [ -f "$live" ] && cmp -s "$new" "$live"; then
  echo UNCHANGED
  exit 0
fi
chmod 0644 "$new"
if [ -f "$live" ]; then mv -f "$live" "$live.bak"; fi
mv -f "$new" "$live"
if ! squid -k parse; then
  rm -f "$live"
  if [ -f "$live.bak" ]; then mv -f "$live.bak" "$live"; fi
  echo PARSE_FAILED
  exit 1
fi
squid -k reconfigure
rm -f "$live.bak"
echo RECONFIGURED
"""
    )
    return _run_fragment_script(incus, script) == "RECONFIGURED"


def drop_fragment(incus: Incus, prefix: str) -> bool:
    """Remove one repo's fragment and reconfigure if it existed."""
    if proxy_status(incus) != ProxyStatus.RUNNING:
        return False
    live = shlex.quote(f"{FRAGMENT_DIR}/{prefix}.conf")
    script = (
        _locked_preamble(live)
        + """\
if [ ! -f "$live" ]; then
  echo UNCHANGED
  exit 0
fi
rm -f "$live" "$live.bak"
squid -k reconfigure
echo RECONFIGURED
"""
    )
    return _run_fragment_script(incus, script) == "RECONFIGURED"


# ---- per-repo rule collection and per-container environment ----------------


def container_wants_proxy(repo_entries: list[str], extras: list[str], mode: str | None) -> bool:
    """A strict container with at least one wildcard entry needs the proxy."""
    return mode == "strict" and any(is_wildcard_entry(e) for e in [*repo_entries, *extras])


def _eth0_device(raw: dict[str, Any]) -> dict[str, Any]:
    """The container's effective ``eth0`` (local device over profile-merged)."""
    local = (raw.get("devices") or {}).get("eth0")
    expanded = (raw.get("expanded_devices") or {}).get("eth0")
    merged: dict[str, Any] = {}
    for device in (expanded, local):
        if isinstance(device, dict):
            merged.update(device)
    return merged


def _source_ipv4(cfg: Config, raw: dict[str, Any]) -> str | None:
    """Work containers: the reserved eth0 address. Legacy: the live lease."""
    from jailbee.network_generation import generation_of
    from jailbee.registry import eth0_global_ipv4

    if generation_of(cfg, raw) == "work":
        address = (raw.get("devices") or {}).get("eth0", {}).get("ipv4.address")
        return address if isinstance(address, str) else None
    return eth0_global_ipv4(raw)


def collect_scopes(
    cfg: Config, incus: Incus, session: Session, raws: list[dict[str, Any]] | None = None
) -> list[ProxyScope]:
    """The repo scope first, then one scope per work container with its own wildcard extras."""
    from jailbee.egress_scope import container_extras, effective_repo_entries
    from jailbee.lifecycle import list_containers
    from jailbee.network_generation import generation_of

    repo_entries = effective_repo_entries(cfg, session)
    raw_by_name = {r["name"]: r for r in (raws if raws is not None else incus.list_containers())}
    repo_sources: list[str] = []
    container_scopes: list[ProxyScope] = []
    for info in list_containers(cfg, incus):
        raw = raw_by_name.get(info.name)
        if raw is None or info.state != "Running":
            continue
        extras = container_extras(incus, info.name)
        if not container_wants_proxy(repo_entries, extras, info.network):
            continue
        ip = _source_ipv4(cfg, raw)
        if ip is None:
            continue  # no address yet; the next sync picks it up
        repo_sources.append(ip)
        if extras and generation_of(cfg, raw) == "work":
            container_scopes.append(ProxyScope(info.name, (ip,), tuple(extras), kind="c"))
    repo_scope = ProxyScope(cfg.container_prefix, tuple(repo_sources), tuple(repo_entries))
    return [repo_scope, *container_scopes]


def sync_repo_rules(cfg: Config, incus: Incus, session: Session) -> bool:
    """Push this repo's fragment, or drop it when no container needs the proxy.

    One ``incus list`` decides first whether there is a running proxy at all.
    Without one, push and drop are no-ops, so nothing else is read: a host that
    never used a wildcard pays for a single list per call.
    """
    raws = incus.list_containers()
    if not any(r.get("name") == PROXY_CONTAINER and r.get("status") == "Running" for r in raws):
        return False
    prefix = cfg.container_prefix
    scopes = collect_scopes(cfg, incus, session, raws)
    if not any(scope.sources for scope in scopes):
        return drop_fragment(incus, prefix)
    return push_fragment(incus, prefix, render_fragment(prefix, scopes))


def _current_env(incus: Incus, name: str, raw: dict[str, Any] | None) -> dict[str, str | None]:
    """The container's proxy environment keys, from its listed config when possible."""
    config = raw.get("config") if raw is not None else None
    if isinstance(config, dict):
        return {k: config.get(f"environment.{k}") or None for k in PROXY_ENV_KEYS}
    return {k: incus.config_get(name, f"environment.{k}") or None for k in PROXY_ENV_KEYS}


def sync_container_env(
    cfg: Config,
    incus: Incus,
    session: Session,
    name: str,
    mode: str | None,
    *,
    raws: list[dict[str, Any]] | None = None,
) -> None:
    """Point one container's proxy environment at its bridge's proxy, or clear it.

    ``raws`` is an ``incus list`` the caller already holds; with it this reads
    the container, the proxy's addresses and the current variables from that
    snapshot instead of asking Incus again.
    """
    from jailbee.egress_scope import container_extras, effective_repo_entries

    entries = [*effective_repo_entries(cfg, session), *container_extras(incus, name)]
    raw = None
    wanted: dict[str, str] = {}
    if container_wants_proxy(entries, [], mode):
        listed = raws if raws is not None else incus.list_containers()
        raw = next((r for r in listed if r.get("name") == name), {})
        bridge = _eth0_device(raw).get("network")
        if not isinstance(bridge, str):
            tui.warn(f"cannot find the eth0 network of {name}; egress proxy env cleared")
            endpoint = None
        else:
            proxy = next((r for r in listed if r.get("name") == PROXY_CONTAINER), None)
            endpoint = _client_devices(proxy).get(bridge)
            if endpoint is None:
                tui.warn(
                    "egress proxy is not running; wildcard egress entries are unavailable "
                    "— run `jailbee apply`"
                )
        if endpoint is not None:
            wanted = proxy_env(endpoint, entries, other_service_ips(incus, EGRESS_PROXY_LABEL))
    elif raws is not None:
        raw = next((r for r in raws if r.get("name") == name), None)
    for key, current in _current_env(incus, name, raw).items():
        value = wanted.get(key)
        if value is None:
            if current:
                incus.config_unset(name, f"environment.{key}")
        elif current != value:
            incus.config_set(name, f"environment.{key}", value)


_SYNC_ERRORS = (IncusError, RuntimeError, ValueError, SQLAlchemyError)


def sync_container_env_only(
    cfg: Config,
    incus: Incus,
    name: str,
    mode: str | None,
    *,
    raws: list[dict[str, Any]] | None = None,
) -> None:
    """Refresh one container's proxy environment alone. Never raises.

    For loops over many containers (``jailbee apply``): the repo's rules are
    pushed once afterwards with ``sync_repo``, not once per container.
    """
    from sqlmodel import Session

    from jailbee.db import get_engine

    try:
        with Session(get_engine()) as session:
            sync_container_env(cfg, incus, session, name, mode, raws=raws)
    except _SYNC_ERRORS as e:
        tui.warn_plain(f"egress proxy environment for {name} failed: {e}")


def sync_repo(cfg: Config, incus: Incus) -> None:
    """Refresh this repo's Squid rules alone. Never raises."""
    from sqlmodel import Session

    from jailbee.db import get_engine

    try:
        with Session(get_engine()) as session:
            sync_repo_rules(cfg, incus, session)
    except _SYNC_ERRORS as e:
        tui.warn_plain(f"egress proxy rules for {cfg.container_prefix} failed: {e}")


def sync_container(cfg: Config, incus: Incus, name: str, mode: str | None) -> None:
    """Refresh one container's proxy env and its repo's rules. Never raises.

    Rules first, then the environment: a container that is told to use the proxy
    must find its source address already allowed, or its first requests get a 403.
    """
    from sqlmodel import Session

    from jailbee.db import get_engine

    try:
        with Session(get_engine()) as session:
            sync_repo_rules(cfg, incus, session)
            sync_container_env(cfg, incus, session, name, mode)
    except _SYNC_ERRORS as e:
        tui.warn_plain(f"egress proxy sync for {name} failed: {e}")
