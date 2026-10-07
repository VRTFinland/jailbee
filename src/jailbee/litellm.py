"""Manage the dedicated LiteLLM Incus container and its per-account proxy.

Provision with a temporary package-host-only ACL and no state volume, then
restrict egress to providers before attaching the state volume, pushing the
rendered files and starting the proxy. The volume survives container deletion.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml

from jailbee import litellm_state
from jailbee.config import CONTAINER_USERNAME
from jailbee.config.local_layer import local_litellm_scopes, scope_files
from jailbee.config.models_litellm import XAI_AUTH_HOST
from jailbee.incus import IncusError
from jailbee.litellm_inputs import load_host_inputs
from jailbee.litellm_render import (
    ACK_FILE,
    CONTAINER_STATE_DIR,
    ENV_FILE,
    HOT_FILE,
    InstanceFiles,
    account_login_providers,
    container_key_file,
    container_profiles,
    egress_hosts,
    render_instance_files,
)
from jailbee.loose_bridge import (
    LOOSE_BRIDGE,
    ensure_loose_bridge_acl,
    loose_bridge_gateways,
    loose_bridge_host_ip,
)
from jailbee.network import SERVICES_ACL, service_container_acl_yaml
from jailbee.services_acl import LITELLM_LABEL, set_service
from jailbee.stopping import stop_container

if TYPE_CHECKING:
    from jailbee.config.models_litellm import LiteLLMConfig, LiteLLMRepoView
    from jailbee.egress import EgressEntry
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus
    from jailbee.litellm_inputs import HostInputs

LITELLM_CONTAINER = "jailbee-litellm"
LITELLM_PROFILE = "jailbee-litellm-profile"
EGRESS_ACL = "jailbee-litellm-egress"
UNIT = "jailbee-litellm@{account}.service"
_IMAGE = "images:ubuntu/26.04/cloud"
_IP_INDEX = 1
_WAIT_SECONDS = 60
# A poll count, not a deadline: tests with `time.sleep` patched stay instant.
_ACK_POLLS = 20
_ACK_INTERVAL = 0.5
_PY = "/opt/litellm/bin/python"
LoginState = Literal["missing", "present", "unknown"]
LOGIN_PROVIDERS: tuple[str, ...] = ("chatgpt", "xai")
_AUTH_DIRS = {"chatgpt": "auth", "xai": "xai-auth"}
XAI_LOGIN_DEVICE = "xai-login"
XAI_CALLBACK_PORT = 56121
"""LiteLLM's fixed loopback callback port for the xAI login (`litellm/llms/xai/oauth.py`)."""
_PACKAGE_ENDPOINTS = (
    "pypi.org:443",
    "files.pythonhosted.org:443",
    "archive.ubuntu.com:80",
    "archive.ubuntu.com:443",
    "security.ubuntu.com:80",
    "security.ubuntu.com:443",
    "ports.ubuntu.com:80",
    "ports.ubuntu.com:443",
)
CONTAINER_FILE = "/etc/jailbee/litellm.json"
CONTAINER_KEY_GLOB = "/etc/jailbee/litellm-*.key"
_UNIT_NAME = re.compile(r"jailbee-litellm@([a-z0-9][a-z0-9_-]*)\.service")
STATE_VOLUME = "jailbee-litellm-state"
_AUTH_PROBE = (
    "import json, sys\n"
    "try:\n"
    "    data = json.load(open(sys.argv[1]))\n"
    "except (OSError, ValueError):\n"
    "    data = None\n"
    "ok = isinstance(data, dict) and any(\n"
    "    isinstance(data.get(k), str) and data[k] for k in ('access_token', 'refresh_token')\n"
    ")\n"
    "print('present' if ok else 'missing')\n"
)


def unit(account: str) -> str:
    return UNIT.format(account=account)


def _no_steps(_message: str) -> None:
    """Default callback for progress updates."""


class ContainerState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    MISSING = "missing"


@dataclass(frozen=True)
class InstanceStatus:
    account: str
    port: int | None
    active: bool
    healthy: bool
    login: LoginState
    # None: no route of this account uses an xAI subscription.
    xai_login: LoginState | None = None


@dataclass(frozen=True)
class LiteLLMStatus:
    container: ContainerState
    ip: str | None
    version: str | None
    instances: list[InstanceStatus]


@dataclass(frozen=True)
class UpResult:
    ip: str
    ports: dict[str, int]
    restarted: list[str]
    retired: list[str]
    installed: bool
    issues: list[str] = field(default_factory=list)
    # Left stopped: its routes need a ChatGPT login it lacks, and the proxy would block at startup
    # on LiteLLM's own device-code prompt, never answering its health probe.
    awaiting_login: list[str] = field(default_factory=list)
    # Started, but their `oauth` routes fail until `jailbee litellm login --provider xai`.
    missing_xai_login: list[str] = field(default_factory=list)
    # Route changes loaded into a running proxy without a restart.
    reloaded: list[str] = field(default_factory=list)
    # Why a live reload did not take; the account is then in `restarted`.
    fallbacks: dict[str, str] = field(default_factory=dict)


def _resolve_egress(hosts: list[str]) -> list[EgressEntry]:
    from jailbee.egress import build_egress_entries

    return build_egress_entries([h if ":" in h else f"{h}:443" for h in hosts])


def _write_egress_acl(incus: Incus, entries: list[EgressEntry], listen_ports: list[int]) -> None:
    """Create-if-missing and set the proxy's NIC ACL, DNS pinned to the bridge."""
    if not incus.network_acl_exists(EGRESS_ACL):
        incus.network_acl_create(EGRESS_ACL)
    incus.network_acl_set_yaml(
        EGRESS_ACL,
        service_container_acl_yaml(
            EGRESS_ACL,
            entries,
            listen_ports=listen_ports,
            gateways=loose_bridge_gateways(incus),
        ),
    )


def _set_egress(incus: Incus, entries: list[EgressEntry], ports: list[int]) -> None:
    _write_egress_acl(incus, entries, ports)


