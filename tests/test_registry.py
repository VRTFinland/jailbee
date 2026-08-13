"""Tests for the Docker registry mirror lifecycle (Incus-hosted)."""

import re
from unittest.mock import MagicMock

import pytest

from jailbee.global_config import GlobalConfig
from jailbee.incus import IncusError
from jailbee.registry import (
    MIRROR_CONTAINER_NAME,
    MIRROR_SERVICE_NAME,
    MirrorStatus,
    _read_provision_text,
    _service_state,
    _wait_for_service_active,
    apply_mirror_registries,
    registry_down,
    registry_status,
    registry_up,
)


def test_mirror_container_name_unchanged():
    """Name is preserved across the host-Docker→Incus migration so users
    who know the name from before find it again on the Incus side."""
    assert MIRROR_CONTAINER_NAME == "jailbee-registry-mirror"


def test_status_running_when_container_running_and_service_active():
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": MIRROR_CONTAINER_NAME, "status": "Running"},
    ]
    incus.exec.return_value = "active\n"

    assert registry_status(incus) == MirrorStatus.RUNNING


def test_status_degraded_when_container_running_but_service_inactive():
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": MIRROR_CONTAINER_NAME, "status": "Running"},
    ]
    # `systemctl is-active` exits non-zero when inactive — the wrapper
    # raises IncusError; treat that as "service not up".
    incus.exec.side_effect = IncusError("`incus exec` failed (exit 3): inactive")

    assert registry_status(incus) == MirrorStatus.DEGRADED


def test_status_stopped_when_container_exists_but_not_running():
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": MIRROR_CONTAINER_NAME, "status": "Stopped"},
    ]

    assert registry_status(incus) == MirrorStatus.STOPPED
    incus.exec.assert_not_called()  # don't probe inner service on stopped container


def test_status_missing_when_container_not_in_list():
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": "other-container", "status": "Running"},
    ]

    assert registry_status(incus) == MirrorStatus.MISSING
    incus.exec.assert_not_called()


def test_up_creates_data_dirs_on_host(tmp_path):
    """The host bind-mount source dirs must exist before incus.start —
    otherwise the mirror container fails to mount /docker_mirror_cache /ca."""
    incus = MagicMock()
    incus.list_containers.return_value = []  # container missing
    incus.profile_exists.return_value = False
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )

    registry_up(incus, gcfg)

    assert (tmp_path / "registry" / "cache").is_dir()
    assert (tmp_path / "registry" / "ca").is_dir()


def test_up_full_init_when_container_missing(tmp_path):
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.profile_exists.return_value = False
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )

    registry_up(incus, gcfg)

    incus.profile_create.assert_called_once_with("jailbee-registry-mirror-profile")
    init_calls = [c for c in incus.init.call_args_list if c.args[1] == "jailbee-registry-mirror"]
    assert len(init_calls) == 1
    assert init_calls[0].args[0].startswith("images:ubuntu/26.04")
    autostart_calls = [
        c
        for c in incus.config_set.call_args_list
        if c.args == ("jailbee-registry-mirror", "boot.autostart", "true")
    ]
    assert autostart_calls
    incus.start.assert_called_with("jailbee-registry-mirror")


def test_up_profile_enables_security_nesting(tmp_path):
    """Without `security.nesting=true`, on Ubuntu 24.04+ hosts systemd 256+
    services (journald, networkd, resolved) hang at `(sd-mkuserns)` because the host's
    `kernel.apparmor_restrict_unprivileged_userns=1` blocks the nested user
    namespace they need for `DynamicUser=`/`PrivateUsers=`. Result: no IPv4
    lease, no /etc/resolv.conf, install.sh's `apt-get update` fails to
    resolve archive.ubuntu.com."""
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.profile_exists.return_value = False
    incus.network_exists.return_value = True
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )

    registry_up(incus, gcfg)

    set_yaml_calls = [
        c
        for c in incus.profile_set_yaml.call_args_list
        if c.args[0] == "jailbee-registry-mirror-profile"
    ]
    assert set_yaml_calls, "mirror profile YAML was never applied"
    yaml_body = set_yaml_calls[-1].args[1]
    assert "security.nesting" in yaml_body
    assert "true" in yaml_body  # value rendered as YAML string


def test_up_profile_carves_idmap_hole_for_host_user(tmp_path, mocker):
    """rpardini inside the mirror needs to write /ca/ca.key etc.; the
    /ca volume is bind-mounted from the host user's
    `~/.local/share/gie/registry/ca` and is therefore owned by the host
    user's UID. Without an idmap hole, container root maps to host UID
    1_000_000 and is denied write access to host-UID-53023 files
    ('genrsa: Permission denied'). Mirror profile maps host-UID → 0
    inside (1:1 to container root) so rpardini can write the dirs."""
    mocker.patch("jailbee.registry.os.getuid", return_value=53023)
    mocker.patch("jailbee.registry.os.getgid", return_value=53023)
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.profile_exists.return_value = False
    incus.network_exists.return_value = True
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )

    registry_up(incus, gcfg)

    set_yaml_calls = [
        c
        for c in incus.profile_set_yaml.call_args_list
        if c.args[0] == "jailbee-registry-mirror-profile"
    ]
    yaml_body = set_yaml_calls[-1].args[1]
    assert "raw.idmap" in yaml_body
    assert "uid 53023 0" in yaml_body
    assert "gid 53023 0" in yaml_body


