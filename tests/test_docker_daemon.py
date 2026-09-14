"""Tests for docker_daemon module (rpardini proxy era).

The provisioning script `apply_docker_proxy` sends into a container is
executed here by a real bash against a fake root, so the restart guard is
tested rather than its spelling: the bug it exists to prevent is a dockerd
restart on an apply that changed nothing, and no amount of string matching
proves that a shell conditional actually took the other branch. Nothing
here touches Incus, Docker or the network.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jailbee.docker_daemon import (
    RESTART_NEEDED_MARKER,
    apply_docker_proxy,
    compute_mirror_endpoint,
    mirror_skip_reason,
    mirror_wanted,
    render_proxy_conf,
    restart_dockerd,
)
from jailbee.global_config import DockerRegistryMirror, GlobalConfig
from tests.conftest import make_cfg

CA_DIR = "/usr/local/share/ca-certificates"
DROPIN_DIR = "/etc/systemd/system/docker.service.d"
DAEMON_JSON = "/etc/docker/daemon.json"

# Every external binary the script may reach for. Replaced by loggers so a
# test can assert on what the script decided to run.
_FAKE_TOOLS = ("update-ca-certificates", "keytool", "docker")

_FAKE_TOOL = '#!/bin/sh\nprintf \'%s %s\\n\' "${0##*/}" "$*" >> "$JB_CALL_LOG"\n'

# systemctl also has to answer, not just record: the script asks it whether
# dockerd is running and when it started. `JB_DOCKER_STARTED` empty means the
# service is not active.
_FAKE_SYSTEMCTL = """\
#!/bin/sh
printf '%s %s\\n' "${0##*/}" "$*" >> "$JB_CALL_LOG"
case "$1" in
  is-active) [ -n "$JB_DOCKER_STARTED" ] ;;
  show) printf '%s\\n' "$JB_DOCKER_STARTED" ;;