def _check_static_ip(incus: Incus, ip: str, containers: list[dict[str, object]]) -> None:
    """Fail before changing the instance when another NIC/lease owns our fixed IP."""
    own = next((c for c in containers if c.get("name") == LITELLM_CONTAINER), {})
    config = own.get("config")
    own_mac = config.get("volatile.eth0.hwaddr") if isinstance(config, dict) else None
    for lease in incus.network_leases(LOOSE_BRIDGE):
        if lease.get("address") == ip and not (
            lease.get("hostname") == LITELLM_CONTAINER
            or (own_mac and lease.get("hwaddr") == own_mac)
        ):
            owner = lease.get("hostname") or "an unknown DHCP client"
            raise RuntimeError(
                f"LiteLLM address {ip} is leased to {owner} on {LOOSE_BRIDGE}; "
                "release that lease or move the conflicting container, then run `jb litellm up`."
            )
    for container in containers:
        name = container.get("name")
        if not isinstance(name, str) or name == LITELLM_CONTAINER:
            continue
        parsed = yaml.safe_load(incus.config_show(name, expanded=True)) or {}
        devices = parsed.get("devices", {})
        if isinstance(devices, dict) and any(
            isinstance(device, dict)
            and device.get("type") == "nic"
            and device.get("network") == LOOSE_BRIDGE
            and device.get("ipv4.address") == ip
            for device in devices.values()
        ):
            raise RuntimeError(
                f"LiteLLM address {ip} is assigned to {name} on {LOOSE_BRIDGE}; "
                "change that NIC's static address, then run `jb litellm up`."
            )


def _has_state(incus: Incus) -> bool:
    parsed = yaml.safe_load(incus.config_show(LITELLM_CONTAINER)) or {}
    devices = parsed.get("devices", {})
    return isinstance(devices, dict) and "state" in devices


def _detach_state(incus: Incus) -> None:
    """Do not suppress device-removal errors: a retained auth mount is unsafe."""
    if _has_state(incus):
        incus.config_device_remove(LITELLM_CONTAINER, "state")


def _profile_root_pool(incus: Incus) -> str | None:
    """The pool of the `default` profile's root disk, or None when it names none."""
    parsed = yaml.safe_load(incus.profile_show("default")) or {}
    devices = parsed.get("devices") if isinstance(parsed, dict) else None
    root = devices.get("root") if isinstance(devices, dict) else None
    pool = root.get("pool") if isinstance(root, dict) else None
    return pool if isinstance(pool, str) and pool else None


def _state_pool(incus: Incus) -> str:
    """The pool of the `default` profile's root disk, where the volume lives."""
    pool = _profile_root_pool(incus)
    if pool is None:
        raise RuntimeError(
            "the default Incus profile has no root disk pool, so there is nowhere to "
            "keep the LiteLLM state volume; run `jailbee init` first."
        )
    return pool


def _ensure_state_volume(incus: Incus, storage_pool: str | None = None) -> str:
    """The pool holding the state volume, creating the volume when it exists nowhere.

    `storage_pool` (`defaults.storage_pool` in `global.yaml`) is where a new
    volume goes. A volume that already exists is kept where it is, in the
    configured pool or else the profile's: creating an empty one in a newly
    configured pool would silently drop every login and key the old one holds.
    Copy the volume to the new pool to move it (see docs/storage.md).
    """
    profile_pool = _profile_root_pool(incus)
    for pool in dict.fromkeys(p for p in (storage_pool, profile_pool) if p):
        if incus.storage_volume_exists(pool, STATE_VOLUME):
            return pool
    pool = storage_pool or _state_pool(incus)
    incus.storage_volume_create(pool, STATE_VOLUME)
    return pool


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _push_state(incus: Incus, files: list[InstanceFiles], callback_source: str) -> None:
    """Write the rendered files into the state volume, through stdin: they hold keys.

    Base64 keeps arbitrary content (an `extra` fragment included) out of any
    heredoc delimiter or shell quoting.
    """
    root = CONTAINER_STATE_DIR
    lines = [
        "set -euo pipefail",
        "umask 077",
        'put() { tmp=$(mktemp "$(dirname "$1")/.jb.XXXXXX"); '
        'printf %s "$2" | base64 -d > "$tmp"; chmod 0600 "$tmp"; mv -f "$tmp" "$1"; }',
        f"mkdir -p {root}/callback; chmod 0700 {root} {root}/callback",
        f"put {root}/callback/jailbee_callback.py {_b64(callback_source)}",
    ]
    for f in files:
        base = f"{root}/{litellm_state.check_account(f.account)}"
        lines += [
            f"mkdir -p {base}/auth; chmod 0700 {base} {base}/auth",
            # The callback re-reads instance.env on reload, so it lands before hot.json.
            f"put {base}/{ENV_FILE} {_b64(f.instance_env)}",
            f"put {base}/config.yaml {_b64(f.config_yaml)}",
            # Last: the running proxy reloads when this file changes.
            f"put {base}/{HOT_FILE} {_b64(f.hot_json)}",
        ]
    incus.exec_with_input(LITELLM_CONTAINER, ["bash", "-s"], "\n".join(lines) + "\n", timeout=60)


def _profile_yaml(ip: str | None, *, with_acl: bool) -> str:
    eth0: dict[str, str] = {"type": "nic", "name": "eth0", "network": LOOSE_BRIDGE}
    if ip is not None:
        eth0["ipv4.address"] = ip
    if with_acl:
        eth0["security.acls"] = EGRESS_ACL
        eth0["security.acls.default.egress.action"] = "reject"
        eth0["security.acls.default.ingress.action"] = "reject"
    profile = {
        "name": LITELLM_PROFILE,
        "description": "security + network for the jailbee-litellm container",
        "config": {"security.nesting": "true"},
        "devices": {"eth0": eth0},
    }
    return yaml.safe_dump(profile, sort_keys=False)