def test_up_profile_puts_eth0_on_gie_loose_bridge(tmp_path):
    """Mirror needs unrestricted egress (apt → archive.ubuntu.com, podman
    pulls rpardini/docker-registry-proxy → docker.io, then proxies any
    upstream registry). incusbr0 carries the per-repo allowlist ACL at
    bridge level, which would default-deny the mirror's traffic. The
    mirror profile therefore moves eth0 to `jailbee-loose` (the ACL-free
    shared bridge), overriding the default profile's
    incusbr0 eth0 via profile-stacking precedence."""
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.profile_exists.return_value = False
    incus.network_exists.return_value = True
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )

    registry_up(incus, gcfg)

    set_yaml_calls = [
        c
        for c in incus.profile_set_yaml.call_args_list
        if c.args[0] == "jailbee-registry-mirror-profile"
    ]
    assert set_yaml_calls, "mirror profile YAML was never applied"
    yaml_body = set_yaml_calls[-1].args[1]
    assert "eth0" in yaml_body
    assert "jailbee-loose" in yaml_body
    assert "incusbr0" not in yaml_body


def test_up_creates_gie_loose_bridge_if_missing(tmp_path):
    """`jailbee registry up` is repo-config-independent and may run before
    any per-repo `gie init` has created the jailbee-loose bridge. Ensure
    registry_up itself creates the bridge when missing — otherwise
    Incus refuses to start the mirror with 'Network not found'."""
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.profile_exists.return_value = False
    incus.network_exists.return_value = False  # jailbee-loose missing
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )

    registry_up(incus, gcfg)

    incus.network_create.assert_called_once_with("jailbee-loose")


def test_up_does_not_recreate_gie_loose_when_present(tmp_path):
    """Idempotent: pre-existing jailbee-loose (from a prior `gie init` or
    `jailbee registry up`) is left untouched."""
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.profile_exists.return_value = False
    incus.network_exists.return_value = True
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )

    registry_up(incus, gcfg)

    incus.network_create.assert_not_called()


def test_up_pushes_quadlet_unit_and_runs_install_script(tmp_path):
    """install.sh must run inside the mirror container, and the Quadlet
    unit must be pushed to /tmp before install.sh runs."""
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.profile_exists.return_value = False
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )

    registry_up(incus, gcfg)

    bash_execs = [
        call
        for call in incus.exec.call_args_list
        if len(call.args) >= 2
        and call.args[0] == "jailbee-registry-mirror"
        and isinstance(call.args[1], list)
        and call.args[1][:2] == ["bash", "-c"]
    ]
    assert bash_execs, "no `bash -c` exec found — install script not run"
    script = bash_execs[0].args[1][2]
    assert "/root/jailbee-registry-proxy.container" in script
    assert "install.sh" in script
    assert "systemctl daemon-reload" in script or "systemctl start" in script
    # Guard against /tmp regressing in: /tmp gets wiped during
    # `apt-get install` inside Ubuntu 26.04 cloud images mid-script.
    assert "/tmp/jailbee-registry-proxy.container" not in script


def test_up_idempotent_when_already_running(tmp_path):
    """Second `jailbee registry up` after first must NOT re-init the container."""
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": MIRROR_CONTAINER_NAME, "status": "Running"},
    ]
    incus.profile_exists.return_value = True
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )

    registry_up(incus, gcfg)

    incus.init.assert_not_called()
    incus.start.assert_not_called()
    incus.profile_create.assert_not_called()


def test_up_starts_only_when_container_stopped(tmp_path):
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": MIRROR_CONTAINER_NAME, "status": "Stopped"},
    ]
    incus.profile_exists.return_value = True
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )

    registry_up(incus, gcfg)

    incus.init.assert_not_called()
    incus.start.assert_called_once_with(MIRROR_CONTAINER_NAME)


# ---------- DHCP static reservation (issue: stale dnsmasq lease)


def test_up_profile_pins_static_ipv4_when_gie_loose_has_cidr(tmp_path):
    """When jailbee-loose has a concrete IPv4 subnet, the mirror profile must
    include `ipv4.address` on eth0 so dnsmasq always issues the same lease.
    Without this, the mirror gets a random lease on (re)create; old lease
    records survive in dnsmasq's database and loose-mode containers
    resolve `jailbee-registry-mirror.incus` to a pre-reboot IP."""
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.profile_exists.return_value = False
    incus.network_exists.return_value = True
    incus.network_get.return_value = "10.79.115.1/24"
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )

    registry_up(incus, gcfg)

    set_yaml_calls = [
        c
        for c in incus.profile_set_yaml.call_args_list
        if c.args[0] == "jailbee-registry-mirror-profile"
    ]
    yaml_body = set_yaml_calls[-1].args[1]
    assert "ipv4.address: 10.79.115.2" in yaml_body
    incus.network_get.assert_any_call("jailbee-loose", "ipv4.address")


