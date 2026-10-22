"""Tests for the setup preflight step. Fully mocked."""

from __future__ import annotations

from pathlib import Path

import pytest

from jailbee.mac_container import MachineInfo
from jailbee.macsetup.engine import MacPaths, SetupContext, SetupError, SetupOptions, SetupState
from jailbee.macsetup.steps_preflight import HostFacts, PreflightStep, resolve_resources

GIB = 1024**3


class FakeAC:
    def __init__(
        self,
        *,
        version: str | None = "1.6.0",
        running: bool = True,
        kernel: str | None = "6.18.35-197-debug",
        machine: MachineInfo | None = None,
    ) -> None:
        self._version, self._running, self._kernel, self._machine = (
            version,
            running,
            kernel,
            machine,
        )
        self.commands: list[list[str]] = []

    def version(self) -> str | None:
        return self._version

    def system_running(self) -> bool:
        return self._running

    def default_kernel_release(self) -> str | None:
        return self._kernel

    def machine(self, name: str) -> MachineInfo | None:
        return self._machine

    def set_default_kernel_recommended(self) -> None:
        self.commands.append(["container", "system", "kernel", "set", "--recommended", "--force"])
        self._kernel = "6.18.35-197-debug"


def make_ctx(
    tmp_path: Path, ac: FakeAC, *, interactive: bool = True, yes: bool = False
) -> SetupContext:
    paths = MacPaths(
        tmp_path / "cfg", tmp_path / "cfg" / "s.json", tmp_path / "k", tmp_path / "ssh"
    )
    lines: list[str] = []
    ctx = SetupContext(
        ac=ac,  # type: ignore[arg-type]
        state=SetupState(options=SetupOptions(assume_yes=yes)),
        paths=paths,
        interactive=interactive,
        confirm=lambda q: True,
        log=lines.append,
    )
    ctx.lines = lines  # type: ignore[attr-defined]
    return ctx


def host(**over: object) -> HostFacts:
    base = dict(arch="arm64", ncpu=8, memsize=16 * GIB, free_bytes=200 * GIB)
    base.update(over)
    return HostFacts(**base)  # type: ignore[arg-type]


def test_resources_half_with_floors() -> None:
    assert resolve_resources(SetupOptions(), 8, 16 * GIB) == (4, "8G")
    assert resolve_resources(SetupOptions(), 2, 4 * GIB) == (2, "4G")
    assert resolve_resources(SetupOptions(cpus=3, memory="6G"), 8, 16 * GIB) == (3, "6G")


def test_refuses_foreign_machine(tmp_path: Path) -> None:
    ac = FakeAC(machine=MachineInfo("jailbee", True, None, "rw"))
    ctx = make_ctx(tmp_path, ac)
    with pytest.raises(SetupError, match="already exists") as excinfo:
        PreflightStep(host()).run(ctx)
    assert "--machine" in (excinfo.value.remedy or "")


def test_own_machine_is_not_foreign(tmp_path: Path) -> None:
    ac = FakeAC(machine=MachineInfo("jailbee", True, None, "rw"))
    ctx = make_ctx(tmp_path, ac)
    ctx.state.facts["machine"] = "jailbee"
    PreflightStep(host()).run(ctx)


def test_stale_kernel_asks_before_global_change(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, FakeAC(kernel="6.12.28-153"), interactive=False)
    with pytest.raises(SetupError) as excinfo:
        PreflightStep(host()).run(ctx)
    assert excinfo.value.exit_code == 2
    assert "container system kernel set --recommended --force" in (excinfo.value.remedy or "")


def test_stale_kernel_updated_when_user_agrees(tmp_path: Path) -> None:
    ac = FakeAC(kernel="6.12.28-153")
    ctx = make_ctx(tmp_path, ac, yes=True)
    PreflightStep(host()).run(ctx)
    assert ac.commands == [["container", "system", "kernel", "set", "--recommended", "--force"]]
    assert ctx.state.facts["kernel_release"] == "6.18.35-197-debug"


def test_low_disk_fails_with_numbers(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, FakeAC())
    with pytest.raises(SetupError, match="10 GiB"):
        PreflightStep(host(free_bytes=10 * GIB)).run(ctx)


def test_missing_cli_and_old_cli_and_stopped_service(tmp_path: Path) -> None:
    with pytest.raises(SetupError, match="not installed"):
        PreflightStep(host()).run(make_ctx(tmp_path, FakeAC(version=None)))
    with pytest.raises(SetupError, match=r"1\.6\.0"):
        PreflightStep(host()).run(make_ctx(tmp_path, FakeAC(version="1.5.2")))
    with pytest.raises(SetupError) as excinfo:
        PreflightStep(host()).run(make_ctx(tmp_path, FakeAC(running=False)))
    assert "container system start --enable-kernel-install" in (excinfo.value.remedy or "")


def test_intel_mac_refused(tmp_path: Path) -> None:
    with pytest.raises(SetupError, match="Apple silicon"):
        PreflightStep(host(arch="x86_64")).run(make_ctx(tmp_path, FakeAC()))


def test_records_kernel_release_and_resources(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, FakeAC())
    PreflightStep(host()).run(ctx)
    assert ctx.state.facts["kernel_release"] == "6.18.35-197-debug"
    assert ctx.state.facts["cpus"] == "4"
    assert ctx.state.facts["memory"] == "8G"


def test_preflight_always_runs(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, FakeAC())
    step = PreflightStep(host())
    assert step.fingerprint(ctx) != step.fingerprint(ctx)