esac
"""

# dockerd entered its current run long before anything a test writes.
LONG_AGO = "2000-01-01 00:00:00 UTC"


def _script(ca_cert_pem: str = "mirror-ca-v1", port: int = 3128) -> str:
    """The bash the given inputs make `apply_docker_proxy` send."""
    incus = MagicMock()
    apply_docker_proxy(incus, "myrepo-feat-x", ca_cert_pem=ca_cert_pem, port=port)
    return str(incus.exec.call_args.args[1][2])


def _sandboxed(script: str, root: Path) -> str:
    """Re-point the script's absolute targets into `root`.

    The trailing assertion is the safety net: an absolute path this helper
    does not know about would otherwise be written for real (or, as a
    non-root test user, fail with a confusing permission error).
    """
    probe = script
    for path in (CA_DIR, DROPIN_DIR, DAEMON_JSON):
        script = script.replace(path, f"{root}{path}")
        probe = probe.replace(path, "<sandboxed>")
    leftovers = [tok for tok in ("/etc/", "/usr/") if tok in probe]
    assert not leftovers, f"unsandboxed absolute path in script: {leftovers}"
    return script


class _Run:
    """One execution of a generated script against a fake root."""

    def __init__(self, calls: list[str], stdout: str) -> None:
        self.calls = calls
        self.stdout = stdout

    @property
    def restarted(self) -> bool:
        return "systemctl restart docker" in self.calls

    @property
    def restart_needed(self) -> bool:
        return RESTART_NEEDED_MARKER in self.stdout.splitlines()


def _bash(
    script: str,
    root: Path,
    *,
    docker_installed: bool = True,
    docker_started: str | None = LONG_AGO,
) -> _Run:
    """Execute a generated script with real bash; report what it ran."""
    bin_dir = root / "fakebin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = root / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    for tool in _FAKE_TOOLS:
        if tool == "docker" and not docker_installed:
            continue
        exe = bin_dir / tool
        exe.write_text(_FAKE_TOOL)
        exe.chmod(0o755)
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(_FAKE_SYSTEMCTL)
    systemctl.chmod(0o755)
    log = root / "calls.log"
    log.write_text("")

    proc = subprocess.run(
        ["bash", "-c", _sandboxed(script, root)],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "TMPDIR": str(tmp_dir),
            "JB_CALL_LOG": str(log),
            "JB_DOCKER_STARTED": docker_started or "",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    return _Run(log.read_text().splitlines(), proc.stdout)


def _run_script(
    root: Path,
    *,
    ca_cert_pem: str = "mirror-ca-v1",
    port: int = 3128,
    docker_installed: bool = True,
    docker_started: str | None = LONG_AGO,
) -> _Run:
    """Run what `apply_docker_proxy` sends for these inputs."""
    return _bash(
        _script(ca_cert_pem, port),
        root,
        docker_installed=docker_installed,
        docker_started=docker_started,
    )


def _run_restart_script(root: Path, *, docker_installed: bool = True) -> _Run:
    """Run what `restart_dockerd` sends."""
    incus = MagicMock()
    restart_dockerd(incus, "myrepo-feat-x")
    script = str(incus.exec.call_args.args[1][2])
    return _bash(script, root, docker_installed=docker_installed)


def _dockerd_restarted(root: Path) -> str:
    """Backdate the installed files and report a dockerd start just after them.

    Both timestamps have second resolution, so a file written in the same
    second as the simulated restart is indistinguishable from one written
    before it. Backdating what is already installed keeps the two apart
    without making the test wait a second.
    """
    past = 1_000_000_000  # 2001-09-09, comfortably before any later write
    for f in (
        root / CA_DIR.lstrip("/") / "jailbee-registry-mirror.crt",
        root / DROPIN_DIR.lstrip("/") / "http-proxy.conf",
    ):
        os.utime(f, (past, past))
    return f"@{past + 1}"


def _installed_conf(root: Path) -> str:
    return (root / DROPIN_DIR.lstrip("/") / "http-proxy.conf").read_text()


def _installed_ca(root: Path) -> str:
    return (root / CA_DIR.lstrip("/") / "jailbee-registry-mirror.crt").read_text()


def test_compute_mirror_endpoint_returns_mirror_container_ip(tmp_path):
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": "jailbee-registry-mirror",
            "status": "Running",
            "state": {
                "network": {
                    "eth0": {
                        "addresses": [
                            {
                                "family": "inet",
                                "address": "10.234.216.42",
                                "netmask": "24",
                                "scope": "global",
                            },
                            {"family": "inet6", "address": "fe80::1", "scope": "link"},
                        ]
                    }
                }
            },
        }
    ]
    gcfg = GlobalConfig(
        docker_registry_mirror=DockerRegistryMirror(port=3128, data_dir=tmp_path),
    )

    ip, port = compute_mirror_endpoint(incus, gcfg)

    assert (ip, port) == ("10.234.216.42", 3128)


def test_compute_mirror_endpoint_uses_configured_port(tmp_path):
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": "jailbee-registry-mirror",
            "status": "Running",
            "state": {
                "network": {
                    "eth0": {
                        "addresses": [
                            {
                                "family": "inet",
                                "address": "10.234.216.42",
                                "scope": "global",
                            },
                        ]
                    }
                }
            },
        }
    ]
    gcfg = GlobalConfig(
        docker_registry_mirror=DockerRegistryMirror(port=4000, data_dir=tmp_path),
    )

    _, port = compute_mirror_endpoint(incus, gcfg)
    assert port == 4000


def test_compute_mirror_endpoint_raises_when_mirror_missing(tmp_path):
    incus = MagicMock()
    incus.list_containers.return_value = []
    gcfg = GlobalConfig(
        docker_registry_mirror=DockerRegistryMirror(data_dir=tmp_path),
    )

    with pytest.raises(ValueError, match=r"Run 'jailbee registry up'"):
        compute_mirror_endpoint(incus, gcfg)


def test_compute_mirror_endpoint_raises_when_mirror_stopped(tmp_path):
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": "jailbee-registry-mirror", "status": "Stopped", "state": None}
    ]
    gcfg = GlobalConfig(
        docker_registry_mirror=DockerRegistryMirror(data_dir=tmp_path),
    )

    with pytest.raises(ValueError, match=r"Run 'jailbee registry up'"):
        compute_mirror_endpoint(incus, gcfg)


def test_compute_mirror_endpoint_raises_when_no_ipv4_assigned(tmp_path):
    """Just-started container with no IPv4 lease yet."""
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": "jailbee-registry-mirror",
            "status": "Running",
            "state": {"network": {"eth0": {"addresses": []}}},
        }
    ]
    gcfg = GlobalConfig(
        docker_registry_mirror=DockerRegistryMirror(data_dir=tmp_path),
    )

    with pytest.raises(ValueError, match=r"no IPv4 address"):
        compute_mirror_endpoint(incus, gcfg)


def test_render_proxy_conf_includes_systemd_section_header():
    out = render_proxy_conf("jailbee-registry-mirror.incus", 3128)
    assert out.startswith("[Service]\n")


def test_render_proxy_conf_sets_both_http_and_https_proxy():
    out = render_proxy_conf("jailbee-registry-mirror.incus", 3128)
    assert 'Environment="HTTPS_PROXY=http://jailbee-registry-mirror.incus:3128"' in out
    assert 'Environment="HTTP_PROXY=http://jailbee-registry-mirror.incus:3128"' in out


def test_render_proxy_conf_sets_no_proxy_for_local_traffic():
    """Local addresses + .incus zone must NOT route through the proxy."""
    out = render_proxy_conf("jailbee-registry-mirror.incus", 3128)
    assert 'Environment="NO_PROXY=localhost,127.0.0.1,incusbr0,*.incus"' in out


def test_render_proxy_conf_ends_with_newline():
    """systemd parses unit files line-by-line; trailing newline keeps diffs clean."""
    out = render_proxy_conf("jailbee-registry-mirror.incus", 3128)
    assert out.endswith("\n")


def test_apply_docker_proxy_runs_single_bash_invocation():
    incus = MagicMock()
    apply_docker_proxy(
        incus,
        "myrepo-feat-x",
        ca_cert_pem="-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n",
        port=3128,
    )

    assert incus.exec.call_count == 1
    name, argv = incus.exec.call_args.args
    assert name == "myrepo-feat-x"
    assert argv[:2] == ["bash", "-c"]


def test_apply_docker_proxy_script_writes_ca_cert_via_heredoc():
    incus = MagicMock()
    apply_docker_proxy(
        incus,
        "myrepo-feat-x",
        ca_cert_pem=("-----BEGIN CERTIFICATE-----\nfake-cert\n-----END CERTIFICATE-----\n"),
        port=3128,
    )

    script = incus.exec.call_args.args[1][2]
    assert "/usr/local/share/ca-certificates/jailbee-registry-mirror.crt" in script
    assert "fake-cert" in script
    assert "update-ca-certificates" in script


def test_apply_docker_proxy_deletes_its_own_alias_before_reimporting():
    """The keystore delete is what makes the re-import idempotent.

    `keytool -importcert` fails on an alias that already exists, so a second
    `jailbee apply` against a container that already trusts the mirror would
    leave the JDK on a stale certificate if the delete ran after — or not at
    all. Ordering is the assertion; presence alone would not catch a swap.
    """
    incus = MagicMock()
    apply_docker_proxy(incus, "myrepo-feat-x", ca_cert_pem="cert", port=3128)

    script = incus.exec.call_args.args[1][2]
    assert script.index("-delete -noprompt -alias jailbee-registry-mirror") < script.index(
        "-importcert"
    )


def test_apply_docker_proxy_writes_systemd_dropin_atomically():
    incus = MagicMock()
    apply_docker_proxy(
        incus,
        "myrepo-feat-x",
        ca_cert_pem="cert",
        port=3128,
    )

    script = incus.exec.call_args.args[1][2]
    assert "/etc/systemd/system/docker.service.d/http-proxy.conf" in script
    assert "mktemp" in script  # atomic write pattern
    assert "mv " in script


def test_first_apply_installs_the_files_and_asks_for_a_restart(tmp_path: Path):
    """A container that has never seen the mirror: both files are new, and
    only a restart makes the running dockerd read them."""
    run = _run_script(tmp_path)

    assert _installed_ca(tmp_path).startswith("mirror-ca-v1")
    assert "HTTPS_PROXY=http://jailbee-registry-mirror.incus:3128" in _installed_conf(tmp_path)
    assert run.restart_needed


def test_installing_the_proxy_never_restarts_dockerd_by_itself(tmp_path: Path):
    """The restart stops every Docker container the user is running, so it is
    the caller's call to make (and `jailbee apply` asks first) — this script
    only reports that one is due."""
    run = _run_script(tmp_path)

    assert not run.restarted
    assert "systemctl daemon-reload" not in run.calls


def test_repeated_apply_with_identical_inputs_needs_no_restart(tmp_path: Path):
    """The bug: `jailbee apply` runs this for every running container, so an
    unconditional restart killed every Docker container in the repo's whole
    fleet on an apply that changed nothing."""
    first = _run_script(tmp_path)
    # dockerd has since restarted, so it is running what is on disk.
    second = _run_script(tmp_path, docker_started=_dockerd_restarted(tmp_path))

    assert first.restart_needed
    assert not second.restart_needed


def test_a_declined_restart_is_still_due_on_the_next_apply(tmp_path: Path):
    """The user can say no. Deciding on file contents alone would then leave
    dockerd on the old config forever, because the next apply finds both
    files already correct — so the question is whether the *running* dockerd
    predates them, not whether this run rewrote them."""
    first = _run_script(tmp_path)
    second = _run_script(tmp_path)

    assert first.restart_needed
    assert second.restart_needed


def test_a_stopped_dockerd_needs_no_restart(tmp_path: Path):
    """Nothing to disturb, and it reads both files when it next starts."""
    run = _run_script(tmp_path, docker_started=None)

    assert not run.restart_needed
    assert _installed_ca(tmp_path).startswith("mirror-ca-v1")


def test_unchanged_apply_still_refreshes_the_ca_bundle(tmp_path: Path):
    """Deliberate: the per-file compare proves the source cert is current,
    not that the generated bundle still contains it. `update-ca-certificates`
    is the cheap self-heal for that and restarts nothing."""
    _run_script(tmp_path)
    second = _run_script(tmp_path, docker_started=_dockerd_restarted(tmp_path))

    assert any(c.startswith("update-ca-certificates") for c in second.calls)
    assert not second.restart_needed


def test_unchanged_apply_leaves_the_installed_files_in_place(tmp_path: Path):
    """Skipping the write must not mean skipping the file."""
    _run_script(tmp_path)
    conf_before, ca_before = _installed_conf(tmp_path), _installed_ca(tmp_path)

    _run_script(tmp_path, docker_started=_dockerd_restarted(tmp_path))

    assert _installed_conf(tmp_path) == conf_before
    assert _installed_ca(tmp_path) == ca_before


def test_a_changed_proxy_port_makes_a_restart_due_again(tmp_path: Path):
    """A real change still has to reach dockerd — systemd re-reads the
    drop-in's `Environment=` only on unit start."""
    _run_script(tmp_path, port=3128)
    started = _dockerd_restarted(tmp_path)
    second = _run_script(tmp_path, port=4000, docker_started=started)

    assert second.restart_needed
    assert "jailbee-registry-mirror.incus:4000" in _installed_conf(tmp_path)