def _set_profile(incus: Incus, ip: str | None, *, with_acl: bool) -> None:
    if not incus.profile_exists(LITELLM_PROFILE):
        incus.profile_create(LITELLM_PROFILE)
    incus.profile_set_yaml(LITELLM_PROFILE, _profile_yaml(ip, with_acl=with_acl))


def _container(incus: Incus) -> dict[str, object] | None:
    for container in incus.list_containers():
        if container.get("name") == LITELLM_CONTAINER:
            return container
    return None


def _installed_version(incus: Incus) -> str | None:
    probe = "import importlib.metadata as m; print(m.version('litellm'))"
    try:
        return incus.exec(LITELLM_CONTAINER, [_PY, "-c", probe], timeout=30).strip() or None
    except IncusError:
        return None


def _read(name: str) -> str:
    return resources.files("jailbee.provision").joinpath("litellm").joinpath(name).read_text()


def _provision(incus: Incus, version: str, pinned: bool) -> None:
    # Shell-quote the user-configurable version; do not interpolate raw YAML
    # into a command run as root inside the service container.
    unlocked = shlex.quote("" if pinned else version)
    script = f"""\
set -euo pipefail
cat > /root/install.sh <<'JB_INSTALL_EOF'
{_read("install.sh").rstrip()}
JB_INSTALL_EOF
cat > /root/jailbee-litellm@.service <<'JB_SERVICE_EOF'
{_read("jailbee-litellm@.service").rstrip()}
JB_SERVICE_EOF
cat > /root/litellm-requirements.lock <<'JB_LOCK_EOF'
{_read("requirements.lock").rstrip()}
JB_LOCK_EOF
cat > /root/litellm-chatgpt-stream-fix.py <<'JB_FIX_EOF'
{_read("chatgpt_stream_fix.py").rstrip()}
JB_FIX_EOF
chmod +x /root/install.sh
JAILBEE_LITELLM_UNLOCKED_VERSION={unlocked} /root/install.sh
"""
    incus.exec_with_input(LITELLM_CONTAINER, ["bash", "-s"], script, timeout=900)


def _annotate_recovery(error: BaseException, details: list[str]) -> None:
    recovery = "; ".join(details)
    error.add_note(recovery)
    # The CLI prints str(IncusError), not traceback notes. Keep the original
    # exception object/traceback and make any failed recovery visible.
    if isinstance(error, IncusError):
        error.args = (f"{error}; {recovery}",)


def _stop_unrestricted_container(incus: Incus, details: list[str]) -> None:
    """Disable autostart and stop; delete if either protection fails."""
    disabled = stopped = True
    try:
        incus.config_set(LITELLM_CONTAINER, "boot.autostart", "false")
    except Exception as disable_error:
        disabled = False
        details.append(f"Failed to disable LiteLLM autostart: {disable_error}")
    try:
        incus.stop(LITELLM_CONTAINER, force=True)
        details.append("container force-stopped")
    except Exception as stop_error:
        stopped = False
        details.append(f"Failed to force-stop unrestricted LiteLLM container: {stop_error}")
    if disabled and stopped:
        return
    try:
        incus.delete(LITELLM_CONTAINER, force=True)
        details.append("container force-deleted")
    except Exception as delete_error:
        details.append(
            "SECURITY: LiteLLM container may still run or autostart without an egress ACL; "
            f"force-delete failed: {delete_error}"
        )


def _secure_failed_create(incus: Incus, error: BaseException) -> None:
    """Discard a partial instance even if `init`/`start` reported an error."""
    try:
        incus.delete(LITELLM_CONTAINER, force=True)
    except Exception as delete_error:
        details = [f"Failed to delete partial LiteLLM container: {delete_error}"]
        _stop_unrestricted_container(incus, details)
        _annotate_recovery(error, details)


def _secure_failed_install(incus: Incus, ip: str, error: BaseException) -> None:
    """Reattach default-deny egress or retire the unprotected container.

    Do not resolve providers on this error path: DNS can also be broken during
    provisioning, so a DHCP/DNS-only ACL is safer and reliably renderable.
    Keep the original install exception and annotate any cleanup failure.
    """
    try:
        _write_egress_acl(incus, [], [])
        _set_profile(incus, ip, with_acl=True)
    except Exception as restore_error:
        details = [f"Failed to restore restrictive LiteLLM NIC ACL: {restore_error}"]
        _stop_unrestricted_container(incus, details)
        _annotate_recovery(error, details)


def _active(incus: Incus, account: str) -> bool:
    try:
        return (
            incus.exec(
                LITELLM_CONTAINER, ["systemctl", "is-active", unit(account)], timeout=10
            ).strip()
            == "active"
        )
    except IncusError:
        return False


def _healthy(incus: Incus, port: int) -> bool:
    probe = (
        "import urllib.request; "
        f"urllib.request.urlopen('http://127.0.0.1:{port}/health/liveliness', timeout=5); "
        "print('ok')"
    )
    try:
        return incus.exec(LITELLM_CONTAINER, [_PY, "-c", probe], timeout=15).strip() == "ok"
    except IncusError:
        return False


def _wait_healthy(incus: Incus, account: str, port: int, on_step: Callable[[str], None]) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while True:
        if _active(incus, account) and _healthy(incus, port):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"{unit(account)} did not become healthy within {_WAIT_SECONDS}s. "
                "See `jailbee litellm logs`."
            )
        on_step(f"waiting for {unit(account)}")
        time.sleep(2)


def _ack_path(account: str) -> str:
    return f"{CONTAINER_STATE_DIR}/{litellm_state.check_account(account)}/{ACK_FILE}"


def _read_ack(incus: Incus, account: str) -> dict[str, object] | None:
    try:
        raw = incus.exec(LITELLM_CONTAINER, ["cat", _ack_path(account)], timeout=10)
    except IncusError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _reload_hot(incus: Incus, instance: InstanceFiles) -> str | None:
    """Wait for the running proxy to confirm the `hot.json` just pushed. None = loaded."""
    expected = instance.hot_digest()
    for _ in range(_ACK_POLLS):
        ack = _read_ack(incus, instance.account)
        if ack is not None and ack.get("hot_digest") == expected:
            error = ack.get("error")
            return None if error is None else f"the proxy refused the new routes ({error})"
        time.sleep(_ACK_INTERVAL)
    return "the proxy did not acknowledge the new routes in time"


