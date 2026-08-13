"""Docker registry mirror — Incus-hosted pull-through cache for all upstreams.

A single ``jailbee-registry-mirror`` Incus container runs podman + rpardini/docker-
registry-proxy, an HTTPS-MITM caching proxy that caches pulls from any registry
(Docker Hub, ECR, GHCR, GCR, Quay, …). User containers reach it on
``jailbee-registry-mirror.incus:3128`` via the dockerd ``HTTPS_PROXY`` env. The
mirror's CA cert is installed inside every user container so the MITM TLS
works.

**Bridge placement.** The mirror runs on the shared ``jailbee-loose`` bridge,
not on ``incusbr0``. ``incusbr0`` carries the per-repo allowlist ACL at
bridge level, which would default-deny the mirror's traffic
to ``archive.ubuntu.com`` (apt install of podman) and to any upstream
registry (docker.io, ghcr.io, …) being proxied. ``jailbee-loose`` has no
bridge-level ACL. Strict-mode user containers reach the
mirror via an /etc/hosts entry for ``jailbee-registry-mirror.incus`` pointing
at its ``jailbee-loose`` IP (see docker_daemon.py).
"""

from __future__ import annotations

import ipaddress
import os
import time
from collections.abc import Iterable
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from jailbee.incus import Incus, IncusError

if TYPE_CHECKING:
    from collections.abc import Callable

    from jailbee.global_config import GlobalConfig


MIRROR_CONTAINER_NAME = "jailbee-registry-mirror"
MIRROR_PROFILE_NAME = "jailbee-registry-mirror-profile"
MIRROR_SERVICE_NAME = "jailbee-registry-proxy.service"
MIRROR_BRIDGE = "jailbee-loose"

_MIRROR_IMAGE = "images:ubuntu/26.04/cloud"
_SERVICE_WAIT_SECONDS = 60
_PROVISION_PKG = "jailbee.provision"
_PROVISION_SUBDIR = "registry-mirror"

# rpardini/docker-registry-proxy:0.6.5 image ENV defaults (verified via
# `podman exec ... env | grep REGISTRIES` on a running mirror). The
# EnvironmentFile= override in jailbee-registry-proxy.container fully replaces
# the image's REGISTRIES — so any per-repo additions must re-include these
# or rpardini silently stops caching k8s.io/gcr.io/quay/ghcr pulls.
_RPARDINI_DEFAULT_REGISTRIES: frozenset[str] = frozenset(
    {"gcr.io", "ghcr.io", "quay.io", "registry.k8s.io"}
)

_PROXY_ENV_FILE = "/etc/jailbee-registry-proxy.env"
_QUADLET_UNIT_PATH = "/etc/containers/systemd/jailbee-registry-proxy.container"


def _no_steps(_message: str) -> None:
    """Default `on_step`: report nowhere.

    `registry_up` takes a callback rather than importing `tui` because this
    module has no business deciding whether its progress is a spinner, a log
    line or nothing at all — the CLI already owns that choice (see
    `console.status` in `cli.py`). A no-op default keeps every call site
    unconditional.
    """


class MirrorStatus(StrEnum):
    """Reported state of the jailbee-registry-mirror container + its inner service."""

    RUNNING = "running"
    STOPPED = "stopped"
    DEGRADED = "degraded"
    MISSING = "missing"


def _service_state(incus: Incus) -> str:
    """Return ``systemctl is-active``'s verdict for the proxy service.

    Gives systemd's word (``active`` / ``inactive`` / ``failed`` /
    ``activating``) when the exec succeeded. ``systemctl is-active`` exits
    non-zero for every non-active state, and a container that is still
    booting may not answer at all — both surface as ``IncusError``, whose
    text is returned so callers can report *why* rather than just "not
    active".
    """
    try:
        out = incus.exec(
            MIRROR_CONTAINER_NAME,
            ["systemctl", "is-active", MIRROR_SERVICE_NAME],
            timeout=10,
        )
    except IncusError as e:
        return str(e)
    return out.strip()


