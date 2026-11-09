"""Tests for the `gie start` CLI command."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jailbee.autostart_plan import AutostartPlan
from jailbee.cli import app

# `run_autostart` returns the plan the CLI hands to a detached worker; a bare
# MagicMock would be serialised into the job file.
_NO_DETACHED = AutostartPlan(blocking=[], detached=[])

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _host_autostart_config(mocker):
    mocker.patch(
        "jailbee.branch_config.effective_autostart_config",
        side_effect=lambda cfg, *_args, **_kwargs: cfg,
    )


def test_start_calls_apply_hosts_for_strict_container(mocker):
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "myrepo-feat-x"),
    )
    mocker.patch("jailbee.runtime_mounts.attach_runtime_devices")
    mocker.patch("jailbee.runtime_mounts.detach_runtime_devices")
    mocker.patch(
        "jailbee.lifecycle.current_network_mode",
        return_value="strict",
    )
    apply = mocker.patch("jailbee.hosts.apply_hosts")

    result = runner.invoke(
        app,
        [
            "start",
            "myrepo-feat-x",
            "--no-autostart",
            "--config",
            str(FIXTURES / "full_config.yaml"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert apply.call_count == 1


def test_start_forwards_mirror_endpoint_to_apply_hosts_for_strict(mocker):
    """Strict containers need <mirror-ip> jailbee-registry-mirror.incus pinned
    in /etc/hosts because incusbr0's dnsmasq can't see the mirror on
    jailbee-loose."""
    from jailbee.global_config import DockerRegistryMirror, GlobalConfig

    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "myrepo-feat-x"),
    )
    mocker.patch("jailbee.runtime_mounts.attach_runtime_devices")
    mocker.patch("jailbee.runtime_mounts.detach_runtime_devices")
    mocker.patch(
        "jailbee.lifecycle.current_network_mode",
        return_value="strict",
    )
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=GlobalConfig(
            docker_registry_mirror=DockerRegistryMirror(
                enabled=True, port=3128, data_dir=Path("/tmp/x")
            ),
        ),
    )
    mocker.patch(
        "jailbee.docker_daemon.compute_mirror_endpoint",
        return_value=("10.42.0.7", 3128),
    )
    apply = mocker.patch("jailbee.hosts.apply_hosts")

    result = runner.invoke(
        app,
        [
            "start",
            "myrepo-feat-x",
            "--no-autostart",
            "--config",
            str(FIXTURES / "full_config.yaml"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert apply.call_args.kwargs.get("mirror_endpoint") == ("10.42.0.7", 3128)


def test_start_forwards_mirror_endpoint_to_run_autostart(mocker):
    """`gie start` must hand `run_autostart` the same `mirror_endpoint`
    used by `apply_hosts`, so transient strict→loose→strict swaps inside
    autostart steps keep `jailbee-registry-mirror.incus` pinned."""
    from jailbee.global_config import DockerRegistryMirror, GlobalConfig

    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "myrepo-feat-x"),
    )
    mocker.patch("jailbee.runtime_mounts.attach_runtime_devices")
    mocker.patch("jailbee.runtime_mounts.detach_runtime_devices")
    mocker.patch(
        "jailbee.lifecycle.current_network_mode",
        return_value="strict",
    )
    mocker.patch("jailbee.hosts.apply_hosts")
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=GlobalConfig(
            docker_registry_mirror=DockerRegistryMirror(
                enabled=True, port=3128, data_dir=Path("/tmp/x")
            ),
        ),
    )
    mocker.patch(
        "jailbee.docker_daemon.compute_mirror_endpoint",
        return_value=("10.42.0.7", 3128),
    )
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.autostart.has_graphical_session", return_value=False)
    run_autostart = mocker.patch("jailbee.autostart.run_autostart", return_value=_NO_DETACHED)

    result = runner.invoke(
        app,
        [
            "start",
            "myrepo-feat-x",
            "--config",
            str(FIXTURES / "full_config.yaml"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert run_autostart.call_args.kwargs.get("mirror_endpoint") == ("10.42.0.7", 3128)


def test_start_no_autostart_still_injects_github_token(mocker):
    """`gie start --no-autostart` skips the user's autostart steps but must
    still inject GH_TOKEN — it's infrastructure, not a user command."""
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "myrepo-feat-x"),
    )
    mocker.patch("jailbee.runtime_mounts.attach_runtime_devices")
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="loose")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.cli._mirror_endpoint_or_none", return_value=None)
    inject = mocker.patch("jailbee.autostart.inject_github_token")
    run_autostart = mocker.patch("jailbee.autostart.run_autostart", return_value=_NO_DETACHED)

    result = runner.invoke(
        app,
        [
            "start",
            "myrepo-feat-x",
            "--no-autostart",
            "--config",
            str(FIXTURES / "full_config.yaml"),
        ],
    )

    assert result.exit_code == 0, result.output
    inject.assert_called_once()
    assert inject.call_args.args[2] == "myrepo-feat-x"
    run_autostart.assert_not_called()