def _deployed_accounts(incus: Incus) -> set[str]:
    """Accounts with a loaded or enabled unit in the proxy container."""
    script = (
        "systemctl list-units --all --plain --no-legend 'jailbee-litellm@*.service' || true; "
        "ls -1 /etc/systemd/system/multi-user.target.wants/ 2>/dev/null || true"
    )
    return set(
        _UNIT_NAME.findall(incus.exec(LITELLM_CONTAINER, ["bash", "-c", script], timeout=30))
    )


def _retire_accounts(incus: Incus, keep: set[str]) -> list[str]:
    """Stop and disable units of accounts no longer configured; their logins stay in the volume."""
    retired = sorted(_deployed_accounts(incus) - keep)
    for account in retired:
        incus.exec(LITELLM_CONTAINER, ["systemctl", "disable", "--now", unit(account)], timeout=60)
    return retired


def _render_all(
    cfg: LiteLLMConfig,
    scopes: Mapping[str, LiteLLMConfig],
    ports: dict[str, int],
    inputs: HostInputs,
) -> list[InstanceFiles]:
    return [
        render_instance_files(
            cfg,
            account,
            port=ports[account],
            master_key=litellm_state.master_key(account),
            secrets=inputs.secrets,
            extra=inputs.extra,
            scopes=scopes,
        )
        for account in cfg.accounts
    ]


@dataclass
class Converged:
    restarted: list[str] = field(default_factory=list)
    reloaded: list[str] = field(default_factory=list)
    awaiting_login: list[str] = field(default_factory=list)
    missing_xai_login: list[str] = field(default_factory=list)
    # Accounts whose instance needed a restart but was not allowed one (`allow_restart=False`).
    unreloaded: list[str] = field(default_factory=list)
    # Why a live reload did not take; the account is then in `restarted` or `unreloaded`.
    fallbacks: dict[str, str] = field(default_factory=dict)


def _converge(
    incus: Incus,
    files: list[InstanceFiles],
    ports: dict[str, int],
    callback_source: str,
    *,
    force: bool,
    allow_restart: bool,
    on_step: Callable[[str], None],
) -> Converged:
    """Bring each pushed instance in line: restart if its cold half changed, else reload.

    A changed `hot.json` is loaded into the running proxy and confirmed by its
    acknowledgement; only when that fails is the instance restarted (when
    `allow_restart`), so a bad reload never leaves a silently stale proxy.
    An account whose routes need a ChatGPT login and lack it is kept stopped
    instead: its proxy would sit in LiteLLM's device-code prompt and never turn
    healthy. `jailbee litellm login` then `up` starts it. An account serving no
    `chatgpt/` route never waits for a login.

    The stamps are recorded only after the unit is confirmed on the new files,
    so a run that fails in between is retried by the next one.
    """
    done = Converged()
    for instance in files:
        account = instance.account
        if "chatgpt" in instance.login_providers and auth_state(incus, account) == "missing":
            incus.exec(
                LITELLM_CONTAINER, ["systemctl", "disable", "--now", unit(account)], timeout=60
            )
            done.awaiting_login.append(account)
            continue
        if "xai" in instance.login_providers and auth_state(incus, account, "xai") == "missing":
            # Unlike ChatGPT, LiteLLM reads the xAI token per request: the proxy
            # starts and stays healthy, and only its `oauth` routes fail.
            done.missing_xai_login.append(account)
        cold, hot = instance.digest(callback_source), instance.hot_digest()
        restart = (
            force or not litellm_state.config_applied(account, cold) or not _active(incus, account)
        )
        reloaded = False
        if not restart and not litellm_state.hot_applied(account, hot):
            problem = _reload_hot(incus, instance)
            if problem is None:
                reloaded = True
            else:
                done.fallbacks[account] = problem
                # The proxy may already serve (part of) the new routes, so the old
                # stamp no longer describes it: reverting the config must push again.
                litellm_state.clear_hot_applied(account)
                restart = True
        if restart and not allow_restart:
            done.unreloaded.append(account)
            continue
        if restart:
            incus.exec(LITELLM_CONTAINER, ["systemctl", "enable", unit(account)], timeout=30)
            incus.exec(LITELLM_CONTAINER, ["systemctl", "restart", unit(account)], timeout=60)
        _wait_healthy(incus, account, ports[account], on_step)
        if restart:
            litellm_state.record_applied(account, cold)
            litellm_state.record_hot_applied(account, hot)
            done.restarted.append(account)
        elif reloaded:
            litellm_state.record_hot_applied(account, hot)
            done.reloaded.append(account)
    return done


