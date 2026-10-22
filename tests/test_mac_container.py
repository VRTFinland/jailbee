"""Tests for the Apple `container` CLI wrapper. Fully mocked — no real machine."""

from __future__ import annotations

import json
import subprocess as _sp
from pathlib import Path

import pytest

from jailbee.mac_container import AppleContainer, AppleContainerError, MachineInfo


class FakeRunner:
    """Records argv lists; answers from a queue of (returncode, stdout, stderr)."""

    def __init__(self, *answers: tuple[int, str, str]) -> None:
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []
        self._answers = list(answers)

    def __call__(self, argv: list[str], **kwargs: object) -> _sp.CompletedProcess[str]:
        self.calls.append(argv)
        self.inputs.append(kwargs.get("input"))  # type: ignore[arg-type]
        rc, out, err = self._answers.pop(0) if self._answers else (0, "", "")
        return _sp.CompletedProcess(argv, rc, stdout=out, stderr=err)


INSPECT = [
    {
        "id": "jailbee",
        "status": "running",
        "homeMount": "rw",
        "cpus": 6,
        "memory": 12884901888,
        "ipAddress": "192.168.64.4",
        "kernel": "/Users/u/Library/Application Support/jailbee/kernels/vmlinux-6.18.35-jailbee",
    }
]


def test_machine_set_passes_path_with_space_as_one_argv_item() -> None:
    runner = FakeRunner()
    ac = AppleContainer(runner=runner)
    kernel = "/Users/u/Library/Application Support/jailbee/kernels/vmlinux-6.18.35-jailbee"
    ac.machine_set("jailbee", kernel=kernel)
    assert runner.calls[0] == ["container", "machine", "set", "-n", "jailbee", f"kernel={kernel}"]


def test_machine_returns_none_when_inspect_fails() -> None:
    runner = FakeRunner((1, "", "Error: machine not found"))
    assert AppleContainer(runner=runner).machine("jailbee") is None


def test_machine_parses_inspect_json() -> None:
    runner = FakeRunner((0, json.dumps(INSPECT), ""))
    info = AppleContainer(runner=runner).machine("jailbee")
    assert info == MachineInfo(
        name="jailbee",
        running=True,
        kernel=INSPECT[0]["kernel"],
        home_mount="rw",
        ip="192.168.64.4",
    )
    assert runner.calls[0] == ["container", "machine", "inspect", "jailbee"]


def test_machine_without_kernel_key_has_none_kernel() -> None:
    entry = {k: v for k, v in INSPECT[0].items() if k != "kernel"}
    runner = FakeRunner((0, json.dumps([entry]), ""))
    info = AppleContainer(runner=runner).machine("jailbee")
    assert info is not None and info.kernel is None


def test_version_none_when_binary_missing() -> None:
    def runner(argv: list[str], **kwargs: object) -> _sp.CompletedProcess[str]:
        raise FileNotFoundError(argv[0])

    assert AppleContainer(runner=runner).version() is None


def test_version_parses_cli_banner() -> None:
    runner = FakeRunner((0, "container CLI version 1.6.0 (build: release, commit: unspeci)\n", ""))
    assert AppleContainer(runner=runner).version() == "1.6.0"


def test_machine_run_joins_argv_into_one_quoted_string() -> None:
    """`container machine run` re-splits argv through bash -c, so a command with
    spaces must reach it as ONE pre-quoted argument (measured on a real Mac)."""
    runner = FakeRunner()
    AppleContainer(runner=runner).machine_run("jailbee", ["sh", "-c", "sleep 1; exit 7"])
    assert runner.calls[0] == [
        "container",
        "machine",
        "run",
        "-n",
        "jailbee",
        "--",
        "sh -c 'sleep 1; exit 7'",
    ]


def test_machine_run_root_and_input_flags() -> None:
    runner = FakeRunner()
    AppleContainer(runner=runner).machine_run("m", ["bash", "-s"], root=True, input="echo hi\n")
    assert runner.calls[0][:6] == ["container", "machine", "run", "-n", "m", "--root"]
    assert "-i" in runner.calls[0]
    assert runner.inputs[0] == "echo hi\n"


def test_wait_ready_accepts_running_and_degraded() -> None:
    for state in ("running", "degraded"):
        AppleContainer(runner=FakeRunner((0, f"{state}\n", ""))).wait_ready("m")


def test_wait_ready_retries_until_the_system_bus_is_up() -> None:
    """Right after boot the bus is missing: the command fails with stderr only."""
    bus_down = (1, "", "Failed to connect to system scope bus via local transport")
    runner = FakeRunner(bus_down, bus_down, (0, "running\n", ""))
    AppleContainer(runner=runner, sleep=lambda seconds: None).wait_ready("m")
    assert len(runner.calls) == 3