def test_start_skips_apply_hosts_for_loose_container(mocker):
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "myrepo-feat-x"),
    )
    mocker.patch("jailbee.runtime_mounts.attach_runtime_devices")
    mocker.patch("jailbee.runtime_mounts.detach_runtime_devices")
    mocker.patch(
        "jailbee.lifecycle.current_network_mode",
        return_value="loose",
    )
    apply = mocker.patch("jailbee.hosts.apply_hosts")

    result = runner.invoke(
        app,
        [
            "start",
            "myrepo-feat-x",
            "--no-autostart",
            "--config",
            str(FIXTURES / "full_config.yaml"),
        ],
    )

    assert result.exit_code == 0, result.output
    apply.assert_not_called()


def test_start_pins_only_the_mirror_for_a_loose_container(mocker):
    """A loose work container asks jailbee-work's dnsmasq, which has no
    record for the mirror on jailbee-loose: the mirror row must survive."""
    incus = mocker.MagicMock()
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "myrepo-feat-x"))
    mocker.patch("jailbee.runtime_mounts.attach_runtime_devices")
    mocker.patch("jailbee.runtime_mounts.detach_runtime_devices")
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="loose")
    mocker.patch("jailbee.cli._mirror_endpoint_or_none", return_value=("10.42.0.7", 3128))
    mocker.patch("jailbee.autostart.inject_github_token")
    apply = mocker.patch("jailbee.hosts.apply_hosts")

    result = runner.invoke(
        app,
        [
            "start",
            "myrepo-feat-x",
            "--no-autostart",
            "--config",
            str(FIXTURES / "full_config.yaml"),
        ],
    )

    assert result.exit_code == 0, result.output
    apply.assert_called_once_with(
        mocker.ANY, incus, "myrepo-feat-x", entries=[], mirror_endpoint=("10.42.0.7", 3128)
    )


def test_start_skips_apply_hosts_when_no_recognised_profile(mocker):
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "myrepo-feat-x"),
    )
    mocker.patch("jailbee.runtime_mounts.attach_runtime_devices")
    mocker.patch("jailbee.runtime_mounts.detach_runtime_devices")
    mocker.patch(
        "jailbee.lifecycle.current_network_mode",
        return_value=None,
    )
    apply = mocker.patch("jailbee.hosts.apply_hosts")

    result = runner.invoke(
        app,
        [
            "start",
            "myrepo-feat-x",
            "--no-autostart",
            "--config",
            str(FIXTURES / "full_config.yaml"),
        ],
    )

    assert result.exit_code == 0, result.output
    apply.assert_not_called()


def test_mirror_endpoint_or_none_skips_compute_without_a_docker_stack(tmp_path, mocker):
    """No Docker in the image → no reason to resolve the mirror, and no
    reason for the /etc/hosts pin to name it."""
    from jailbee import cli
    from jailbee.global_config import DockerRegistryMirror, GlobalConfig
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path / "repo")
    mocker.patch.object(
        cli,
        "_load_global",
        return_value=GlobalConfig(
            docker_registry_mirror=DockerRegistryMirror(data_dir=tmp_path / "registry"),
        ),
    )
    compute = mocker.patch("jailbee.docker_daemon.compute_mirror_endpoint")

    assert cli._mirror_endpoint_or_none(cfg, mocker.MagicMock()) is None
    compute.assert_not_called()


@pytest.mark.parametrize("command", ["start", "restart"])
def test_boot_refusal_is_reported_cleanly_not_as_a_traceback(mocker, command):
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "myrepo-feat-x"),
    )
    mocker.patch(
        "jailbee.lifecycle.boot_container",
        side_effect=ValueError("cannot start: the agent-instructions directory is [missing]"),
    )

    result = runner.invoke(
        app,
        [command, "myrepo-feat-x", "--config", str(FIXTURES / "full_config.yaml")],
    )

    assert result.exit_code == 1
    assert "cannot start: the agent-instructions directory is [missing]" in result.output
    assert "Traceback" not in result.output