def test_up_profile_omits_static_ipv4_when_gie_loose_has_auto_cidr(tmp_path):
    """When jailbee-loose's ipv4.address is `auto` (Incus hasn't materialised
    a concrete subnet yet), fall back to plain DHCP — losing the
    stable-IP property but keeping the mirror operational. Strict-mode
    containers still get the correct IP via gie's /etc/hosts pin."""
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.profile_exists.return_value = False
    incus.network_exists.return_value = True
    incus.network_get.return_value = "auto"
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )

    registry_up(incus, gcfg)

    set_yaml_calls = [
        c
        for c in incus.profile_set_yaml.call_args_list
        if c.args[0] == "jailbee-registry-mirror-profile"
    ]
    yaml_body = set_yaml_calls[-1].args[1]
    assert "ipv4.address" not in yaml_body


def test_up_profile_omits_static_ipv4_when_gie_loose_has_none_cidr(tmp_path):
    """`ipv4.address: none` on the bridge means IPv4 is disabled entirely
    — no host range to pick from; profile must not invent one."""
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.profile_exists.return_value = False
    incus.network_exists.return_value = True
    incus.network_get.return_value = "none"
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )

    registry_up(incus, gcfg)

    set_yaml_calls = [
        c
        for c in incus.profile_set_yaml.call_args_list
        if c.args[0] == "jailbee-registry-mirror-profile"
    ]
    yaml_body = set_yaml_calls[-1].args[1]
    assert "ipv4.address" not in yaml_body


def test_up_restarts_running_mirror_when_live_ip_differs_from_reservation(tmp_path):
    """Existing host with a random lease (e.g. .98) upgrading into the
    reservation logic (.2 picked from jailbee-loose's CIDR) must drop the
    old lease — otherwise the mirror stays on .98 and the fix is moot."""
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": MIRROR_CONTAINER_NAME,
            "status": "Running",
            "state": {
                "network": {
                    "eth0": {
                        "addresses": [
                            {"family": "inet", "scope": "global", "address": "10.79.115.98"},
                        ],
                    },
                },
            },
        },
    ]
    incus.profile_exists.return_value = True
    incus.network_exists.return_value = True
    incus.network_get.return_value = "10.79.115.1/24"
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )

    registry_up(incus, gcfg)

    incus.stop.assert_called_once_with(MIRROR_CONTAINER_NAME)
    incus.start.assert_called_once_with(MIRROR_CONTAINER_NAME)


def test_up_does_not_restart_running_mirror_when_live_ip_matches_reservation(tmp_path):
    """If the mirror is already on the reserved IP, leave it alone —
    no spurious downtime on every `jailbee registry up` invocation."""
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": MIRROR_CONTAINER_NAME,
            "status": "Running",
            "state": {
                "network": {
                    "eth0": {
                        "addresses": [
                            {"family": "inet", "scope": "global", "address": "10.79.115.2"},
                        ],
                    },
                },
            },
        },
    ]
    incus.profile_exists.return_value = True
    incus.network_exists.return_value = True
    incus.network_get.return_value = "10.79.115.1/24"
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )

    registry_up(incus, gcfg)

    incus.stop.assert_not_called()
    incus.start.assert_not_called()


def test_up_does_not_restart_running_mirror_when_reservation_unavailable(tmp_path):
    """No CIDR → no reservation → no basis for "drift", so leave the
    running mirror alone even if its IP looks unusual."""
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": MIRROR_CONTAINER_NAME,
            "status": "Running",
            "state": {
                "network": {
                    "eth0": {
                        "addresses": [
                            {"family": "inet", "scope": "global", "address": "10.79.115.98"},
                        ],
                    },
                },
            },
        },
    ]
    incus.profile_exists.return_value = True
    incus.network_exists.return_value = True
    incus.network_get.return_value = "auto"  # no CIDR → fallback
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )

    registry_up(incus, gcfg)

    incus.stop.assert_not_called()
    incus.start.assert_not_called()


def test_down_stops_container_when_present():
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": MIRROR_CONTAINER_NAME, "status": "Running"},
    ]

    registry_down(incus)

    incus.stop.assert_called_once_with(MIRROR_CONTAINER_NAME)


def test_down_is_noop_when_container_missing():
    incus = MagicMock()
    incus.list_containers.return_value = []

    registry_down(incus)

    incus.stop.assert_not_called()


def test_down_is_noop_when_container_already_stopped():
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": MIRROR_CONTAINER_NAME, "status": "Stopped"},
    ]

    registry_down(incus)

    incus.stop.assert_not_called()


def test_up_raises_if_service_does_not_become_active(tmp_path, mocker):
    """Caller decides retry — we surface the timeout, don't swallow it."""
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": MIRROR_CONTAINER_NAME, "status": "Running"},
    ]
    incus.profile_exists.return_value = True
    incus.exec.side_effect = IncusError("inactive")
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )
    mocker.patch("jailbee.registry._SERVICE_WAIT_SECONDS", 1)
    mocker.patch("jailbee.registry.time.sleep")  # don't actually sleep

    with pytest.raises(RuntimeError, match=r"service did not become active"):
        registry_up(incus, gcfg)