def test_wait_ready_gives_up_with_a_plain_message() -> None:
    runner = FakeRunner(*[(1, "", "no bus")] * 100)
    with pytest.raises(AppleContainerError, match="did not finish booting"):
        AppleContainer(runner=runner, sleep=lambda seconds: None).wait_ready("m")


def test_wait_ready_raises_on_offline() -> None:
    ac = AppleContainer(runner=FakeRunner((1, "offline\n", "")), sleep=lambda seconds: None)
    with pytest.raises(AppleContainerError, match="offline"):
        ac.wait_ready("m")


def test_errors_carry_stderr() -> None:
    runner = FakeRunner((1, "", "Error: disk full\n"))
    with pytest.raises(AppleContainerError, match="disk full"):
        AppleContainer(runner=runner).machine_stop("jailbee")


def test_default_kernel_release_reads_symlink_name(tmp_path: Path) -> None:
    kdir = tmp_path / "kernels"
    kdir.mkdir()
    (kdir / "vmlinux-6.18.35-197-debug").write_bytes(b"")
    (kdir / "default.kernel-arm64").symlink_to(kdir / "vmlinux-6.18.35-197-debug")
    ac = AppleContainer(runner=FakeRunner(), kernels_dir=kdir)
    assert ac.default_kernel_release() == "6.18.35-197-debug"


def test_default_kernel_release_none_without_symlink(tmp_path: Path) -> None:
    ac = AppleContainer(runner=FakeRunner(), kernels_dir=tmp_path)
    assert ac.default_kernel_release() is None


def test_run_ephemeral_mounts_volumes_and_returns_exit_code() -> None:
    runner = FakeRunner((3, "", ""))
    rc = AppleContainer(runner=runner).run_ephemeral(
        "ubuntu:26.04",
        ["sh", "/kbuild.sh", "6.18.35"],
        volumes={Path("/Users/u/out"): "/out"},
        cpus=4,
        memory="6G",
    )
    assert rc == 3
    argv = runner.calls[0]
    assert argv[:3] == ["container", "run", "--rm"]
    assert argv[argv.index("-v") + 1] == "/Users/u/out:/out"
    assert argv[argv.index("--cpus") + 1] == "4"
    assert argv[argv.index("--memory") + 1] == "6G"
    assert argv[-4:] == ["ubuntu:26.04", "sh", "/kbuild.sh", "6.18.35"]


def test_machine_create_argv_puts_image_last_and_keeps_kernel_path_whole() -> None:
    runner = FakeRunner()
    kernel = Path("/Users/u/Library/Application Support/jailbee/kernels/vmlinux-6.18.35-jailbee")
    AppleContainer(runner=runner).machine_create(
        "jailbee",
        "local/jailbee-machine:abc",
        cpus=4,
        memory="8G",
        home_mount="none",
        kernel=kernel,
    )
    argv = runner.calls[0]
    assert argv[:4] == ["container", "machine", "create", "--name"]
    assert argv[argv.index("--home-mount") + 1] == "none"
    assert argv[argv.index("--kernel") + 1] == str(kernel)
    assert argv[argv.index("--cpus") + 1] == "4"
    assert argv[argv.index("--memory") + 1] == "8G"
    assert argv[-1] == "local/jailbee-machine:abc"


def test_build_image_uses_plain_progress_and_resources(tmp_path: Path) -> None:
    runner = FakeRunner()
    AppleContainer(runner=runner).build_image(tmp_path, "local/x:1", cpus=4, memory="4G")
    argv = runner.calls[0]
    assert argv[:2] == ["container", "build"]
    assert argv[argv.index("--progress") + 1] == "plain"
    assert argv[argv.index("-t") + 1] == "local/x:1"
    assert argv[-1] == str(tmp_path)


def test_image_exists_follows_inspect_exit_status() -> None:
    assert AppleContainer(runner=FakeRunner((0, "[]", ""))).image_exists("local/x:1")
    assert not AppleContainer(runner=FakeRunner((1, "", "not found"))).image_exists("local/x:1")


def test_system_running_reads_status_table() -> None:
    ok = FakeRunner((0, "FIELD VALUE\nstatus              running\n", ""))
    assert AppleContainer(runner=ok).system_running()
    down = FakeRunner((1, "apiserver is not running and not registered with launchd\n", ""))
    assert not AppleContainer(runner=down).system_running()


def test_machine_stop_rm_and_image_rm_argv() -> None:
    runner = FakeRunner()
    ac = AppleContainer(runner=runner)
    ac.machine_stop("m")
    ac.machine_rm("m")
    ac.image_rm("local/x:1")
    assert runner.calls == [
        ["container", "machine", "stop", "m"],
        ["container", "machine", "rm", "m"],
        ["container", "image", "rm", "local/x:1"],
    ]


def test_set_default_kernel_recommended_argv() -> None:
    runner = FakeRunner()
    AppleContainer(runner=runner).set_default_kernel_recommended()
    assert runner.calls == [["container", "system", "kernel", "set", "--recommended", "--force"]]
