"""Diagnostics — verify Incus, profiles, GPG agent, registry mirror status."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import Session, select

from jailbee.config import Config
from jailbee.db import get_engine
from jailbee.git import detect_upstream_remote
from jailbee.global_config import GlobalConfig
from jailbee.incus import Incus, IncusError
from jailbee.init_command import LOOSE_BRIDGE
from jailbee.network import acl_name
from jailbee.profiles import profile_names
from jailbee.registry import (
    MIRROR_CONTAINER_NAME,
    MirrorStatus,
    eth0_global_ipv4,
    registry_status,
)

# Default Incus subuid mapping puts the container's root at host uid 1000000.
# runc creates a session keyring per container under this uid; the per-uid
# default kernel.keys.maxkeys=200 fills up after several concurrent containers
# and the next launch fails with a misleading "disk quota exceeded" error.
_KERNEL_KEYS_MAXKEYS = Path("/proc/sys/kernel/keys/maxkeys")
_KERNEL_KEY_USERS = Path("/proc/key-users")
_INCUS_MAPPED_ROOT_UID = 1000000
_KEYRING_MAXKEYS_RECOMMENDED = 1000
_KEYRING_USAGE_WARN_FRACTION = 0.75


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _upstream_remote_check(cfg: Config) -> CheckResult:
    """Report which remote jailbee resolved as the upstream, and which branch.

    Detection is otherwise invisible: nothing in normal output says whether
    jailbee landed on the remote the user thinks it did. Failing the check when
    the name could not be resolved matters more than the happy path — jailbee
    then falls back to the literal `origin`, which is a guess, and every
    `refs/remotes/origin/*` operation quietly degrades from there.
    """
    resolved = detect_upstream_remote(cfg.repo_root)
    if resolved is None:
        return CheckResult(
            "upstream remote",
            False,
            f"could not tell which remote is the upstream — falling back to "
            f"'{cfg.upstream_remote}'. Pick one with "
            f"`git config remote.pushDefault <name>`, or set the branch's "
            f"upstream with `git branch --set-upstream-to=<remote>/<branch>`",
        )
    return CheckResult(
        "upstream remote",
        True,
        f"'{resolved}' (default branch '{cfg.default_branch}')",
    )


def run_checks(cfg: Config, incus: Incus, *, gcfg: GlobalConfig | None = None) -> list[CheckResult]:
    """Run all diagnostic checks. Returns list of results.

    `gcfg=None` means the same defaults `load_global_config` returns for an
    absent `global.yaml`, so tests that do not care about host config need not
    build one.
    """
    if gcfg is None:
        gcfg = GlobalConfig()

    results: list[CheckResult] = []

    # 1. incus binary. Every check below that talks to Incus hangs off this:
    # with no binary there is nothing to inspect, and running them anyway
    # would repeat one root cause a dozen times, burying it.
    incus_available = shutil.which("incus") is not None
    if incus_available:
        results.append(CheckResult("incus binary", True, "found"))
    else:
        results.append(
            CheckResult("incus binary", False, "`incus` not found in PATH — install Incus")
        )

    # 1b. Kernel keyring quota for Incus's mapped root uid (issue: misleading
    # "disk quota exceeded" from runc when starting nested containers).
    results.extend(_check_kernel_keyring())

    # 2. UID/GID match
    real_uid = os.getuid()
    real_gid = os.getgid()
    if cfg.container_user.uid != real_uid or cfg.container_user.gid != real_gid:
        results.append(
            CheckResult(
                "container_user uid/gid",
                False,
                f"config has uid={cfg.container_user.uid} gid={cfg.container_user.gid}, "
                f"host has uid={real_uid} gid={real_gid}",
            )
        )
    else:
        results.append(
            CheckResult(
                "container_user uid/gid", True, f"matches host (uid={real_uid}, gid={real_gid})"
            )
        )

    # 2b. Host git repo (soft requirement — only clone-mode commands need it).
    if not (cfg.repo_root / ".git").exists():
        results.append(
            CheckResult(
                "host git repo",
                True,
                "no .git at repo_root — only `jailbee new --mount <name>` works; "
                "`jailbee new <branch>`, `jailbee git fetch`, `jailbee git checkout`, "
                "`jailbee git pull` will error",
            )
        )
    else:
        results.append(_upstream_remote_check(cfg))

    if not incus_available:
        results.append(
            CheckResult(
                "Incus-dependent checks",
                False,
                "skipped — profiles, ACL, bridge, registry mirror and port "
                "forwards all need the `incus` binary",
            )
        )

    # 3. Profiles exist
    if incus_available:
        names = profile_names(cfg)
        try:
            for p in (names.base, names.binds, *names.net_by_mode.values()):
                if not incus.profile_exists(p):
                    results.append(
                        CheckResult(f"profile {p}", False, "missing — run `jailbee init`")
                    )
                else:
                    results.append(CheckResult(f"profile {p}", True, "present"))
        except IncusError as e:
            results.append(CheckResult("profile checks", False, str(e)))

        # 4. ACL exists
        acl = acl_name(cfg)
        try:
            if incus.network_acl_exists(acl):
                results.append(CheckResult(f"ACL {acl}", True, "present"))
            else:
                results.append(CheckResult(f"ACL {acl}", False, "missing — run `jailbee init`"))
        except IncusError as e:
            results.append(CheckResult("ACL check", False, str(e)))

        # 4b. jailbee-loose bridge exists
        try:
            if incus.network_exists(LOOSE_BRIDGE):
                results.append(CheckResult(f"network {LOOSE_BRIDGE}", True, "present"))
            else:
                results.append(
                    CheckResult(
                        f"network {LOOSE_BRIDGE}",
                        False,
                        "missing — run `jailbee init`",
                    )
                )
        except IncusError as e:
            results.append(CheckResult(f"network {LOOSE_BRIDGE}", False, str(e)))

        # 4c. ...and actually carries traffic
        addressing = _check_loose_bridge_addressing(cfg, incus)
        if addressing is not None:
            results.append(addressing)

    # 5. Shared dir tree
    assert cfg.shared_dir is not None  # set by load_config
    expected = [
        "caches/pnpm-store",
        "caches/gradle",
        "chrome-pool/slots",
    ]
    if cfg.jetbrains.enabled:
        expected.append("jetbrains-config")
        if cfg.jetbrains.share_idea:
            expected.append("jetbrains-idea")
    from jailbee.agents import enabled_agent_specs

    for spec in enabled_agent_specs(cfg):
        expected.extend(spec.dir_subpaths)
    missing = [s for s in expected if not (cfg.shared_dir / s).is_dir()]
    if missing:
        results.append(
            CheckResult(
                "shared_dir tree", False, f"missing: {', '.join(missing)} — run `jailbee init`"
            )
        )
    else:
        results.append(CheckResult("shared_dir tree", True, f"present at {cfg.shared_dir}"))

    # 6. GPG agent socket — only when gpg integration is enabled
    if cfg.gpg.enabled:
        runtime = Path(f"/run/user/{cfg.container_user.uid}/gnupg")
        home_socket = Path.home() / ".gnupg" / "S.gpg-agent"
        if (runtime / "S.gpg-agent").exists():
            results.append(CheckResult("gpg-agent socket", True, str(runtime / "S.gpg-agent")))
        elif home_socket.exists():
            results.append(CheckResult("gpg-agent socket", True, str(home_socket)))
        else:
            results.append(
                CheckResult(
                    "gpg-agent socket",
                    False,
                    "not found at /run/user/<uid>/gnupg/ or ~/.gnupg/ — "
                    "start gpg-agent (`gpg --card-status`) on host",
                )
            )

    # 7. Docker registry mirror status (Incus-hosted)
    from jailbee.docker_daemon import mirror_skip_reason

    skip_reason = mirror_skip_reason(cfg, gcfg)
    if skip_reason is not None:
        # Reported rather than omitted: the user should see that the gate
        # decided something, and which of the two reasons it was — "no docker"
        # would be a lie to someone who wrote `enabled: false`, a case the gate
        # never checks the repo for. (When the mirror *is* wanted but Incus is
        # missing, the line does drop out below; the "Incus-dependent checks"
        # result above names the mirror explicitly, so the reason is still on
        # screen exactly once.)
        results.append(CheckResult("registry mirror", True, f"not needed — {skip_reason}"))
    elif incus_available:
        try:
            rstatus = registry_status(incus)
        except IncusError as e:
            results.append(CheckResult("registry mirror", False, f"error querying: {e}"))
        else:
            if rstatus == MirrorStatus.RUNNING:
                results.append(CheckResult("registry mirror", True, "status: running"))
            elif rstatus == MirrorStatus.DEGRADED:
                results.append(
                    CheckResult(
                        "registry mirror",
                        False,
                        "status: degraded (container up, inner service inactive) — "
                        "run 'jailbee registry up' to recover",
                    )
                )
            elif rstatus == MirrorStatus.STOPPED:
                results.append(
                    CheckResult(
                        "registry mirror",
                        False,
                        "status: stopped — run 'jailbee registry up'",
                    )
                )
            else:  # MISSING
                results.append(
                    CheckResult(
                        "registry mirror",
                        False,
                        "status: missing — run 'jailbee registry up'",
                    )
                )

    # 7b. Legacy host-Docker mirror left over from installs that predate
    # the Incus-hosted registry mirror.
    if shutil.which("docker"):
        result = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"name={MIRROR_CONTAINER_NAME}",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if MIRROR_CONTAINER_NAME in result.stdout:
            results.append(
                CheckResult(
                    "legacy host-Docker mirror",
                    False,
                    f"Old host-Docker mirror detected. Remove with: "
                    f"docker rm -f {MIRROR_CONTAINER_NAME}. See CHANGELOG.",
                )
            )

    # 8. Wayland session. Specifically Wayland: `runtime_mounts` passes the
    # compositor's own socket into the container and nothing else, so a
    # bare DISPLAY has no matching socket on the container side. Reporting
    # an X11 session as a pass told users their GUI would work when
    # `jailbee ide` / `jailbee chrome` would start and never open a window.
    if os.environ.get("WAYLAND_DISPLAY"):
        results.append(CheckResult("graphical session", True, "Wayland"))
    elif os.environ.get("DISPLAY"):
        results.append(
            CheckResult(
                "graphical session",
                False,
                "X11 session — only the Wayland socket is passed into containers, "
                "so GUI launches will not display",
            )
        )
    else:
        results.append(
            CheckResult(
                "graphical session",
                False,
                "no WAYLAND_DISPLAY set — GUI launches will skip",
            )
        )

    # 9. GitHub CLI integration (only when github.enabled)
    results.extend(_check_github(cfg))

    # 10. Egress pool auto-refresh subsystem
    results.extend(_check_egress_pool(cfg))

    # 11. The one surviving piece of pre-1.0 compatibility: a repo whose config
    # still lives in `.gie/`. Everything else `gie`-era — the migrator, the
    # console script, the /etc/hosts sentinel, the data symlink — was removed
    # in 1.1.0, so there is no host state left to inspect and no `jailbee
    # migrate` to recommend. This is a plain file check, which is why it sits
    # outside the `incus_available` gate the removed version needed.
    if (cfg.repo_root / ".gie" / "config.yaml").is_file():
        results.append(
            CheckResult(
                "legacy repo config",
                False,
                "reading .gie/config.yaml, deprecated and removed in 2.0.0 — "
                "run `git mv .gie .jailbee` in this repo",
            )
        )
    else:
        results.append(CheckResult("legacy repo config", True, "none"))

    # 12. Config-declared forwards that never got attached — the container
    # predates the entry, or an `apply` was skipped. Only meaningful when the
    # repo declares any. Needs the `incus` binary like every other check in
    # this block, so it lives inside the same gate — otherwise a host with no
    # Incus and a declared `host_ports` got a second, redundant red line for
    # a cause the "Incus-dependent checks" line above already reported once.
    if incus_available and cfg.host_ports:
        from jailbee.lifecycle import list_containers as _list_infos
        from jailbee.ports import entry_device, list_forwards

        wanted = {entry_device(e)[0] for e in cfg.host_ports}
        missing_forwards: list[str] = []
        try:
            infos = _list_infos(cfg, incus)
            # One `incus list` for every container's forwards, instead of one
            # per container — `list_forwards` exists for exactly this.
            by_container = list_forwards(incus, [ci.name for ci in infos])
            for ci in infos:
                present = {f.device for f in by_container.get(ci.name, [])}
                for device in sorted(wanted - present):
                    missing_forwards.append(f"{ci.name}:{device}")
        except Exception as e:
            results.append(CheckResult("port forwards", False, f"could not inspect: {e}"))
        else:
            if missing_forwards:
                results.append(
                    CheckResult(
                        "port forwards",
                        False,
                        f"not attached: {', '.join(missing_forwards)} — run `jailbee apply`",
                    )
                )
            else:
                results.append(
                    CheckResult("port forwards", True, f"{len(wanted)} declared, all attached")
                )

    return results


def _check_kernel_keyring(
    maxkeys_path: Path | None = None,
    key_users_path: Path | None = None,
) -> list[CheckResult]:
    """Verify the host kernel keyring quota suffices for concurrent containers.

    Returns an empty list when /proc keyring files are unavailable (e.g. on
    a kernel built without CONFIG_KEYS). Otherwise emits one CheckResult.
    Warns when `kernel.keys.maxkeys` is below the recommended floor or when
    the Incus-mapped root uid is already close to its per-uid quota.
    """
    mk = maxkeys_path if maxkeys_path is not None else _KERNEL_KEYS_MAXKEYS
    ku = key_users_path if key_users_path is not None else _KERNEL_KEY_USERS

    if not mk.exists() or not ku.exists():
        return []

    try:
        maxkeys = int(mk.read_text().strip())
    except (OSError, ValueError):
        return []

    used, quota = _parse_key_users_for_uid(ku, _INCUS_MAPPED_ROOT_UID)

    sysctl_hint = (
        "Raise host kernel keyring limits — add to /etc/sysctl.d/99-jailbee-keys.conf:\n"
        "  kernel.keys.maxkeys=2000\n"
        "  kernel.keys.maxbytes=2000000\n"
        "  kernel.keys.root_maxkeys=2000\n"
        "  kernel.keys.root_maxbytes=2000000\n"
        "then run `sudo sysctl --system`. "
        "See docs/installation.md → 'Kernel keyring limits'."
    )

    if maxkeys < _KEYRING_MAXKEYS_RECOMMENDED:
        return [
            CheckResult(
                name="kernel keyring quota",
                ok=False,
                detail=(
                    f"kernel.keys.maxkeys={maxkeys} is below the recommended "
                    f"{_KEYRING_MAXKEYS_RECOMMENDED} for running multiple concurrent "
                    f"containers. When the per-uid quota is hit, `incus launch` and "
                    f"nested Docker fail with a misleading 'disk quota exceeded' "
                    f"error from runc. " + sysctl_hint
                ),
            )
        ]

    if used is not None and quota is not None and quota > 0:
        if used / quota >= _KEYRING_USAGE_WARN_FRACTION:
            return [
                CheckResult(
                    name="kernel keyring quota",
                    ok=False,
                    detail=(
                        f"uid {_INCUS_MAPPED_ROOT_UID} (Incus mapped root) keyring "
                        f"usage {used}/{quota} is near the limit. Starting more "
                        f"containers may fail with 'disk quota exceeded' from runc. " + sysctl_hint
                    ),
                )
            ]
        return [
            CheckResult(
                name="kernel keyring quota",
                ok=True,
                detail=(f"maxkeys={maxkeys}, uid {_INCUS_MAPPED_ROOT_UID} usage={used}/{quota}"),
            )
        ]

    return [
        CheckResult(
            name="kernel keyring quota",
            ok=True,
            detail=(
                f"maxkeys={maxkeys}, no uid {_INCUS_MAPPED_ROOT_UID} entry yet "
                f"(no Incus containers have started under the mapped root)"
            ),
        )
    ]


def _parse_key_users_for_uid(path: Path, uid: int) -> tuple[int | None, int | None]:
    """Return (used, maxkeys) for `uid` from /proc/key-users, or (None, None).

    Line format (kernel docs):
        UID:  Usage   nkeys/nikeys   qnkeys/maxkeys   qnbytes/maxbytes
    Example:
        1000000:   199 199/199 199/200 6151/20000
    """
    try:
        content = path.read_text()
    except OSError:
        return None, None

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        head, sep, rest = stripped.partition(":")
        if not sep:
            continue
        try:
            line_uid = int(head)
        except ValueError:
            continue
        if line_uid != uid:
            continue
        parts = rest.split()
        # parts: ["199", "199/199", "199/200", "6151/20000"]
        if len(parts) < 3 or "/" not in parts[2]:
            return None, None
        used_str, _, quota_str = parts[2].partition("/")
        try:
            return int(used_str), int(quota_str)
        except ValueError:
            return None, None
    return None, None


def _check_egress_pool(cfg: Config) -> list[CheckResult]:
    """Doctor checks for the egress pool refresh subsystem.

    Reports the timer state, this repo's last refresh status, and the
    current pool size. Empty refresh_state row is flagged as "never run".
    """
    from datetime import UTC, datetime, timedelta

    from jailbee.db.models import PoolIP, RefreshState

    results: list[CheckResult] = []

    proc = subprocess.run(
        ["systemctl", "--user", "is-active", "jailbee-net-refresh.timer"],
        capture_output=True,
        text=True,
        check=False,
    )
    timer_state = proc.stdout.strip() or "unknown"
    results.append(
        CheckResult(
            name="net refresh timer",
            ok=(timer_state == "active"),
            detail=(
                timer_state
                if timer_state == "active"
                else f"{timer_state} — run `jailbee init` to install/enable"
            ),
        )
    )

    with Session(get_engine()) as session:
        state = session.get(RefreshState, cfg.container_prefix)
        pool_rows = session.exec(
            select(PoolIP).where(PoolIP.container_prefix == cfg.container_prefix)
        ).all()

    if state is None:
        results.append(
            CheckResult(
                name="pool refresh state",
                ok=False,
                detail="no refresh has run yet — run `jailbee net refresh`",
            )
        )
    else:
        age = datetime.now(UTC) - state.last_refresh_at
        results.append(
            CheckResult(
                name="pool last refresh",
                ok=(state.last_refresh_status == "ok" and age < timedelta(minutes=5)),
                detail=(
                    f"{int(age.total_seconds())}s ago, "
                    f"status={state.last_refresh_status}"
                    + (f" — {state.last_error_msg}" if state.last_error_msg else "")
                ),
            )
        )

    hostnames = {r.hostname for r in pool_rows}
    results.append(
        CheckResult(
            name="pool size",
            ok=len(pool_rows) > 0,
            detail=f"{len(pool_rows)} IPs across {len(hostnames)} hostnames",
        )
    )
    return results


def _check_loose_bridge_addressing(cfg: Config, incus: Incus) -> CheckResult | None:
    """Report when nothing running on the loose bridge has an IPv4 address.

    The bridge existing says nothing about whether it carries traffic, and
    the difference is expensive to diagnose from the symptoms: a host
    firewall that drops DHCP to the bridge — UFW ships a silent DROP for
    it, and a rule naming a since-renamed interface leaves exactly that —
    produces containers with a working IPv6 address (kernel autoconfigures
    it from router advertisements, which need nothing inbound) and no IPv4,
    which surfaces much later as `apt-get` failing to resolve anything.

    Returns ``None`` when there is nothing on the bridge to judge by; an
    absent verdict beats a fabricated one. A container that only just
    started may not have its lease yet, so this reports a problem only when
    *no* container on the bridge has an address.
    """
    loose_profile = profile_names(cfg).net_loose
    try:
        containers = incus.list_containers()
    except IncusError as e:
        return CheckResult(f"network {LOOSE_BRIDGE} addressing", False, str(e))

    on_bridge = [
        c
        for c in containers
        if c.get("status") == "Running"
        and (c.get("name") == MIRROR_CONTAINER_NAME or loose_profile in (c.get("profiles") or []))
    ]
    if not on_bridge:
        return None

    addressed = [c for c in on_bridge if eth0_global_ipv4(c)]
    if addressed:
        return CheckResult(
            f"network {LOOSE_BRIDGE} addressing",
            True,
            f"{len(addressed)}/{len(on_bridge)} running containers have an IPv4 address",
        )
    names = ", ".join(sorted(str(c.get("name")) for c in on_bridge))
    return CheckResult(
        f"network {LOOSE_BRIDGE} addressing",
        False,
        f"no IPv4 on {names} — the bridge exists but hands out no addresses. "
        f"A host firewall blocking DHCP to it is the usual cause (a rule naming "
        f"a renamed interface leaves exactly this). See docs/installation.md, "
        f"'Host networking'. A container that just started may simply not have "
        f"its lease yet.",
    )


def _check_github(cfg: Config) -> list[CheckResult]:
    """Doctor checks for the github integration.

    Empty list when github.enabled=false. One info-level CheckResult
    when enabled but this repo's container_prefix has no token entry
    (legitimate "this repo doesn't use gh" state). Three checks when a
    token is in scope: global.yaml perms, non-empty value, PAT shape
    heuristic.

    The token contents are never written into any returned
    CheckResult.detail string.
    """
    if not cfg.github.enabled:
        return []

    secret = cfg.github.api_tokens.get(cfg.container_prefix)
    if secret is None:
        return [
            CheckResult(
                name="github token",
                ok=True,
                detail=(
                    f"no token configured for container_prefix "
                    f"'{cfg.container_prefix}' — gh will not authenticate "
                    f"in this repo's containers (add an entry under "
                    f"github.api_tokens to enable)"
                ),
            ),
        ]

    from jailbee.global_config import default_global_config_path

    results: list[CheckResult] = []

    gy = default_global_config_path()
    if gy.exists():
        mode = gy.stat().st_mode & 0o777
        if mode & 0o077 != 0:
            results.append(
                CheckResult(
                    name="github global.yaml perms",
                    ok=False,
                    detail=(
                        f"~/.config/jailbee/global.yaml has insecure perms "
                        f"(0{mode:03o}) — run `chmod 600 {gy}`"
                    ),
                )
            )
        else:
            results.append(
                CheckResult(
                    name="github global.yaml perms",
                    ok=True,
                    detail="0600",
                )
            )
    else:
        results.append(
            CheckResult(
                name="github global.yaml perms",
                ok=False,
                detail=f"{gy} does not exist",
            )
        )

    token = secret.get_secret_value().strip()
    if not token:
        results.append(
            CheckResult(
                name="github token non-empty",
                ok=False,
                detail=f"github.api_tokens['{cfg.container_prefix}'] is empty",
            )
        )
        return results

    results.append(
        CheckResult(
            name="github token non-empty",
            ok=True,
            detail="non-empty",
        )
    )

    if token.startswith("github_pat_"):
        results.append(
            CheckResult(
                name="github token shape",
                ok=True,
                detail="fine-grained PAT",
            )
        )
    elif token.startswith("ghp_"):
        results.append(
            CheckResult(
                name="github token shape",
                ok=False,
                detail=(
                    "classic PAT — does not scope to repos; "
                    "recommend a fine-grained PAT (`github_pat_*`) "
                    "scoped to your work repos. See docs/git-bridge.md → "
                    "'GitHub CLI (gh) inside containers'."
                ),
            )
        )
    else:
        results.append(
            CheckResult(
                name="github token shape",
                ok=False,
                detail=(
                    "unknown token format — confirm the value is a "
                    "GitHub PAT (`github_pat_*` or `ghp_*`)"
                ),
            )
        )

    return results