def test_service_state_returns_systemd_verdict():
    """The raw word matters: later logic distinguishes 'active' from
    everything else, and reports the rest verbatim in error messages."""
    incus = MagicMock()
    incus.exec.return_value = "activating\n"

    assert _service_state(incus) == "activating"


def test_service_state_reports_exec_failure_text():
    """`systemctl is-active` exits non-zero for every non-active state, so the
    wrapper raises. Surface the message rather than collapsing it to a bool —
    it is the only diagnostic the final error message has to offer."""
    incus = MagicMock()
    incus.exec.side_effect = IncusError("`incus exec` failed (exit 3): inactive")

    assert "exit 3" in _service_state(incus)


def test_wait_for_service_active_returns_none_when_active():
    incus = MagicMock()
    incus.exec.return_value = "active\n"

    assert _wait_for_service_active(incus) is None


def test_wait_for_service_active_returns_reason_on_timeout(mocker):
    """Returning instead of raising is what lets registry_up reinstall and
    try again rather than dead-ending on a recoverable failure."""
    incus = MagicMock()
    incus.exec.return_value = "failed\n"
    mocker.patch("jailbee.registry._SERVICE_WAIT_SECONDS", 0)
    mocker.patch("jailbee.registry.time.sleep")

    reason = _wait_for_service_active(incus)

    assert reason is not None
    assert "did not become active" in reason
    assert "failed" in reason  # the last observed state is reported


# ---------- apply_mirror_registries (per-repo REGISTRIES plumbing)


def _exec_bash_calls(incus_mock: MagicMock) -> list[str]:
    """Extract the inline bash script bodies passed to `incus exec mirror bash -c ...`."""
    return [
        call.args[1][2]
        for call in incus_mock.exec.call_args_list
        if len(call.args) >= 2
        and call.args[0] == MIRROR_CONTAINER_NAME
        and isinstance(call.args[1], list)
        and call.args[1][:2] == ["bash", "-c"]
    ]


def test_apply_mirror_registries_noop_for_empty_list():
    """No registries passed → nothing changes; don't even read state."""
    incus = MagicMock()

    changed = apply_mirror_registries(incus, [])

    assert changed is False
    incus.exec.assert_not_called()


def test_apply_mirror_registries_writes_env_and_restarts_on_new_registry():
    """First call with an unlisted upstream writes env file + restarts proxy."""
    incus = MagicMock()
    # No existing env file → cat falls back via `|| true`, returns empty.
    incus.exec.return_value = ""

    changed = apply_mirror_registries(incus, ["803520778560.dkr.ecr.eu-north-1.amazonaws.com"])

    assert changed is True
    scripts = _exec_bash_calls(incus)
    assert scripts, "expected a `bash -c` exec writing the env file"
    body = scripts[0]
    assert "/etc/jailbee-registry-proxy.env" in body
    assert "REGISTRIES=" in body
    assert "803520778560.dkr.ecr.eu-north-1.amazonaws.com" in body
    assert f"systemctl restart {MIRROR_SERVICE_NAME}" in body


def test_apply_mirror_registries_written_value_includes_rpardini_defaults():
    """EnvironmentFile= overrides the image's own ENV, so the file must
    carry the defaults too — otherwise rpardini drops k8s.io/gcr.io/quay/ghcr."""
    incus = MagicMock()
    incus.exec.return_value = ""

    apply_mirror_registries(incus, ["example.com"])

    body = _exec_bash_calls(incus)[0]
    for default in ("gcr.io", "ghcr.io", "quay.io", "registry.k8s.io"):
        assert default in body, f"image default {default!r} missing from written env"


def test_apply_mirror_registries_idempotent_when_already_present():
    """Second call with the same list reads the env file, sees nothing new,
    skips both write and restart."""
    incus = MagicMock()
    incus.exec.return_value = (
        "REGISTRIES=803520778560.dkr.ecr.eu-north-1.amazonaws.com "
        "gcr.io ghcr.io quay.io registry.k8s.io\n"
    )

    changed = apply_mirror_registries(incus, ["803520778560.dkr.ecr.eu-north-1.amazonaws.com"])

    assert changed is False
    assert _exec_bash_calls(incus) == []


def test_apply_mirror_registries_noop_when_only_defaults_requested():
    """Caller-supplied list that's a subset of rpardini's image defaults
    requires no action."""
    incus = MagicMock()
    incus.exec.return_value = ""

    changed = apply_mirror_registries(incus, ["quay.io", "ghcr.io"])

    assert changed is False
    assert _exec_bash_calls(incus) == []


