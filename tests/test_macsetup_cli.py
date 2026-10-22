"""Tests for the `jailbee mac setup|upgrade|doctor|uninstall` command. Fully mocked."""

from __future__ import annotations

import subprocess as _sp
from pathlib import Path

import pytest

from jailbee.mac_container import MachineInfo
from jailbee.macsetup import cli
from jailbee.macsetup.engine import (
    MacPaths,
    SetupContext,
    SetupOptions,
    SetupState,
    load_state,
    save_state,
)


def _cp(rc: int = 0, out: str = "", err: str = "") -> _sp.CompletedProcess[str]:
    return _sp.CompletedProcess([], rc, stdout=out, stderr=err)


class FakeAC:
    def __init__(self, machine: MachineInfo | None = None) -> None:
        self._machine = machine
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.runs = []
        self.fail_rm = False

    def machine(self, name: str) -> MachineInfo | None:
        return self._machine

    def machine_stop(self, name: str) -> None:
        self.calls.append(("stop", (name,)))

    def machine_rm(self, name: str) -> None:
        if self.fail_rm:
            from jailbee.mac_container import AppleContainerError

            raise AppleContainerError("disk busy")
        self.calls.append(("rm", (name,)))

    def image_rm(self, tag: str) -> None:
        self.calls.append(("image_rm", (tag,)))

    def machine_run(self, name, argv, *, root=False, input=None):  # type: ignore[no-untyped-def]
        self.runs.append(" ".join(argv))
        return _cp(0, "1.7.8\n")

    def wait_ready(self, name: str) -> None:
        pass

    def system_running(self) -> bool:
        return self.service_up

    runs: list[str]
    service_up = True


class RecStep:
    def __init__(self, id: str, fingerprint: str = "1") -> None:
        self.id, self.title, self._fp, self.runs = id, f"Step {id}", fingerprint, 0

    def fingerprint(self, ctx: SetupContext) -> str:
        return self._fp

    def run(self, ctx: SetupContext) -> None:
        self.runs += 1


@pytest.fixture
def env(tmp_path: Path):  # type: ignore[no-untyped-def]
    paths = MacPaths(
        tmp_path / "cfg",
        tmp_path / "cfg" / "setup-state.json",
        tmp_path / "k",
        tmp_path / "cfg" / "ssh",
    )
    return {
        "paths": paths,
        "user_ssh_config": tmp_path / "dotssh" / "config",
        "macos_yaml": tmp_path / "jb" / "macos.yaml",
        "interactive": False,
    }


def test_second_run_reuses_stored_options(env) -> None:  # type: ignore[no-untyped-def]
    steps = [RecStep("a")]
    assert (
        cli.main(
            ["setup", "--isolated", "--machine", "m1", "--no-gui", "--cpus", "3"],
            ac=FakeAC(),
            steps=steps,
            **env,
        )
        == 0
    )
    assert cli.main(["setup"], ac=FakeAC(), steps=steps, **env) == 0
    options = load_state(env["paths"].state_file).options  # type: ignore[union-attr]
    assert (options.mode, options.machine, options.gui, options.cpus) == (
        "isolated",
        "m1",
        False,
        3,
    )
    assert steps[0].runs == 1  # the second run found it up to date


def test_mode_change_on_existing_machine_refused(env, capsys) -> None:  # type: ignore[no-untyped-def]
    steps = [RecStep("a")]
    assert cli.main(["setup"], ac=FakeAC(), steps=steps, **env) == 0
    existing = MachineInfo("jailbee", True, None, "rw")
    assert cli.main(["setup", "--isolated"], ac=FakeAC(existing), steps=steps, **env) == 1
    assert "jailbee mac uninstall" in capsys.readouterr().out


def test_upgrade_is_setup_with_stored_options(env) -> None:  # type: ignore[no-untyped-def]
    steps = [RecStep("a", "v1")]
    cli.main(["setup", "--no-gui"], ac=FakeAC(), steps=steps, **env)
    steps[0]._fp = "v2"
    assert cli.main(["upgrade"], ac=FakeAC(), steps=steps, **env) == 0
    assert steps[0].runs == 2
    assert load_state(env["paths"].state_file).options.gui is False  # type: ignore[union-attr]


def test_doctor_reports_stale_after_version_bump(env, capsys) -> None:  # type: ignore[no-untyped-def]
    steps = [RecStep("a", "v1"), RecStep("b", "v1")]
    machine = MachineInfo("jailbee", True, None, "rw")
    cli.main(["setup"], ac=FakeAC(machine), steps=steps, **env)
    assert cli.main(["doctor"], ac=FakeAC(machine), steps=steps, **env) == 0
    steps[1]._fp = "v2"
    assert cli.main(["doctor"], ac=FakeAC(machine), steps=steps, **env) == 1
    out = capsys.readouterr().out
    assert "b" in out and "stale" in out


