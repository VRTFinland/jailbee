"""Tests for the machine, prepare and Incus steps. Fully mocked."""

from __future__ import annotations

import subprocess as _sp
from importlib import resources
from pathlib import Path

import pytest

from jailbee.mac_container import MachineInfo
from jailbee.macsetup.engine import MacPaths, SetupContext, SetupError, SetupOptions, SetupState
from jailbee.macsetup.steps_machine import IncusStep, MachineStep, PrepareStep

KERNEL = "/Users/u/Library/Application Support/jailbee/kernels/vmlinux-6.18.35-jailbee"


def _cp(rc: int = 0, out: str = "", err: str = "") -> _sp.CompletedProcess[str]:
    return _sp.CompletedProcess([], rc, stdout=out, stderr=err)


class FakeAC:
    def __init__(
        self,
        machine: MachineInfo | None = None,
        vm: dict[str, _sp.CompletedProcess[str]] | None = None,
    ) -> None:
        self._machine = machine
        self.events: list[tuple[str, object]] = []
        self.vm = vm or {}

    def machine(self, name: str) -> MachineInfo | None:
        return self._machine

    def machine_create(self, name, image, **kw):  # type: ignore[no-untyped-def]
        self.events.append(("create", {"name": name, "image": image, **kw}))

    def machine_set(self, name, **settings):  # type: ignore[no-untyped-def]
        self.events.append(("set", settings))

    def machine_stop(self, name):  # type: ignore[no-untyped-def]
        self.events.append(("stop", name))

    def wait_ready(self, name):  # type: ignore[no-untyped-def]
        self.events.append(("wait_ready", name))

    def machine_run(self, name, argv, *, root=False, input=None):  # type: ignore[no-untyped-def]
        self.events.append(("run", {"argv": argv, "root": root, "input": input}))
        for key, result in self.vm.items():
            if key in " ".join(argv):
                return result
        return _cp()


def make_ctx(tmp_path: Path, ac: FakeAC, *, mode: str = "mac-repos") -> SetupContext:
    paths = MacPaths(tmp_path, tmp_path / "s.json", tmp_path / "k", tmp_path / "ssh")
    return SetupContext(
        ac=ac,  # type: ignore[arg-type]
        state=SetupState(
            options=SetupOptions(mode=mode),  # type: ignore[arg-type]
            facts={
                "image": "local/jailbee-machine:abc",
                "kernel_path": KERNEL,
                "cpus": "4",
                "memory": "8G",
            },
        ),
        paths=paths,
        interactive=True,
        confirm=lambda q: True,
        log=lambda line: None,
    )


def kinds(ac: FakeAC) -> list[str]:
    return [kind for kind, _ in ac.events]


def test_creates_machine_with_kernel_and_home_mount_per_mode(tmp_path: Path) -> None:
    for mode, mount in (("mac-repos", "rw"), ("isolated", "none")):
        ac = FakeAC()
        ctx = make_ctx(tmp_path, ac, mode=mode)
        MachineStep().run(ctx)
        create = dict(ac.events)["create"]
        assert create["home_mount"] == mount  # type: ignore[index]
        assert create["kernel"] == Path(KERNEL)  # type: ignore[index]
        assert create["cpus"] == 4 and create["memory"] == "8G"  # type: ignore[index]
        assert ctx.state.facts["machine"] == "jailbee"
        assert kinds(ac)[-1] == "wait_ready"


def test_existing_machine_gets_new_kernel_and_restart(tmp_path: Path) -> None:
    ac = FakeAC(MachineInfo("jailbee", True, "/old/kernel", "rw"))
    MachineStep().run(make_ctx(tmp_path, ac))
    assert kinds(ac)[:2] == ["set", "stop"]
    assert dict(ac.events)["set"] == {"kernel": KERNEL}
    assert "create" not in kinds(ac)
    assert kinds(ac)[-1] == "wait_ready"


def test_existing_machine_with_other_home_mount_is_refused(tmp_path: Path) -> None:
    ac = FakeAC(MachineInfo("jailbee", True, KERNEL, "none"))
    with pytest.raises(SetupError, match="home"):
        MachineStep().run(make_ctx(tmp_path, ac))