def registry_status(incus: Incus) -> MirrorStatus:
    """Return the mirror's effective status.

    ``running`` requires both: the Incus container is in ``Running`` state
    AND ``systemctl is-active jailbee-registry-proxy.service`` returns ``active``
    inside it.
    """
    for c in incus.list_containers():
        if c.get("name") != MIRROR_CONTAINER_NAME:
            continue
        if c.get("status") != "Running":
            return MirrorStatus.STOPPED
        if _service_state(incus) == "active":
            return MirrorStatus.RUNNING
        return MirrorStatus.DEGRADED
    return MirrorStatus.MISSING


def _ensure_data_dirs(gcfg: GlobalConfig) -> tuple[Path, Path]:
    """Create the two host directories rpardini bind-mounts into."""
    base = gcfg.docker_registry_mirror.data_dir
    cache_dir = base / "cache"
    ca_dir = base / "ca"
    cache_dir.mkdir(parents=True, exist_ok=True)
    ca_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir, ca_dir


def _mirror_profile_yaml(host_uid: int, host_gid: int, mirror_ip: str | None) -> str:
    """YAML for `jailbee-registry-mirror-profile`.

    `security.nesting: true` is required on Ubuntu 24.04+ hosts where
    `kernel.apparmor_restrict_unprivileged_userns=1` blocks the nested
    user namespaces systemd 256+ uses for `DynamicUser=`/`PrivateUsers=`
    services. Without it, journald/networkd/resolved hang at
    `(sd-mkuserns)` inside the mirror container — no DHCPv4 lease, no
    /etc/resolv.conf, install.sh's `apt-get update` fails to resolve
    archive.ubuntu.com. Podman in the mirror also needs nesting to
    create its own container user namespaces.

    `raw.idmap` carves the host user's UID/GID out of the otherwise
    shifted namespace and maps them to container 0 (root). The mirror
    bind-mounts `~/.local/share/jailbee/registry/{cache,ca}` (owned by the
    host user) into `/docker_mirror_cache` and `/ca`; rpardini inside
    runs as container-root and would otherwise hit "Permission denied"
    when writing `/ca/ca.key` (root-in-container maps to host UID
    1_000_000 by default, which can't write host-UID-53023 files).
    Requires `/etc/subuid` + `/etc/subgid` to authorise the delegation
    (`root:<UID>:1`); see docs/installation.md's subuid/subgid section.

    The eth0 device overrides the `default` profile's incusbr0 NIC via
    profile-stacking precedence, putting the mirror on the ACL-free
    `jailbee-loose` bridge so its egress isn't filtered by any per-repo
    allowlist. See module docstring.

    ``mirror_ip`` (when not None) pins the mirror's eth0 to a fixed
    IPv4 via Incus' ``ipv4.address`` device option, which lowers to a
    dnsmasq ``--dhcp-host=`` static reservation. Without this, the
    mirror gets a random DHCP lease on each (re)create and surviving
    lease records can cause loose-mode containers to resolve
    ``jailbee-registry-mirror.incus`` to a pre-reboot IP (DNS query goes
    to jailbee-loose's dnsmasq, which keeps the stale entry alive until
    explicitly cleared). Strict mode side-steps it because jailbee pins
    /etc/hosts from the live container state, not from DNS — but loose
    mode strips that pin (``clear_hosts``) and is therefore at the
    mercy of dnsmasq.
    """
    eth0: dict[str, str] = {
        "type": "nic",
        "name": "eth0",
        "network": MIRROR_BRIDGE,
    }
    if mirror_ip is not None:
        eth0["ipv4.address"] = mirror_ip
    profile = {
        "name": MIRROR_PROFILE_NAME,
        "description": "security + network for the jailbee-registry-mirror container",
        "config": {
            "security.nesting": "true",
            "raw.idmap": f"uid {host_uid} 0\ngid {host_gid} 0",
        },
        "devices": {"eth0": eth0},
    }
    return yaml.safe_dump(profile, sort_keys=False)