def test_a_regenerated_ca_makes_a_restart_due_again(tmp_path: Path):
    """Go caches the system cert pool per process, so a new mirror CA is
    invisible to a dockerd that keeps running."""
    _run_script(tmp_path, ca_cert_pem="mirror-ca-v1")
    started = _dockerd_restarted(tmp_path)
    second = _run_script(tmp_path, ca_cert_pem="mirror-ca-v2", docker_started=started)

    assert second.restart_needed
    assert _installed_ca(tmp_path).startswith("mirror-ca-v2")


def test_removing_a_stale_daemon_json_makes_a_restart_due(tmp_path: Path):
    """Removing the pre-rpardini daemon.json changes dockerd's configuration,
    and it is the one change no file timestamp records."""
    _run_script(tmp_path)
    started = _dockerd_restarted(tmp_path)
    daemon_json = tmp_path / DAEMON_JSON.lstrip("/")
    daemon_json.parent.mkdir(parents=True, exist_ok=True)
    daemon_json.write_text('{"registry-mirrors": ["http://old:5000"]}')

    second = _run_script(tmp_path, docker_started=started)

    assert not daemon_json.exists()
    assert second.restart_needed


def test_a_container_without_docker_is_never_asked_to_restart(tmp_path: Path):
    """No docker.service to restart: the CA and drop-in still install, and
    the exec must not fail under `set -e`."""
    run = _run_script(tmp_path, docker_installed=False)

    assert not run.restart_needed
    assert _installed_ca(tmp_path).startswith("mirror-ca-v1")