def test_doctor_without_state_says_to_run_setup(env, capsys) -> None:  # type: ignore[no-untyped-def]
    assert cli.main(["doctor"], ac=FakeAC(), steps=[RecStep("a")], **env) == 1
    assert "jailbee mac setup" in capsys.readouterr().out


def test_uninstall_removes_include_line_only(env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ssh_config = env["user_ssh_config"]
    ssh_config.parent.mkdir(parents=True)
    ssh_config.write_text("Include ~/.config/jailbee/mac/ssh/config\n\nHost keep\n    User me\n")
    env["macos_yaml"].parent.mkdir(parents=True, exist_ok=True)
    env["macos_yaml"].write_text("backend: apple\n")
    (env["macos_yaml"].parent / "macos.yaml.colima.bak").write_text("transport: [colima, ssh]\n")
    kernel = tmp_path / "k" / "vmlinux-6.18.35-jailbee"
    kernel.parent.mkdir(parents=True)
    kernel.write_bytes(b"k")
    state = SetupState(
        options=SetupOptions(),
        facts={
            "machine": "jailbee",
            "image": "local/jailbee-machine:abc",
            "kernel_path": str(kernel),
        },
    )
    save_state(env["paths"].state_file, state)
    ac = FakeAC(MachineInfo("jailbee", True, None, "rw"))
    assert cli.main(["uninstall", "--yes"], ac=ac, steps=[], **env) == 0
    assert ssh_config.read_text() == "Host keep\n    User me\n"
    assert [c[0] for c in ac.calls] == ["stop", "rm", "image_rm"]
    assert not kernel.exists()
    assert not kernel.parent.exists()  # the emptied kernels directory goes too
    assert not env["paths"].state_file.exists()
    assert not env["paths"].config_dir.exists()  # nothing of ours is left behind
    assert env["macos_yaml"].read_text() == "transport: [colima, ssh]\n"


def test_non_interactive_confirmation_exits_2(env) -> None:  # type: ignore[no-untyped-def]
    save_state(
        env["paths"].state_file, SetupState(options=SetupOptions(), facts={"machine": "jailbee"})
    )
    assert cli.main(["uninstall"], ac=FakeAC(), steps=[], **env) == 2


def test_default_steps_order() -> None:
    ids = [step.id for step in cli.default_steps()]
    assert ids == [
        "preflight",
        "kernel",
        "image",
        "machine",
        "prepare",
        "incus",
        "jailbee",
        "wiring",
        "gui",
        "base",
        "verify",
    ]


def _uninstall_setup(env, tmp_path: Path):  # type: ignore[no-untyped-def]
    kernel = tmp_path / "k" / "vmlinux-6.18.35-jailbee"
    kernel.parent.mkdir(parents=True)
    kernel.write_bytes(b"k")
    save_state(
        env["paths"].state_file,
        SetupState(
            options=SetupOptions(),
            facts={"machine": "jailbee", "image": "local/x:1", "kernel_path": str(kernel)},
        ),
    )
    return kernel


def test_uninstall_keeps_everything_when_the_service_is_down(env, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    kernel = _uninstall_setup(env, tmp_path)
    ac = FakeAC(MachineInfo("jailbee", True, None, "rw"))
    ac.service_up = False
    assert cli.main(["uninstall", "--yes"], ac=ac, steps=[], **env) == 1
    assert kernel.exists() and env["paths"].state_file.exists()
    assert "container system start" in capsys.readouterr().out


def test_uninstall_keeps_state_when_the_machine_cannot_be_removed(env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    kernel = _uninstall_setup(env, tmp_path)
    ac = FakeAC(MachineInfo("jailbee", True, None, "rw"))
    ac.fail_rm = True
    assert cli.main(["uninstall", "--yes"], ac=ac, steps=[], **env) == 1
    assert kernel.exists() and env["paths"].state_file.exists()


def test_uninstall_leaves_a_colima_macos_yaml_alone(env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _uninstall_setup(env, tmp_path)
    env["macos_yaml"].parent.mkdir(parents=True, exist_ok=True)
    env["macos_yaml"].write_text("transport: [colima, ssh]\n")  # no backup: wiring never ran
    assert cli.main(["uninstall", "--yes"], ac=FakeAC(), steps=[], **env) == 0
    assert env["macos_yaml"].read_text() == "transport: [colima, ssh]\n"


def test_verify_removes_a_leftover_smoke_container_first(tmp_path: Path) -> None:
    ac = FakeAC()
    paths = MacPaths(tmp_path, tmp_path / "s.json", tmp_path / "k", tmp_path / "ssh")
    ctx = SetupContext(
        ac=ac,  # type: ignore[arg-type]
        state=SetupState(options=SetupOptions()),
        paths=paths,
        interactive=False,
        confirm=lambda q: True,
        log=lambda line: None,
    )
    cli.VerifyStep().run(ctx)
    script = next(r for r in ac.runs if "jailbee new mac-setup-smoke" in r)
    assert script.index("jailbee destroy mac-setup-smoke --force") < script.index(
        "jailbee new mac-setup-smoke"
    )