def test_apply_mirror_registries_unions_with_existing_state():
    """Adding ECR-B when ECR-A is already in the env file keeps both."""
    incus = MagicMock()
    incus.exec.return_value = (
        "REGISTRIES=ecr-a.example.com gcr.io ghcr.io quay.io registry.k8s.io\n"
    )

    changed = apply_mirror_registries(incus, ["ecr-b.example.com"])

    assert changed is True
    body = _exec_bash_calls(incus)[0]
    assert "ecr-a.example.com" in body
    assert "ecr-b.example.com" in body


def test_apply_mirror_registries_writes_sorted_for_stability():
    """Env file content is sorted so re-runs with the same logical set produce
    identical bytes — avoids spurious diffs / restarts on re-execution."""
    incus = MagicMock()
    incus.exec.return_value = ""

    apply_mirror_registries(incus, ["zzz.example.com", "aaa.example.com"])

    body = _exec_bash_calls(incus)[0]
    # Find the REGISTRIES= line and check ordering
    registries_line = next(line for line in body.splitlines() if line.startswith("REGISTRIES="))
    items = registries_line.removeprefix("REGISTRIES=").split()
    assert items == sorted(items), f"REGISTRIES not sorted: {items}"


def test_apply_mirror_registries_deduplicates_caller_input():
    incus = MagicMock()
    incus.exec.return_value = ""

    apply_mirror_registries(incus, ["foo.example.com", "foo.example.com"])

    body = _exec_bash_calls(incus)[0]
    registries_line = next(line for line in body.splitlines() if line.startswith("REGISTRIES="))
    items = registries_line.removeprefix("REGISTRIES=").split()
    assert items.count("foo.example.com") == 1


def test_quadlet_unit_references_environment_file_with_absolute_path():
    """Quadlet's `EnvironmentFile=` lowers to `podman run --env-file=`, which
    has no `-` (optional) semantics — the dash would be taken as part of a
    relative path and resolved against the unit directory, yielding the
    nonsensical `/etc/containers/systemd/-/etc/jailbee-registry-proxy.env`. So:
    bare absolute path, and the file is created (empty) at provision time."""
    body = _read_provision_text("jailbee-registry-proxy.container")
    assert "EnvironmentFile=/etc/jailbee-registry-proxy.env" in body
    assert "EnvironmentFile=-" not in body


def test_provision_creates_empty_env_file_before_service_start(tmp_path):
    """Quadlet's `EnvironmentFile=` (translated to `podman run --env-file=`)
    refuses to start the container when the referenced file is missing. The
    install script must guarantee the file exists — via a guarded
    `test -f ... || install -D` placeholder, so a pre-existing file (and the
    REGISTRIES= it holds) survives a re-provision — before the first
    `systemctl start jailbee-registry-proxy.service`."""
    install_sh = _read_provision_text("install.sh")
    # Crude ordering check: the env-file creation line must appear before
    # `systemctl start jailbee-registry-proxy.service` in the script body.
    create_line_idx = install_sh.find("/etc/jailbee-registry-proxy.env")
    start_line_idx = install_sh.find("systemctl start jailbee-registry-proxy.service")
    assert create_line_idx != -1, "install.sh does not create the env file"
    assert start_line_idx != -1, "install.sh does not start the service"
    assert create_line_idx < start_line_idx, "env file must be created before the service starts"


def test_install_sh_does_not_truncate_an_existing_env_file():
    """Reprovisioning an existing mirror re-runs install.sh. An unconditional
    `install -D /dev/null /etc/jailbee-registry-proxy.env` would blow away the
    REGISTRIES= list apply_mirror_registries() maintains, and every later
    `gie new` would re-pull those upstreams from the internet instead of the
    cache."""
    body = _read_provision_text("install.sh")

    env_lines = [ln for ln in body.splitlines() if "/etc/jailbee-registry-proxy.env" in ln]
    assert env_lines, "install.sh no longer touches the env file — update this test"
    assert re.search(r"test -f /etc/jailbee-registry-proxy\.env\s*\|\|", body), (
        "the env-file creation must be guarded by 'test -f ... ||', not '&&' or unconditional"
    )


def _mirror_running_entry() -> dict:
    """An `incus list` entry for a healthy, running mirror on its reserved IP."""
    return {
        "name": MIRROR_CONTAINER_NAME,
        "status": "Running",
        "state": {
            "network": {
                "eth0": {
                    "addresses": [
                        {"family": "inet", "scope": "global", "address": "10.79.115.2"},
                    ],
                },
            },
        },
    }


def _install_sh_was_run(incus_mock: MagicMock) -> bool:
    """True if _provision_mirror ran — it execs a bash script containing the
    install.sh heredoc marker."""
    return any("JAILBEE_INSTALL_EOF" in body for body in _exec_bash_calls(incus_mock))


def _first_exec_call_index(incus_mock: MagicMock, predicate) -> int:
    """Index in `incus.exec.call_args_list` of the first call whose `cmd`
    argument satisfies `predicate`. Raises if none match, so a broken
    predicate fails loudly instead of comparing against -1."""
    for i, call in enumerate(incus_mock.exec.call_args_list):
        if len(call.args) >= 2 and predicate(call.args[1]):
            return i
    raise AssertionError(f"no exec call matched {predicate!r}")