def test_apply_docker_proxy_reports_a_due_restart_to_its_caller(tmp_path: Path):
    incus = MagicMock()
    incus.exec.return_value = f"some other output\n{RESTART_NEEDED_MARKER}\n"

    assert apply_docker_proxy(incus, "myrepo-feat-x", ca_cert_pem="cert", port=3128) is True


def test_apply_docker_proxy_reports_nothing_due_when_dockerd_is_current():
    incus = MagicMock()
    incus.exec.return_value = "Certificate was added to keystore\n"

    assert apply_docker_proxy(incus, "myrepo-feat-x", ca_cert_pem="cert", port=3128) is False


def test_restart_dockerd_reloads_the_unit_before_restarting_it(tmp_path: Path):
    """The drop-in is a unit file: without `daemon-reload` systemd restarts
    docker.service from the definition it already had."""
    run = _run_restart_script(tmp_path)

    assert run.calls == ["systemctl daemon-reload", "systemctl restart docker"]


def test_restart_dockerd_is_a_no_op_without_docker(tmp_path: Path):
    """`systemctl restart docker` on a container with no docker.service fails
    with exit 5 and would abort the exec under `set -e`."""
    run = _run_restart_script(tmp_path, docker_installed=False)

    assert run.calls == []


def _gcfg(enabled, tmp_path):
    return GlobalConfig(
        docker_registry_mirror=DockerRegistryMirror(enabled=enabled, data_dir=tmp_path),
    )