def litellm_up(
    incus: Incus,
    gcfg: GlobalConfig,
    *,
    reinstall: bool = False,
    recreate: bool = False,
    on_step: Callable[[str], None] = _no_steps,
) -> UpResult:
    cfg = gcfg.litellm
    if not cfg.enabled:
        raise ValueError("LiteLLM is disabled: set `litellm.enabled: true` in global.yaml first.")
    # Host inputs first: a missing secret must fail before anything changes.
    scopes, issues = local_litellm_scopes(cfg)
    inputs = load_host_inputs(cfg, scopes.values(), scope_files(scopes))
    version = cfg.effective_version()
    pinned = cfg.version is None

    on_step("rendering the proxy configuration")
    callback_source = _read("jailbee_callback.py")
    ports = {account: litellm_state.port_for(account) for account in cfg.accounts}
    files = _render_all(cfg, scopes, ports, inputs)
    listen = sorted(ports.values())

    if not incus.network_exists(LOOSE_BRIDGE):
        incus.network_create(LOOSE_BRIDGE)
    # Before any NIC ACL is written: see ensure_loose_bridge_acl.
    ensure_loose_bridge_acl(incus)
    ip = loose_bridge_host_ip(incus, _IP_INDEX)
    if ip is None:
        raise RuntimeError(
            f"{LOOSE_BRIDGE} has no concrete IPv4 subnet; the LiteLLM proxy needs a static address."
        )

    containers = incus.list_containers()
    _check_static_ip(incus, ip, containers)
    pool = _ensure_state_volume(incus, gcfg.service_storage_pool)
    info = next((c for c in containers if c.get("name") == LITELLM_CONTAINER), None)
    if recreate and info is not None:
        # The state volume is a separate object and survives: logins and secrets
        # carry over, only the container (and so its pool) is made again.
        on_step(f"deleting {LITELLM_CONTAINER}")
        incus.delete(LITELLM_CONTAINER, force=True)
        info = None
    needs_install = reinstall or info is None
    if info is None:
        _set_egress(incus, _resolve_egress(list(_PACKAGE_ENDPOINTS)), listen)
        _set_profile(incus, ip, with_acl=True)
        on_step(f"creating {LITELLM_CONTAINER} from {_IMAGE}")
        try:
            incus.init(_IMAGE, LITELLM_CONTAINER, storage_pool=gcfg.service_storage_pool)
            incus.profile_assign(LITELLM_CONTAINER, ["default", LITELLM_PROFILE])
            incus.start(LITELLM_CONTAINER)
        except BaseException as error:
            _secure_failed_create(incus, error)
            raise
    elif info.get("status") != "Running" and not reinstall:
        # A stopped container can retain an ACL-free profile from an interrupted
        # install. Restrict it before start: Incus may report a failed start
        # after the instance has already reached Running.
        try:
            _write_egress_acl(incus, [], [])
            _set_profile(incus, ip, with_acl=True)
            incus.start(LITELLM_CONTAINER)
        except BaseException as error:
            _secure_failed_install(incus, ip, error)
            raise
    if not needs_install and _installed_version(incus) != version:
        needs_install = True

    try:
        if needs_install:
            if info is not None:
                # No live service or mounted token directory may see package egress.
                incus.config_set(LITELLM_CONTAINER, "boot.autostart", "false")
                if info.get("status") == "Running" or not reinstall:
                    incus.stop(LITELLM_CONTAINER, force=True)
                _detach_state(incus)
                _set_egress(incus, _resolve_egress(list(_PACKAGE_ENDPOINTS)), listen)
                _set_profile(incus, ip, with_acl=True)
                incus.start(LITELLM_CONTAINER)
            on_step(f"installing LiteLLM {version} (up to 15 min)")
            _provision(incus, version, pinned)

        on_step("writing the proxy's egress allowlist")
        _set_egress(incus, _resolve_egress(egress_hosts(cfg, scopes=scopes)), listen)
        _set_profile(incus, ip, with_acl=True)
        # A run that failed after the install but before this point leaves the
        # container installed and locked down without its state mount; the next
        # run sees the right version, so it must attach the mount here too.
        attach_state = needs_install or not _has_state(incus)
        if attach_state:
            incus.config_device_add(
                LITELLM_CONTAINER,
                "state",
                "disk",
                {"pool": pool, "source": STATE_VOLUME, "path": CONTAINER_STATE_DIR},
            )
    except BaseException as error:
        if needs_install:
            _secure_failed_install(incus, ip, error)
        raise

    if attach_state:
        # Never autoboot while package access and auth state can coexist.
        incus.config_set(LITELLM_CONTAINER, "boot.autostart", "true")

    on_step("writing the proxy configuration")
    _push_state(incus, files, callback_source)
    # Always, reinstall included: a reinstall keeps the rootfs, and with it the
    # enabled symlink of an account that has since been removed.
    retired = _retire_accounts(incus, set(cfg.accounts))

    done = _converge(
        incus,
        files,
        ports,
        callback_source,
        force=needs_install,
        allow_restart=True,
        on_step=on_step,
    )

    set_service(incus, LITELLM_LABEL, ([ip], listen))
    return UpResult(
        ip=ip,
        ports=ports,
        restarted=done.restarted,
        retired=retired,
        installed=needs_install,
        issues=issues,
        awaiting_login=done.awaiting_login,
        missing_xai_login=done.missing_xai_login,
        reloaded=done.reloaded,
        fallbacks=done.fallbacks,
    )


def litellm_down(incus: Incus, *, purge: bool = False) -> None:
    """Stop the proxy and close its dev-container ACL allowance; container and state persist.

    `purge` deletes the container and every login and secret it held instead.
    """
    set_service(incus, LITELLM_LABEL, None)
    info = _container(incus)
    if not purge:
        if info is not None and info.get("status") == "Running":
            stop_container(incus, LITELLM_CONTAINER, force_fallback=True, label="the LiteLLM proxy")
        return
    if info is not None:
        incus.delete(LITELLM_CONTAINER, force=True)
    # Wherever it lives: the pool it was created in may no longer be the
    # profile's, nor the one `defaults.storage_pool` names today.
    listed = (str(p.get("name")) for p in incus.list_storage_pools())
    pools = [_profile_root_pool(incus), *listed]
    for pool in dict.fromkeys(p for p in pools if p):
        if incus.storage_volume_exists(pool, STATE_VOLUME):
            incus.storage_volume_delete(pool, STATE_VOLUME)


@dataclass(frozen=True)
class ReconcileResult:
    """What `jailbee apply` did to the proxy."""

    restarted: list[str] = field(default_factory=list)
    # Changed but left alone (`--no-restart`) because it needs a restart; the
    # stamps are not recorded, so the next plain `apply` converges them.
    pending: list[str] = field(default_factory=list)
    # The subset of `pending` whose unit is not running at all (not merely on old routes).
    stopped: list[str] = field(default_factory=list)
    # Why only `jailbee litellm up` can bring the proxy in line; None if it was not needed.
    needs_up: str | None = None
    issues: list[str] = field(default_factory=list)
    # Skipped, not started: no login yet (see `UpResult.awaiting_login`).
    awaiting_login: list[str] = field(default_factory=list)
    # Started, but their `oauth` routes fail until `jailbee litellm login --provider xai`.
    missing_xai_login: list[str] = field(default_factory=list)
    reloaded: list[str] = field(default_factory=list)
    # Why a live reload did not take; the account is then in `restarted` or `pending`.
    fallbacks: dict[str, str] = field(default_factory=dict)