def test_up_reprovisions_when_quadlet_unit_file_is_absent(tmp_path, mocker):
    """install.sh installs the Quadlet unit *after* `apt-get install podman`,
    so a network drop during apt leaves a container that boots fine with no
    unit file and no service. That is the reported failure, and it must be
    repaired without waiting out the full service timeout first — proven
    here by call ordering (probe, then install, then any service-state
    poll), not merely by the install having happened at some point."""
    incus = MagicMock()
    incus.list_containers.return_value = [_mirror_running_entry()]
    incus.profile_exists.return_value = True
    incus.network_exists.return_value = True
    incus.network_get.return_value = "10.79.115.1/24"

    def exec_side_effect(name, cmd, **kwargs):
        if cmd[:2] == ["sh", "-c"] and "jailbee-registry-proxy.container" in cmd[2]:
            return "absent\n"
        return "active\n"

    incus.exec.side_effect = exec_side_effect
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )
    sleep = mocker.patch("jailbee.registry.time.sleep")

    registry_up(incus, gcfg)

    assert _install_sh_was_run(incus)
    sleep.assert_not_called()  # no waiting out the timeout before repairing
    incus.delete.assert_not_called()  # repair in place, don't rebuild

    # Ordering, not just occurrence: the Quadlet probe must run before the
    # install script, which must run before any systemctl is-active poll.
    # Without this, a regression that moved the probe to *after*
    # `_wait_for_service_active` — i.e. wait 60s, then maybe repair, the
    # exact bug this task fixes — would still satisfy every assertion above.
    probe_idx = _first_exec_call_index(
        incus,
        lambda cmd: cmd[:2] == ["sh", "-c"] and "jailbee-registry-proxy.container" in cmd[2],
    )
    install_idx = _first_exec_call_index(
        incus, lambda cmd: cmd[:2] == ["bash", "-c"] and "JAILBEE_INSTALL_EOF" in cmd[2]
    )
    service_check_idx = _first_exec_call_index(
        incus, lambda cmd: cmd[:2] == ["systemctl", "is-active"]
    )
    assert probe_idx < install_idx < service_check_idx


def test_up_does_not_reprovision_a_healthy_running_mirror(tmp_path):
    """The guard the two-phase design exists for: a healthy mirror must never
    be handed a redundant apt run."""
    incus = MagicMock()
    incus.list_containers.return_value = [_mirror_running_entry()]
    incus.profile_exists.return_value = True
    incus.network_exists.return_value = True
    incus.network_get.return_value = "10.79.115.1/24"

    def exec_side_effect(name, cmd, **kwargs):
        if cmd[:2] == ["sh", "-c"] and "jailbee-registry-proxy.container" in cmd[2]:
            return "present\n"
        return "active\n"

    incus.exec.side_effect = exec_side_effect
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )

    registry_up(incus, gcfg)

    assert not _install_sh_was_run(incus)


def test_up_treats_an_unanswerable_probe_as_inconclusive(tmp_path, mocker):
    """A container that has only just started may not answer exec yet.
    Reading that as "unprovisioned" would reinstall on top of healthy,
    still-booting mirrors — so fall through to the wait instead."""
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": MIRROR_CONTAINER_NAME, "status": "Stopped"},
    ]
    incus.profile_exists.return_value = True
    incus.network_exists.return_value = True
    calls = {"n": 0}

    def exec_side_effect(name, cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IncusError("`incus exec` failed: container not ready")
        return "active\n"

    incus.exec.side_effect = exec_side_effect
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )
    mocker.patch("jailbee.registry.time.sleep")

    registry_up(incus, gcfg)

    assert not _install_sh_was_run(incus)


def test_up_reprovisions_once_when_the_service_never_comes_up(tmp_path, mocker):
    """The unit file can be in place while the service is still dead — a
    truncated podman install, a wedged systemd generator. install.sh is
    idempotent, so reinstall once and give it a second window."""
    incus = MagicMock()
    incus.list_containers.return_value = [_mirror_running_entry()]
    incus.profile_exists.return_value = True
    incus.network_exists.return_value = True
    incus.network_get.return_value = "10.79.115.1/24"
    state = {"active": False}

    def exec_side_effect(name, cmd, **kwargs):
        if cmd[:2] == ["sh", "-c"] and "jailbee-registry-proxy.container" in cmd[2]:
            return "present\n"
        if cmd[:2] == ["bash", "-c"]:  # _provision_mirror ran
            state["active"] = True
            return ""
        return "active\n" if state["active"] else "failed\n"

    incus.exec.side_effect = exec_side_effect
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )
    mocker.patch("jailbee.registry._SERVICE_WAIT_SECONDS", 0)
    mocker.patch("jailbee.registry.time.sleep")

    registry_up(incus, gcfg)  # must not raise

    assert _install_sh_was_run(incus)