def _compute_mirror_static_ip(incus: Incus) -> str | None:
    """Pick a deterministic IPv4 inside ``jailbee-loose``'s subnet for the mirror.

    Reads ``jailbee-loose``'s ``ipv4.address`` (the bridge CIDR) and returns
    the lowest host address that isn't the bridge gateway itself — e.g.
    bridge ``10.79.115.1/24`` → ``10.79.115.2``. Caller injects this
    into the mirror profile so dnsmasq always issues the same lease.

    Returns ``None`` when ``jailbee-loose`` has no concrete IPv4 subnet to
    pick from (``ipv4.address: none`` or ``auto``, or the value is
    empty / unparseable). The caller falls back to plain DHCP — losing
    the stable-IP property but keeping the mirror operational, since
    strict-mode containers still get the correct live IP via the
    /etc/hosts pin jailbee writes from ``incus list`` state.
    """
    raw = incus.network_get(MIRROR_BRIDGE, "ipv4.address")
    if not isinstance(raw, str):
        return None
    cidr = raw.strip()
    if not cidr or cidr in ("none", "auto"):
        return None
    try:
        iface = ipaddress.IPv4Interface(cidr)
    except ValueError:
        return None
    network = iface.network
    bridge_ip = iface.ip
    for candidate in network.hosts():
        if candidate != bridge_ip:
            return str(candidate)
    return None


def _ensure_profile(incus: Incus) -> None:
    if not incus.profile_exists(MIRROR_PROFILE_NAME):
        incus.profile_create(MIRROR_PROFILE_NAME)
    mirror_ip = _compute_mirror_static_ip(incus)
    incus.profile_set_yaml(
        MIRROR_PROFILE_NAME,
        _mirror_profile_yaml(os.getuid(), os.getgid(), mirror_ip),
    )


def _ensure_mirror_bridge(incus: Incus) -> None:
    """Create the shared `jailbee-loose` bridge if missing. Idempotent.

    `jailbee init` also creates this bridge for loose-mode user containers,
    but `jailbee registry up` is repo-config-independent and
    may run before any `jailbee init` on a fresh host — so we re-create it
    here defensively. The bridge is shared across all jailbee-managed
    repos; no per-repo state attaches to it.
    """
    if incus.network_exists(MIRROR_BRIDGE):
        return
    incus.network_create(MIRROR_BRIDGE)


def _container_present(incus: Incus) -> dict[str, Any] | None:
    for c in incus.list_containers():
        if c.get("name") == MIRROR_CONTAINER_NAME:
            return c
    return None


def _read_provision_text(filename: str) -> str:
    """Read a packaged provisioning artifact (install.sh / Quadlet unit)."""
    return (
        resources.files(_PROVISION_PKG).joinpath(_PROVISION_SUBDIR).joinpath(filename).read_text()
    )


def _provision_mirror(incus: Incus) -> None:
    """Push install.sh + Quadlet unit into the container and run install.sh.

    Files are written to ``/root/`` rather than ``/tmp/``: on
    ``images:ubuntu/26.04/cloud`` something in the `apt-get install`
    path wipes /tmp mid-script, which broke the Quadlet copy step. The
    cause was not pinned to a tmpfiles.d rule or a cron job; /root is
    persistent and root-only, so it sidesteps the problem regardless.
    """
    quadlet_body = _read_provision_text("jailbee-registry-proxy.container")
    install_body = _read_provision_text("install.sh")

    script = f"""\
set -euo pipefail
cat > /root/jailbee-registry-proxy.container <<'JAILBEE_QUADLET_EOF'
{quadlet_body.rstrip()}
JAILBEE_QUADLET_EOF
cat > /root/install.sh <<'JAILBEE_INSTALL_EOF'
{install_body.rstrip()}
JAILBEE_INSTALL_EOF
chmod +x /root/install.sh
/root/install.sh
"""
    incus.exec(MIRROR_CONTAINER_NAME, ["bash", "-c", script], timeout=600)