def litellm_reconcile(
    incus: Incus,
    gcfg: GlobalConfig,
    *,
    restart: bool = True,
    on_step: Callable[[str], None] = _no_steps,
) -> ReconcileResult | None:
    """Bring a running proxy in line with the config; restart only changed instances.

    For `jailbee apply`, so an edited route or repo override reaches the
    proxy without `jailbee litellm up`. Never creates the container,
    installs, allocates a port or retires an account: those are `up`'s, and
    `needs_up` says so. None when LiteLLM is off or its container is not
    running: there is nothing to reconcile, and `apply` must not start it.
    """
    cfg = gcfg.litellm
    if not cfg.enabled:
        return None
    info = _container(incus)
    if info is None or info.get("status") != "Running":
        return None
    scopes, issues = local_litellm_scopes(cfg)
    version = cfg.effective_version()
    installed = _installed_version(incus)
    if installed != version:
        return ReconcileResult(
            needs_up=f"it runs LiteLLM {installed or 'unknown'}, the config asks for {version}",
            issues=issues,
        )
    if not _has_state(incus):
        # An interrupted `up` (or a concurrent reinstall): pushing now would
        # leave keys on the rootfs and rewrite the package-egress ACL.
        return ReconcileResult(
            needs_up="its state volume is not attached (an interrupted `jailbee litellm up`)",
            issues=issues,
        )
    ports = {account: litellm_state.known_port(account) for account in cfg.accounts}
    missing = sorted(
        account
        for account, port in ports.items()
        if port is None or not litellm_state.master_key_path(account).exists()
    )
    if missing:
        return ReconcileResult(
            needs_up=f"account(s) {', '.join(missing)} have no proxy instance yet", issues=issues
        )
    removed = sorted(_deployed_accounts(incus) - set(cfg.accounts))
    if removed:
        return ReconcileResult(
            needs_up=f"account(s) {', '.join(removed)} left `litellm.accounts` but still run",
            issues=issues,
        )
    known = {account: port for account, port in ports.items() if port is not None}
    inputs = load_host_inputs(cfg, scopes.values(), scope_files(scopes))
    callback_source = _read("jailbee_callback.py")
    files = _render_all(cfg, scopes, known, inputs)
    down = {f.account for f in files if not _active(incus, f.account)}
    cold = [
        f
        for f in files
        if not litellm_state.config_applied(f.account, f.digest(callback_source))
        or f.account in down
    ]
    cold_accounts = {f.account for f in cold}
    hot = [
        f
        for f in files
        if f.account not in cold_accounts
        and not litellm_state.hot_applied(f.account, f.hot_digest())
    ]
    if not cold and not hot:
        return ReconcileResult(issues=issues)
    # Reloads interrupt nothing, so `--no-restart` still applies them; only an
    # instance needing a restart stays pending; a cold one is not written to.
    pending = [] if restart else [f.account for f in cold]
    stopped = [] if restart else [f.account for f in cold if f.account in down]
    targets = [*cold, *hot] if restart else hot
    done = Converged()
    if targets:
        on_step("writing the proxy's egress allowlist")
        _set_egress(
            incus, _resolve_egress(egress_hosts(cfg, scopes=scopes)), sorted(known.values())
        )
        on_step("writing the proxy configuration")
        _push_state(incus, targets, callback_source)
        done = _converge(
            incus,
            targets,
            known,
            callback_source,
            force=False,
            allow_restart=restart,
            on_step=on_step,
        )
    return ReconcileResult(
        restarted=done.restarted,
        reloaded=done.reloaded,
        pending=[*pending, *done.unreloaded],
        stopped=stopped,
        issues=issues,
        awaiting_login=done.awaiting_login,
        missing_xai_login=done.missing_xai_login,
        fallbacks=done.fallbacks,
    )


def container_sync_payload(
    incus: Incus, gcfg: GlobalConfig, *, view: LiteLLMRepoView | None = None
) -> dict[str, object] | None:
    """Resolve the dev-container settings without putting a key in a background job.

    Only accounts `up` has brought up (a port and a master key exist) are
    offered; profiles bound to any other account are listed as `unserved`.
    When none is offered, `json` is None: `sync_container` then retires the
    settings, and `unserved` still names the profiles for the warning.
    `view` is one repo's (`Config.litellm_view()`); None means the host's own.
    """
    cfg = view.config if view is not None else gcfg.litellm
    scope = view.scope if view is not None else None
    if not cfg.enabled or _container(incus) is None:
        return None
    ip = loose_bridge_host_ip(incus, _IP_INDEX)
    if ip is None:
        return None
    base_urls: dict[str, str] = {}
    key_paths: dict[str, str] = {}
    for account in cfg.accounts:
        port = litellm_state.known_port(account)
        key_path = litellm_state.master_key_path(account)
        if port is None or not key_path.exists():
            continue
        base_urls[account] = f"http://{ip}:{port}"
        key_paths[account] = str(key_path)
    profiles = container_profiles(cfg, base_urls=base_urls, scope=scope)
    effective = cfg.effective_profiles()
    unserved = sorted(set(effective) - set(profiles))
    if not profiles:
        return {"json": None, "keys": {}, "unserved": unserved}
    used = {cfg.instance_account(effective[name]) for name in profiles}
    return {
        "json": {"version": 1, "default_profile": cfg.default_profile, "profiles": profiles},
        "keys": {account: key_paths[account] for account in sorted(used)},
        "unserved": unserved,
    }


def unserved_profiles(payload: dict[str, object] | None) -> list[str]:
    names = payload.get("unserved") if payload else None
    return [str(n) for n in names] if isinstance(names, list) else []