def _docker_cfg(tmp_path):
    return make_cfg(tmp_path / "repo", golden={"stacks": {"docker": True}})


def _plain_cfg(tmp_path):
    return make_cfg(tmp_path / "repo")


def test_mirror_wanted_auto_follows_the_docker_stack(tmp_path):
    assert mirror_wanted(_docker_cfg(tmp_path), _gcfg("auto", tmp_path)) is True


def test_mirror_wanted_auto_is_false_without_docker(tmp_path):
    assert mirror_wanted(_plain_cfg(tmp_path), _gcfg("auto", tmp_path)) is False


def test_mirror_wanted_auto_sees_docker_in_extra_apt_packages(tmp_path):
    """`golden.extra_apt_packages` is staged by 05-extra-apt.sh, so Docker can
    reach the image without the `docker` snippet ever resolving."""
    cfg = make_cfg(tmp_path / "repo", golden={"extra_apt_packages": ["docker.io"]})
    assert mirror_wanted(cfg, _gcfg("auto", tmp_path)) is True


def test_mirror_wanted_auto_sees_extra_registries_as_intent(tmp_path):
    """Naming upstream registries to cache is the clearest statement of intent
    there is; without this the key would be an inert no-op (both push sites
    are gated on the endpoint)."""
    cfg = make_cfg(
        tmp_path / "repo",
        docker_registry_mirror={"extra_registries": ["x.dkr.ecr.eu-north-1.amazonaws.com"]},
    )
    assert mirror_wanted(cfg, _gcfg("auto", tmp_path)) is True