def _wait_for_service_active(
    incus: Incus, on_step: Callable[[str], None] = _no_steps
) -> str | None:
    """Poll until the proxy service is active.

    Returns ``None`` once it is, or a human-readable reason when
    ``_SERVICE_WAIT_SECONDS`` elapses first. Reporting instead of raising is
    deliberate: ``registry_up`` treats a timeout as "provisioning may be
    incomplete — reinstall and give it one more window", which a raised
    exception would turn into a dead end.

    Reports each poll, carrying the service's own state and the time left.
    A bare "waiting" that sits there for a minute is indistinguishable from
    a hang; "activating, 42s left" is visibly a countdown.
    """
    started = time.monotonic()
    deadline = started + _SERVICE_WAIT_SECONDS
    while True:
        state = _service_state(incus)
        if state == "active":
            return None
        now = time.monotonic()
        if now >= deadline:
            return (
                f"{MIRROR_SERVICE_NAME} did not become active within "
                f"{_SERVICE_WAIT_SECONDS}s (last state: {state})"
            )
        on_step(f"waiting for {MIRROR_SERVICE_NAME} — {state}, {int(deadline - now)}s left")
        time.sleep(2)


def eth0_global_ipv4(container_info: dict[str, Any]) -> str | None:
    """Return a container's live eth0 IPv4 (global scope), or None.

    Reads from the ``state.network.eth0.addresses`` field of an
    ``incus list --format json`` entry. Mirrors the lookup
    ``docker_daemon.compute_mirror_endpoint`` does. ``None`` means the
    container has no eth0 yet (still booting) or only link-local
    addresses — callers should treat that as "no answer yet" rather than
    as a positive finding about one container.

    Nothing here is mirror-specific; `doctor` uses it to notice that
    *nothing* on the loose bridge is getting an address, which is what a
    host firewall blocking DHCP looks like from the outside.
    """
    state = container_info.get("state") or {}
    eth0 = state.get("network", {}).get("eth0", {})
    for addr in eth0.get("addresses") or []:
        if addr.get("family") == "inet" and addr.get("scope") == "global":
            value = addr.get("address")
            if isinstance(value, str):
                return value
    return None


def _delete_mirror(incus: Incus) -> None:
    """Remove the mirror container. No-op when it isn't there.

    ``force=True`` because a running container can't be deleted otherwise.
    The host bind-mount sources are deliberately untouched: the cache is the
    whole point of the mirror, and the CA *must* survive — every user
    container already trusts it, and a regenerated CA would break dockerd's
    TLS to the mirror everywhere at once.
    """
    if _container_present(incus) is None:
        return
    incus.delete(MIRROR_CONTAINER_NAME, force=True)


def _create_mirror(incus: Incus, cache_dir: Path, ca_dir: Path) -> None:
    """Create, configure and start the mirror container. Does not provision it.

    Split out of ``registry_up`` because ``--recreate`` needs the same
    sequence after deleting an existing container. The two disk devices
    bind-mount the host-side cache and CA directories that outlive any
    single container.
    """
    incus.init(_MIRROR_IMAGE, MIRROR_CONTAINER_NAME)
    incus.profile_assign(MIRROR_CONTAINER_NAME, ["default", MIRROR_PROFILE_NAME])
    incus.config_set(MIRROR_CONTAINER_NAME, "boot.autostart", "true")
    incus.config_device_add(
        MIRROR_CONTAINER_NAME,
        "cache",
        "disk",
        {"source": str(cache_dir), "path": "/docker_mirror_cache"},
    )
    incus.config_device_add(
        MIRROR_CONTAINER_NAME,
        "ca",
        "disk",
        {"source": str(ca_dir), "path": "/ca"},
    )
    incus.start(MIRROR_CONTAINER_NAME)


def _provisioning_incomplete(incus: Incus) -> bool:
    """True only when the Quadlet unit file is positively observed missing.

    ``install.sh`` installs that file *after* ``apt-get install podman …``,
    so a network drop during the apt stage leaves a container that boots
    fine with no proxy service and no unit file. Detecting it lets
    ``registry_up`` reinstall straight away instead of burning
    ``_SERVICE_WAIT_SECONDS`` waiting for a service that was never
    installed.

    Every other outcome returns ``False`` — the file is there, or the probe
    itself failed because a still-booting container isn't answering exec
    yet. Inferring "unprovisioned" from an inconclusive probe would
    reinstall on top of healthy mirrors; the caller's wait has its own
    reinstall fallback for anything this cannot see.
    """
    probe = f"test -f {_QUADLET_UNIT_PATH} && echo present || echo absent"
    try:
        out = incus.exec(MIRROR_CONTAINER_NAME, ["sh", "-c", probe], timeout=15)
    except IncusError:
        return False
    return out.strip() == "absent"