def unserved_warning(payload: dict[str, object] | None) -> str | None:
    """The one message `apply` and `new` print for profiles no instance serves."""
    unserved = unserved_profiles(payload)
    if not unserved:
        return None
    return (
        f"LiteLLM profile(s) {', '.join(unserved)} have no proxy instance yet; "
        "run `jailbee litellm up`, then `jailbee apply`."
    )


def _stale_key_loop(keep: list[str], pattern: str) -> str:
    listed = " ".join(keep)
    return f'for f in {pattern}; do case " {listed} " in *" $f "*) ;; *) rm -f "$f" ;; esac; done'


def sync_container(incus: Incus, name: str, payload: dict[str, object] | None) -> None:
    """Install the settings and one key per account, or retire stale settings.

    Retire when there is no payload, or when it serves nothing (`json` None).
    """
    if payload is None or payload.get("json") is None:
        incus.exec(name, ["bash", "-c", f"rm -f {CONTAINER_FILE} {CONTAINER_KEY_GLOB}"], timeout=30)
        return
    keys = payload["keys"]
    assert isinstance(keys, dict)
    body = json.dumps(payload["json"], indent=2)
    lines = ["set -euo pipefail", "mkdir -p /etc/jailbee"]
    targets: list[str] = []
    for account, key_path in sorted(keys.items()):
        key = Path(str(key_path)).read_text().strip()
        assert "'" not in key
        target = container_key_file(litellm_state.check_account(str(account)))
        targets.append(target)
        lines += [
            "tmp=$(mktemp)",
            f"printf '%s\\n' '{key}' > \"$tmp\"",
            f'chmod 0640 "$tmp"; chown root:{CONTAINER_USERNAME} "$tmp"; mv "$tmp" {target}',
        ]
    # Keys first, then the JSON naming them, then the stale keys: the JSON
    # on disk never points at a key file that is not there.
    lines += [
        "tmp=$(mktemp)",
        "cat > \"$tmp\" <<'JB_EOF'",
        body,
        "JB_EOF",
        f'chmod 0644 "$tmp"; mv "$tmp" {CONTAINER_FILE}',
        _stale_key_loop(targets, CONTAINER_KEY_GLOB),
    ]
    incus.exec_with_input(name, ["bash", "-s"], "\n".join(lines) + "\n", timeout=30)


def upstream_reachable(incus: Incus, host: str, port: int = 443) -> bool:
    """Probe provider TCP reachability from inside the restricted proxy."""
    probe = f"import socket; socket.create_connection(({host!r}, {port}), 5); print('ok')"
    try:
        return incus.exec(LITELLM_CONTAINER, [_PY, "-c", probe], timeout=15).strip() == "ok"
    except IncusError:
        return False


def litellm_status(incus: Incus, gcfg: GlobalConfig) -> LiteLLMStatus:
    info = _container(incus)
    if info is None:
        return LiteLLMStatus(ContainerState.MISSING, None, None, [])
    ip = loose_bridge_host_ip(incus, _IP_INDEX)
    if info.get("status") != "Running":
        return LiteLLMStatus(ContainerState.STOPPED, ip, None, [])
    instances: list[InstanceStatus] = []
    cfg = gcfg.litellm
    for account in cfg.accounts:
        port = litellm_state.known_port(account)
        instances.append(
            InstanceStatus(
                account=account,
                port=port,
                active=_active(incus, account),
                healthy=port is not None and _healthy(incus, port),
                login=auth_state(incus, account),
                xai_login=(
                    auth_state(incus, account, "xai")
                    if "xai" in login_providers(cfg, account)
                    else None
                ),
            )
        )
    return LiteLLMStatus(ContainerState.RUNNING, ip, _installed_version(incus), instances)


def _require_running(incus: Incus) -> None:
    info = _container(incus)
    if info is None or info.get("status") != "Running":
        raise RuntimeError(f"{LITELLM_CONTAINER} is not running. Run `jailbee litellm up` first.")


def login_providers(cfg: LiteLLMConfig, account: str) -> tuple[str, ...]:
    """The logins the account's instance needs, repo overrides included."""
    scopes, _issues = local_litellm_scopes(cfg)
    return account_login_providers(cfg, account, scopes)


def auth_state(incus: Incus, account: str, provider: str = "chatgpt") -> LoginState:
    """Whether the account holds the provider's login; never reads token material out."""
    account_dir = litellm_state.check_account(account)
    path = f"{CONTAINER_STATE_DIR}/{account_dir}/{_AUTH_DIRS[provider]}/auth.json"
    try:
        out = incus.exec(LITELLM_CONTAINER, [_PY, "-c", _AUTH_PROBE, path], timeout=15).strip()
    except IncusError:
        return "unknown"
    return "present" if out == "present" else "missing"


def litellm_logout(incus: Incus, account: str, provider: str = "chatgpt") -> bool:
    """Delete the provider's token in the volume; True if there was one."""
    _require_running(incus)
    account_dir = litellm_state.check_account(account)
    path = shlex.quote(f"{CONTAINER_STATE_DIR}/{account_dir}/{_AUTH_DIRS[provider]}/auth.json")
    script = f"if [ -e {path} ]; then rm -f -- {path}; echo removed; fi"
    return incus.exec(LITELLM_CONTAINER, ["bash", "-c", script], timeout=15).strip() == "removed"


def litellm_login(incus: Incus, account: str) -> int:
    """Start LiteLLM's own ChatGPT device-code flow on an interactive PTY."""
    _require_running(incus)
    env_file = shlex.quote(f"{CONTAINER_STATE_DIR}/{account}/{ENV_FILE}")
    auth_dir = shlex.quote(f"{CONTAINER_STATE_DIR}/{account}/auth")
    script = (
        f"set -e; umask 0077; test -r {env_file}; unset CHATGPT_TOKEN_DIR; "
        f"set -a; . {env_file}; set +a; "
        f'test "${{CHATGPT_TOKEN_DIR:-}}" = {auth_dir}; '
        f"exec {_PY} -c 'from litellm.llms.chatgpt.authenticator import Authenticator; "
        f'Authenticator().get_access_token(); print("Logged in.")\''
    )
    return incus.exec_interactive(LITELLM_CONTAINER, ["bash", "-c", script])