def test_up_does_not_reprovision_twice_in_one_call(tmp_path, mocker):
    """A mirror provisioned moments ago in this same call gets no second,
    identical apt run — the script that just failed will fail again."""
    incus = MagicMock()
    incus.list_containers.return_value = []  # fresh create → provisions once
    incus.profile_exists.return_value = True
    incus.network_exists.return_value = True
    incus.exec.return_value = "failed\n"  # service never comes up
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )
    mocker.patch("jailbee.registry._SERVICE_WAIT_SECONDS", 0)
    mocker.patch("jailbee.registry.time.sleep")

    with pytest.raises(RuntimeError, match=r"did not become active"):
        registry_up(incus, gcfg)

    install_runs = [b for b in _exec_bash_calls(incus) if "JAILBEE_INSTALL_EOF" in b]
    assert len(install_runs) == 1


def test_up_reports_a_failing_reinstall_without_losing_the_timeout(tmp_path, mocker):
    """If the repair attempt itself dies (apt still has no network), the user
    needs both facts: the service is down *and* why the repair failed."""
    incus = MagicMock()
    incus.list_containers.return_value = [_mirror_running_entry()]
    incus.profile_exists.return_value = True
    incus.network_exists.return_value = True
    incus.network_get.return_value = "10.79.115.1/24"

    def exec_side_effect(name, cmd, **kwargs):
        if cmd[:2] == ["sh", "-c"] and "jailbee-registry-proxy.container" in cmd[2]:
            return "present\n"
        if cmd[:2] == ["bash", "-c"]:
            raise IncusError("apt-get update: Temporary failure resolving")
        return "failed\n"

    incus.exec.side_effect = exec_side_effect
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )
    mocker.patch("jailbee.registry._SERVICE_WAIT_SECONDS", 0)
    mocker.patch("jailbee.registry.time.sleep")

    with pytest.raises(RuntimeError) as excinfo:
        registry_up(incus, gcfg)

    message = str(excinfo.value)
    assert "did not become active" in message
    assert "Temporary failure resolving" in message


def test_up_recreate_deletes_and_reprovisions_a_healthy_mirror(tmp_path):
    """--recreate is for containers reinstalling can't fix, so it must act
    even when the mirror looks fine."""
    incus = MagicMock()
    incus.list_containers.return_value = [_mirror_running_entry()]
    incus.profile_exists.return_value = True
    incus.network_exists.return_value = True
    incus.network_get.return_value = "10.79.115.1/24"
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )

    registry_up(incus, gcfg, recreate=True)

    incus.delete.assert_called_once_with(MIRROR_CONTAINER_NAME, force=True)
    incus.init.assert_called_once_with("images:ubuntu/26.04/cloud", MIRROR_CONTAINER_NAME)
    assert _install_sh_was_run(incus)


def test_up_recreate_preserves_the_host_cache_and_ca(tmp_path):
    """Deleting the container must not touch the bind-mount sources: the
    cache is the point of the mirror, and every user container already
    trusts the CA in .../ca."""
    base = tmp_path / "registry"
    (base / "cache").mkdir(parents=True)
    (base / "ca").mkdir(parents=True)
    (base / "ca" / "ca.crt").write_text("PEM")
    incus = MagicMock()
    incus.list_containers.return_value = [_mirror_running_entry()]
    incus.profile_exists.return_value = True
    incus.network_exists.return_value = True
    incus.network_get.return_value = "10.79.115.1/24"
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate({"docker_registry_mirror": {"data_dir": str(base)}})

    registry_up(incus, gcfg, recreate=True)

    assert (base / "ca" / "ca.crt").read_text() == "PEM"
    assert (base / "cache").is_dir()


def test_up_recreate_is_fine_when_no_container_exists(tmp_path):
    """--recreate on a host that never had a mirror is a plain create."""
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.profile_exists.return_value = True
    incus.network_exists.return_value = True
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )

    registry_up(incus, gcfg, recreate=True)

    incus.delete.assert_not_called()
    incus.init.assert_called_once_with("images:ubuntu/26.04/cloud", MIRROR_CONTAINER_NAME)


def test_up_failure_message_points_at_recreate(tmp_path, mocker):
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.profile_exists.return_value = True
    incus.network_exists.return_value = True
    incus.exec.return_value = "failed\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )
    mocker.patch("jailbee.registry._SERVICE_WAIT_SECONDS", 0)
    mocker.patch("jailbee.registry.time.sleep")

    with pytest.raises(RuntimeError, match=r"--recreate"):
        registry_up(incus, gcfg)