def _reinstall_proxy(incus: Incus) -> str | None:
    """Re-run install.sh in the mirror. ``None`` on success, else why it failed.

    install.sh is idempotent — its env-file write is guarded — so re-running
    it on a half-provisioned container is safe. A failure is reported rather
    than raised so the caller can fold it into one message alongside the
    service's own state: the user needs both facts.
    """
    try:
        _provision_mirror(incus)
    except IncusError as e:
        return f"reinstalling the proxy failed: {e}"
    return None


def _ensure_service_active(
    incus: Incus,
    *,
    provisioned: bool,
    repair_failure: str | None,
    on_step: Callable[[str], None] = _no_steps,
) -> None:
    """Wait for the proxy service, reinstalling once if it never comes up.

    The wait always runs first, even when a repair already failed earlier in
    this call: a service that comes up anyway is still success, so
    ``repair_failure`` only matters once the service is confirmed down.

    ``provisioned`` marks that this call already ran install.sh once this
    call — a fresh create, or the fast quadlet-missing repair — so a second,
    identical reinstall attempt here would be pointless; the failure (if any)
    from that earlier attempt is ``repair_failure``. Otherwise, one reinstall
    is attempted here before the second and final wait — the slow-path
    fallback for breakage the quadlet probe couldn't see.

    Raises ``RuntimeError`` naming ``--recreate`` when the service is still
    down after everything this call could try.
    """
    reason = _wait_for_service_active(incus, on_step)
    if reason is None:
        return
    if provisioned:
        if repair_failure is not None:
            reason = f"{reason}; {repair_failure}"
    else:
        on_step("service did not come up; reinstalling the proxy once (up to 10 min)")
        retry_failure = _reinstall_proxy(incus)
        if retry_failure is not None:
            reason = f"{reason}; {retry_failure}"
        else:
            second = _wait_for_service_active(incus, on_step)
            if second is None:
                return
            reason = f"reinstalled the proxy once; {second}"
    raise RuntimeError(
        f"{reason}. Run `jailbee registry up --recreate` to rebuild the mirror "
        f"container from scratch; the host-side cache and CA are preserved."
    )


def registry_up(
    incus: Incus,
    gcfg: GlobalConfig,
    *,
    recreate: bool = False,
    on_step: Callable[[str], None] = _no_steps,
) -> None:
    """Bring the mirror Incus container up. Idempotent.

    Repairs a half-provisioned mirror in place: reinstalls the proxy when
    its Quadlet unit is missing, and once more if the service never becomes
    active. ``recreate`` skips the repair path and rebuilds the container
    from the image instead — for mirrors that reinstalling can't fix. Host
    state under ``gcfg.docker_registry_mirror.data_dir`` survives either
    way.

    ``on_step`` is called as each phase begins. This function can run for
    several minutes — a first call downloads a base image and then installs
    podman inside the container — with nothing to show for it in between,
    which reads as a hang rather than as work.
    """
    on_step("preparing the host cache, CA and profile")
    cache_dir, ca_dir = _ensure_data_dirs(gcfg)
    _ensure_mirror_bridge(incus)
    _ensure_profile(incus)

    if recreate:
        on_step(f"deleting {MIRROR_CONTAINER_NAME}")
        _delete_mirror(incus)
        container_info = None
    else:
        container_info = _container_present(incus)

    provisioned = False
    repair_failure: str | None = None
    if container_info is None:
        # The image pull happens inside `incus init` with its output
        # captured, so Incus's own progress bar never reaches the terminal.
        # Saying how long this can take is the next best thing.
        on_step(f"creating {MIRROR_CONTAINER_NAME} from {_MIRROR_IMAGE} (first run downloads it)")
        _create_mirror(incus, cache_dir, ca_dir)
        on_step("installing the registry proxy in the container (apt + podman, up to 10 min)")
        # A first install failing is not a recovery scenario — its apt
        # stderr is the whole diagnosis — so this propagates raw rather than
        # going through _reinstall_proxy's report-don't-raise handling.
        _provision_mirror(incus)
        provisioned = True
    else:
        if container_info.get("status") != "Running":
            on_step(f"starting {MIRROR_CONTAINER_NAME}")
            incus.start(MIRROR_CONTAINER_NAME)
        else:
            # Container already running. If we just installed a static IP
            # reservation that doesn't match the live lease, force a
            # stop/start so dnsmasq re-issues under the new --dhcp-host=
            # rule and old lease records become inert. Without this an
            # existing host that upgrades into the reservation logic keeps
            # its previously-random lease, defeating the fix.
            wanted_ip = _compute_mirror_static_ip(incus)
            current_ip = eth0_global_ipv4(container_info)
            if wanted_ip is not None and current_ip is not None and current_ip != wanted_ip:
                on_step(f"restarting {MIRROR_CONTAINER_NAME} to pick up its static address")
                incus.stop(MIRROR_CONTAINER_NAME)
                incus.start(MIRROR_CONTAINER_NAME)
        if _provisioning_incomplete(incus):
            on_step("repairing the proxy install (apt + podman, up to 10 min)")
            repair_failure = _reinstall_proxy(incus)
            provisioned = True

    _ensure_service_active(
        incus, provisioned=provisioned, repair_failure=repair_failure, on_step=on_step
    )