_XAI_DISCOVERY_PROBE = (
    "import json, urllib.parse, urllib.request\n"
    "doc = json.load(urllib.request.urlopen("
    "'https://auth.x.ai/.well-known/openid-configuration', timeout=15))\n"
    "print(urllib.parse.urlsplit(doc.get('token_endpoint') or '').hostname or '')\n"
)


def litellm_login_xai(incus: Incus, cfg: LiteLLMConfig, account: str) -> int:
    """Run LiteLLM's xAI browser login, its loopback callback forwarded from the host.

    LiteLLM listens on 127.0.0.1:56121 inside the proxy; a proxy device makes
    the same address on the host reach it while the login runs, and only then.
    """
    _require_running(incus)
    if "xai" not in login_providers(cfg, account):
        raise RuntimeError(
            f"No route of account {account} uses an xAI subscription (`oauth: true`). "
            "Add one, run `jailbee litellm up`, then log in."
        )
    try:
        token_host = incus.exec(
            LITELLM_CONTAINER, [_PY, "-c", _XAI_DISCOVERY_PROBE], timeout=30
        ).strip()
    except IncusError as exc:
        raise RuntimeError(
            f"The proxy cannot reach {XAI_AUTH_HOST}; run `jailbee litellm up` and retry ({exc})."
        ) from exc
    if token_host != XAI_AUTH_HOST:
        raise RuntimeError(
            f"xAI moved its token endpoint to {token_host or '(none)'}, which the proxy may "
            "not reach; please report this to jailbee."
        )
    helper = "/usr/local/lib/jailbee-xai-login.py"
    incus.exec_with_input(
        LITELLM_CONTAINER,
        ["bash", "-s"],
        f"set -e; umask 0022; install -d -m 0755 /usr/local/lib\n"
        f"cat > {helper} <<'JAILBEE_XAI_LOGIN'\n{_read('xai_login.py')}\n"
        f"JAILBEE_XAI_LOGIN\nchown root:root {helper}\nchmod 0644 {helper}\n",
        timeout=30,
    )
    env_file = shlex.quote(f"{CONTAINER_STATE_DIR}/{account}/{ENV_FILE}")
    auth_dir = shlex.quote(f"{CONTAINER_STATE_DIR}/{account}/{_AUTH_DIRS['xai']}")
    script = (
        f"set -e; umask 0077; test -r {env_file}; "
        "unset XAI_OAUTH_TOKEN_DIR XAI_OAUTH_AUTH_FILE XAI_API_KEY XAI_API_BASE "
        "XAI_OAUTH_API_BASE; "
        f"set -a; . {env_file}; set +a; "
        f'test "${{XAI_OAUTH_TOKEN_DIR:-}}" = {auth_dir}; '
        f"exec {_PY} {helper}"
    )
    endpoint = f"tcp:127.0.0.1:{XAI_CALLBACK_PORT}"
    incus.config_device_remove(LITELLM_CONTAINER, XAI_LOGIN_DEVICE, missing_ok=True)
    try:
        incus.config_device_add(
            LITELLM_CONTAINER, XAI_LOGIN_DEVICE, "proxy", {"listen": endpoint, "connect": endpoint}
        )
    except IncusError as exc:
        raise RuntimeError(
            f"Cannot listen on 127.0.0.1:{XAI_CALLBACK_PORT} on the host for the xAI login "
            f"callback: {exc}"
        ) from exc
    try:
        return incus.exec_interactive(LITELLM_CONTAINER, ["bash", "-c", script])
    finally:
        incus.config_device_remove(LITELLM_CONTAINER, XAI_LOGIN_DEVICE)


def litellm_logs(incus: Incus, account: str, *, follow: bool, lines: int = 200) -> int:
    """Display the per-account systemd journal, optionally following updates."""
    _require_running(incus)
    cmd = ["journalctl", "-u", unit(account), "-n", str(lines), "--no-pager"]
    if follow:
        cmd.append("-f")
    return incus.exec_interactive(LITELLM_CONTAINER, cmd)


def reconcile_services_acl(incus: Incus) -> bool:
    """Drop a services rule whose proxy container no longer exists; True if dropped.

    `litellm down` clears the rule, but a container removed by hand or a
    recreated bridge leaves `ip/32:port` allowed while its reservation is gone.
    DHCP can then hand that address to a loose container, which strict
    containers would be allowed to reach.
    """
    if not incus.network_acl_exists(SERVICES_ACL):
        return False
    if any(c.get("name") == LITELLM_CONTAINER for c in incus.list_containers()):
        return False
    raw = incus.network_acl_show(SERVICES_ACL)
    parsed = yaml.safe_load(raw) if isinstance(raw, str) else None
    rules = parsed.get("egress") if isinstance(parsed, dict) else None
    if not any(isinstance(r, dict) and r.get("description") == LITELLM_LABEL for r in rules or []):
        return False
    set_service(incus, LITELLM_LABEL, None)
    return True


def bridges_missing_services_acl(incus: Incus) -> list[str]:
    """Managed bridges that lack the services ACL, so strict containers cannot reach the proxy.

    A bridge with no ACLs at all is not jailbee's (an unmanaged `incusbr0`),
    and is left out. The attachment happens in `init`/`apply`, not `litellm up`.
    """
    from jailbee.network_generation import WORK_BRIDGE

    missing: list[str] = []
    for bridge in ("incusbr0", WORK_BRIDGE):
        if not incus.network_exists(bridge):
            continue
        raw = incus.network_get(bridge, "security.acls")
        attached = [a.strip() for a in raw.split(",") if a.strip()] if isinstance(raw, str) else []
        if attached and SERVICES_ACL not in attached:
            missing.append(bridge)
    return missing