def test_mirror_wanted_auto_sees_the_ecr_stack(tmp_path):
    """`stacks.ecr` stages a Docker credential helper without the `docker`
    snippet, so image content alone would miss it."""
    cfg = make_cfg(tmp_path / "repo", golden={"stacks": {"ecr": True}})
    assert mirror_wanted(cfg, _gcfg("auto", tmp_path)) is True


def test_mirror_wanted_false_wins_over_every_auto_signal(tmp_path):
    """`false` must short-circuit before detection, not merely outvote it."""
    cfg = make_cfg(
        tmp_path / "repo",
        golden={"extra_apt_packages": ["docker.io"], "stacks": {"docker": True, "ecr": True}},
        docker_registry_mirror={"extra_registries": ["x.dkr.ecr.eu-north-1.amazonaws.com"]},
    )
    assert mirror_wanted(cfg, _gcfg(False, tmp_path)) is False


def test_mirror_wanted_true_forces_on_without_docker(tmp_path):
    """The escape hatch for a repo whose Docker install jailbee cannot see."""
    assert mirror_wanted(_plain_cfg(tmp_path), _gcfg(True, tmp_path)) is True


def test_mirror_wanted_false_wins_over_the_docker_stack(tmp_path):
    assert mirror_wanted(_docker_cfg(tmp_path), _gcfg(False, tmp_path)) is False


def test_mirror_skip_reason_is_none_when_the_mirror_is_wanted(tmp_path):
    """A reason exists only for the two "no" cases; None is the caller's cue
    that the mirror really is expected to be there."""
    assert mirror_skip_reason(_docker_cfg(tmp_path), _gcfg("auto", tmp_path)) is None
    assert mirror_skip_reason(_plain_cfg(tmp_path), _gcfg(True, tmp_path)) is None


def test_mirror_skip_reason_distinguishes_the_two_no_cases(tmp_path):
    """`enabled: false` never looks at the repo, so it must not be reported as
    "no docker detected" — that is a fact about a check that never ran."""
    disabled = mirror_skip_reason(_docker_cfg(tmp_path), _gcfg(False, tmp_path))
    undetected = mirror_skip_reason(_plain_cfg(tmp_path), _gcfg("auto", tmp_path))

    assert disabled is not None and "enabled: false" in disabled
    assert undetected is not None and "no docker detected" in undetected
    assert disabled != undetected


def test_enabled_is_read_in_exactly_two_modules():
    """`mirror_wanted` is the only reader; a call site that peeks at the raw
    field would silently ignore `auto` and re-introduce the old behaviour.

    The scan matches the full attribute access including its receiver
    (`gcfg.docker_registry_mirror.enabled`), not the bare field name — a
    docstring or comment is free to name the flag in prose (e.g.
    `golden.py` explains what the flag is for) without that counting as a
    read. Every migrated call site accesses the field through a variable
    named `gcfg`, so this is also the pattern a new offending read would
    have to reproduce to actually work.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "jailbee"
    offenders = sorted(
        p.name
        for p in src.rglob("*.py")
        if "gcfg.docker_registry_mirror.enabled" in p.read_text()
        and p.name not in {"global_config.py", "docker_daemon.py"}
    )
    assert offenders == []