def registry_down(incus: Incus) -> None:
    """Stop the mirror Incus container. Data + container persist."""
    info = _container_present(incus)
    if info is None:
        return
    if info.get("status") != "Running":
        return
    incus.stop(MIRROR_CONTAINER_NAME)


def _read_registries_env_file(incus: Incus) -> set[str]:
    """Parse REGISTRIES= from the mirror's env file. Empty set if absent."""
    out = incus.exec(
        MIRROR_CONTAINER_NAME,
        ["sh", "-c", f"cat {_PROXY_ENV_FILE} 2>/dev/null || true"],
        timeout=10,
    )
    for line in out.splitlines():
        if line.startswith("REGISTRIES="):
            return set(line.removeprefix("REGISTRIES=").split())
    return set()


def apply_mirror_registries(incus: Incus, registries: Iterable[str]) -> bool:
    """Ensure ``registries`` are cached by the running mirror.

    rpardini's nginx generates MITM certs + ``proxy_cache`` upstreams only
    for hostnames listed in its ``REGISTRIES`` env. Unlisted hostnames are
    CONNECT-tunneled without caching — so per-repo upstreams (notably ECR
    hosts) need adding here, otherwise every ``jailbee new`` re-pulls them
    from the internet.

    Idempotent: returns ``False`` when the file already contains everything
    requested. Otherwise rewrites ``/etc/jailbee-registry-proxy.env`` with the
    union of (rpardini image defaults + previously persisted set + caller's
    list), sorted for stable diffs, and restarts the proxy service. Set
    semantics mean once-added registries stick: a repo whose
    ``extra_registries`` later shrinks doesn't get them removed from the
    host-global mirror. To prune, recreate the container.
    """
    requested = set(registries)
    if not requested:
        return False

    current = _read_registries_env_file(incus)
    new_items = requested - current - _RPARDINI_DEFAULT_REGISTRIES
    if not new_items:
        return False

    full = sorted(current | _RPARDINI_DEFAULT_REGISTRIES | requested)
    env_body = f"REGISTRIES={' '.join(full)}\n"
    script = f"""\
set -euo pipefail
tmp=$(mktemp)
cat > "$tmp" <<'JAILBEE_ENV_EOF'
{env_body.rstrip()}
JAILBEE_ENV_EOF
mv "$tmp" {_PROXY_ENV_FILE}
systemctl restart {MIRROR_SERVICE_NAME}
"""
    incus.exec(MIRROR_CONTAINER_NAME, ["bash", "-c", script], timeout=60)
    return True