def test_up_reports_a_failing_fast_path_reinstall_without_losing_the_timeout(tmp_path, mocker):
    """The quadlet-missing fast path is precisely the scenario this feature
    exists for: a network drop during the first install. If the network is
    *still* down when the fast-path reinstall runs, that IncusError must not
    escape raw — it must be folded into the same actionable RuntimeError the
    slow path produces, naming both the dead service and why the repair
    failed, plus `--recreate`. Before the fix this exec sequence made
    `_provision_mirror`'s IncusError propagate straight out of registry_up,
    so this test would fail with an unmatched IncusError rather than a
    RuntimeError if the fast path's own error handling were removed."""
    incus = MagicMock()
    incus.list_containers.return_value = [_mirror_running_entry()]
    incus.profile_exists.return_value = True
    incus.network_exists.return_value = True
    incus.network_get.return_value = "10.79.115.1/24"

    def exec_side_effect(name, cmd, **kwargs):
        if cmd[:2] == ["sh", "-c"] and "jailbee-registry-proxy.container" in cmd[2]:
            return "absent\n"
        if cmd[:2] == ["bash", "-c"]:
            raise IncusError("apt-get install podman: Temporary failure resolving")
        return "failed\n"

    incus.exec.side_effect = exec_side_effect
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )
    mocker.patch("jailbee.registry._SERVICE_WAIT_SECONDS", 0)
    mocker.patch("jailbee.registry.time.sleep")

    with pytest.raises(RuntimeError) as excinfo:
        registry_up(incus, gcfg)

    message = str(excinfo.value)
    assert "did not become active" in message
    assert "apt-get install podman: Temporary failure resolving" in message
    assert "--recreate" in message

    # Only one reinstall attempt total: the fast path's, none from the
    # fallback (the provisioned guard must prevent a second, identical run).
    install_runs = [b for b in _exec_bash_calls(incus) if "JAILBEE_INSTALL_EOF" in b]
    assert len(install_runs) == 1


def test_up_second_wait_timeout_message_mentions_the_earlier_reinstall(tmp_path, mocker):
    """The slow-path fallback reinstalls once and waits again. If that second
    wait *also* times out, the message must still say a reinstall was already
    attempted — otherwise an operator reading only the final error has no way
    to tell "never repaired" from "repaired and still broken", which call for
    different next steps. Before this fix the second timeout's reason simply
    replaced the first, dropping that fact — so this test would fail on the
    "reinstalled the proxy once" assertion if that behaviour were removed,
    even though it would still correctly raise on the timeout itself."""
    incus = MagicMock()
    incus.list_containers.return_value = [_mirror_running_entry()]
    incus.profile_exists.return_value = True
    incus.network_exists.return_value = True
    incus.network_get.return_value = "10.79.115.1/24"

    def exec_side_effect(name, cmd, **kwargs):
        if cmd[:2] == ["sh", "-c"] and "jailbee-registry-proxy.container" in cmd[2]:
            return "present\n"
        if cmd[:2] == ["bash", "-c"]:  # reinstall itself succeeds...
            return ""
        return "failed\n"  # ...but the service still never comes up

    incus.exec.side_effect = exec_side_effect
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )
    mocker.patch("jailbee.registry._SERVICE_WAIT_SECONDS", 0)
    mocker.patch("jailbee.registry.time.sleep")

    with pytest.raises(RuntimeError) as excinfo:
        registry_up(incus, gcfg)

    message = str(excinfo.value)
    assert "reinstalled the proxy once" in message
    assert "did not become active" in message
    assert "--recreate" in message

    install_runs = [b for b in _exec_bash_calls(incus) if "JAILBEE_INSTALL_EOF" in b]
    assert len(install_runs) == 1


# ---- progress reporting: the slow phases must announce themselves ----


def test_up_reports_the_two_slow_phases_of_a_fresh_create(tmp_path, mocker):
    """The image pull and the apt run are the minutes-long silent stretches.

    `incus init` captures Incus's own download progress, so if these two
    stop reporting, a first `jailbee registry up` is several minutes of a
    blank terminal — which is what users read as a hang.
    """
    incus = MagicMock()
    incus.list_containers.return_value = []  # fresh create
    incus.profile_exists.return_value = True
    incus.network_exists.return_value = True
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )
    steps: list[str] = []

    registry_up(incus, gcfg, on_step=steps.append)

    joined = " | ".join(steps)
    assert "images:ubuntu/26.04/cloud" in joined  # the image pull is named
    assert "downloads it" in joined  # ...and flagged as slow
    assert any("apt" in s and "10 min" in s for s in steps)  # the provisioning run


def test_up_stays_silent_by_default(tmp_path, mocker):
    """`on_step` is opt-in: every existing caller keeps its current output."""
    incus = MagicMock()
    incus.list_containers.return_value = []
    incus.profile_exists.return_value = True
    incus.network_exists.return_value = True
    incus.exec.return_value = "active\n"
    gcfg = GlobalConfig.model_validate(
        {"docker_registry_mirror": {"data_dir": str(tmp_path / "registry")}}
    )
    printed = mocker.patch("builtins.print")

    registry_up(incus, gcfg)  # no on_step

    printed.assert_not_called()


def test_wait_for_service_counts_down_with_the_service_state(mocker):
    """A bare "waiting" for a minute is indistinguishable from a hang."""
    from jailbee.registry import _wait_for_service_active

    incus = MagicMock()
    incus.exec.side_effect = ["activating\n", "activating\n", "active\n"]
    mocker.patch("jailbee.registry.time.sleep")
    steps: list[str] = []

    assert _wait_for_service_active(incus, steps.append) is None

    assert len(steps) == 2  # reported on each poll that was not yet active
    assert "activating" in steps[0]
    assert "s left" in steps[0]