def test_path_with_space_reaches_the_cli_as_one_value(tmp_path: Path) -> None:
    ac = FakeAC()
    MachineStep().run(make_ctx(tmp_path, ac))
    assert str(dict(ac.events)["create"]["kernel"]) == KERNEL  # type: ignore[index]


def test_prepare_pipes_script_as_root_after_wait_ready(tmp_path: Path) -> None:
    ac = FakeAC()
    PrepareStep(dev_user="jb").run(make_ctx(tmp_path, ac))
    assert kinds(ac)[0] == "wait_ready"
    run = dict(ac.events)["run"]
    assert run["root"] is True  # type: ignore[index]
    assert run["argv"][:2] == ["env", "DEV_USER=jb"]  # type: ignore[index]
    assert "securityfs" in run["input"]  # type: ignore[index,operator]


def test_prepare_fails_with_stderr(tmp_path: Path) -> None:
    ac = FakeAC(vm={"bash": _cp(1, "", "E: no network")})
    with pytest.raises(SetupError, match="no network"):
        PrepareStep(dev_user="jb").run(make_ctx(tmp_path, ac))


def test_prepare_fingerprint_changes_with_packages(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, FakeAC())
    assert PrepareStep(dev_user="jb", packages="git").fingerprint(ctx) != PrepareStep(
        dev_user="jb", packages="git curl"
    ).fingerprint(ctx)


def test_incus_step_skips_existing_pool(tmp_path: Path) -> None:
    ac = FakeAC(
        vm={
            "storage list": _cp(0, "default,dir\njailbee-btrfs,btrfs\n"),
            "uname": _cp(0, "6.18.35-jailbee\n"),
            "lsm": _cp(0, "lockdown,capability,landlock,yama,apparmor,bpf"),
        }
    )
    IncusStep(pool_gib=50).run(make_ctx(tmp_path, ac))
    commands = [" ".join(e["argv"]) for kind, e in ac.events if kind == "run"]  # type: ignore[index]
    assert not any("admin init" in c or "storage create" in c for c in commands)


def test_incus_step_initialises_and_creates_pool(tmp_path: Path) -> None:
    ac = FakeAC(
        vm={
            "storage list": _cp(0, ""),
            "uname": _cp(0, "6.18.35-jailbee\n"),
            "lsm": _cp(0, "yama,apparmor"),
        }
    )
    IncusStep(pool_gib=50).run(make_ctx(tmp_path, ac))
    commands = [" ".join(e["argv"]) for kind, e in ac.events if kind == "run"]  # type: ignore[index]
    assert any("admin init --auto --storage-backend dir" in c for c in commands)
    assert any("storage create jailbee-btrfs btrfs size=50GiB" in c for c in commands)


def test_incus_step_names_kernel_step_when_apparmor_missing(tmp_path: Path) -> None:
    ac = FakeAC(
        vm={
            "storage list": _cp(0, "x,y"),
            "uname": _cp(0, "6.18.35-197-debug\n"),
            "lsm": _cp(0, "yama"),
        }
    )
    with pytest.raises(SetupError, match="custom kernel"):
        IncusStep(pool_gib=50).run(make_ctx(tmp_path, ac))


def test_wait_ready_called_before_vm_steps(tmp_path: Path) -> None:
    ac = FakeAC(
        vm={
            "storage list": _cp(0, "a,b\njailbee-btrfs,btrfs"),
            "uname": _cp(0, "x-jailbee"),
            "lsm": _cp(0, "apparmor"),
        }
    )
    IncusStep(pool_gib=50).run(make_ctx(tmp_path, ac))
    assert kinds(ac)[0] == "wait_ready"


def test_root_prepare_contains_securityfs_unit_and_discard() -> None:
    script = (
        resources.files("jailbee.provision").joinpath("mac").joinpath("root-prepare.sh").read_text()
    )
    assert "mount -t securityfs" in script
    assert "mount -o remount,discard /" in script
    assert "fstrim.timer" in script
    assert "kernel.keys.maxkeys=2000" in script
