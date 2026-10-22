"""Tests for the GUI step (Cocoa-Way, waypipe, JailBee's SSH service). Fully mocked."""

from __future__ import annotations

import subprocess as _sp
from pathlib import Path

import pytest
import yaml

from jailbee.macsetup.engine import MacPaths, SetupContext, SetupError, SetupOptions, SetupState
from jailbee.macsetup.steps_gui import GuiStep, merge_remote_yaml


def _cp(rc: int = 0, out: str = "", err: str = "") -> _sp.CompletedProcess[str]:
    return _sp.CompletedProcess([], rc, stdout=out, stderr=err)


class FakeAC:
    def __init__(self, global_yaml: str = "") -> None:
        self.runs: list[dict[str, object]] = []
        self._global = global_yaml

    def machine_run(self, name, argv, *, root=False, input=None):  # type: ignore[no-untyped-def]
        self.runs.append({"argv": argv, "input": input})
        return _cp(
            0, self._global if "cat" in " ".join(argv) and "global.yaml" in " ".join(argv) else ""
        )


def make(tmp_path: Path, *, gui: bool = True, yes: bool = True, interactive: bool = True):  # type: ignore[no-untyped-def]
    ac = FakeAC()
    paths = MacPaths(tmp_path, tmp_path / "s.json", tmp_path / "k", tmp_path / "ssh")
    (tmp_path / "ssh").mkdir(exist_ok=True)
    (tmp_path / "ssh" / "id_ed25519.pub").write_text("ssh-ed25519 AAAAKEY x\n")
    ctx = SetupContext(
        ac=ac,  # type: ignore[arg-type]
        state=SetupState(options=SetupOptions(gui=gui, assume_yes=yes)),
        paths=paths,
        interactive=interactive,
        confirm=lambda q: True,
        log=lambda line: None,
    )
    return ctx, ac


def test_gui_off_is_a_noop_and_cheap(tmp_path: Path) -> None:
    ctx, ac = make(tmp_path, gui=False)
    step = GuiStep(which=lambda name: None)
    assert step.fingerprint(ctx) == "off"
    step.run(ctx)
    assert ac.runs == []


def test_missing_brew_suggests_no_gui(tmp_path: Path) -> None:
    ctx, _ = make(tmp_path)
    with pytest.raises(SetupError) as excinfo:
        GuiStep(which=lambda name: None).run(ctx)
    assert "--no-gui" in (excinfo.value.remedy or "")


def test_missing_tools_are_installed_from_the_tap_after_asking(tmp_path: Path) -> None:
    ctx, _ = make(tmp_path)
    ran: list[list[str]] = []
    present = {"brew": "/opt/homebrew/bin/brew"}
    step = GuiStep(which=present.get, runner=lambda argv, **kw: ran.append(argv) or _cp())
    step.run(ctx)
    assert ["brew", "tap", "J-x-Z/tap"] in ran
    assert ["brew", "install", "cocoa-way", "waypipe-darwin"] in ran


def test_non_interactive_install_needs_yes(tmp_path: Path) -> None:
    ctx, _ = make(tmp_path, yes=False, interactive=False)
    with pytest.raises(SetupError) as excinfo:
        GuiStep(which={"brew": "/b"}.get).run(ctx)
    assert excinfo.value.exit_code == 2


def test_service_is_enabled_with_the_mac_key_and_host_restrictions_off(tmp_path: Path) -> None:
    ctx, ac = make(tmp_path)
    present = {"brew": "/b", "cocoa-way": "/c", "waypipe": "/w"}
    GuiStep(which=present.get).run(ctx)
    joined = [" ".join(str(a) for a in r["argv"]) for r in ac.runs]
    key = next(r for r in ac.runs if "remote ssh key add -" in " ".join(r["argv"]))  # type: ignore[arg-type]
    assert "AAAAKEY" in str(key["input"])
    assert any("remote ssh enable" in c for c in joined)
    write = next(r for r in ac.runs if r["input"] and "restrict_host" in str(r["input"]))
    assert yaml.safe_load(write["input"])["remote"]["ssh"]["restrict_host"] is False  # type: ignore[arg-type]
    assert any("remote ssh restart" in c for c in joined)


def test_merge_remote_yaml_keeps_other_keys_and_user_choice() -> None:
    merged = yaml.safe_load(merge_remote_yaml("defaults:\n  memory: 4GiB\n"))
    assert merged["defaults"] == {"memory": "4GiB"}
    assert merged["remote"]["ssh"]["restrict_host"] is False
    kept = yaml.safe_load(merge_remote_yaml("remote:\n  ssh:\n    restrict_host: true\n"))
    assert kept["remote"]["ssh"]["restrict_host"] is True
