"""Tests for installing JailBee inside the machine. Fully mocked."""

from __future__ import annotations

import subprocess as _sp
from pathlib import Path

import yaml

from jailbee.macsetup.engine import MacPaths, SetupContext, SetupOptions, SetupState
from jailbee.macsetup.steps_jailbee import (
    BaseImageStep,
    JailbeeStep,
    install_command,
    merge_global_yaml,
)


def _cp(rc: int = 0, out: str = "", err: str = "") -> _sp.CompletedProcess[str]:
    return _sp.CompletedProcess([], rc, stdout=out, stderr=err)


class FakeAC:
    def __init__(self, global_yaml: str = "") -> None:
        self.runs: list[dict[str, object]] = []
        self._global = global_yaml

    def machine_run(self, name, argv, *, root=False, input=None):  # type: ignore[no-untyped-def]
        self.runs.append({"argv": argv, "root": root, "input": input})
        if argv[:1] == ["cat"]:
            return _cp(0 if self._global else 1, self._global)
        return _cp()

    def wait_ready(self, name):  # type: ignore[no-untyped-def]
        pass


def make_ctx(tmp_path: Path, ac: FakeAC) -> SetupContext:
    paths = MacPaths(tmp_path, tmp_path / "s.json", tmp_path / "k", tmp_path / "ssh")
    return SetupContext(
        ac=ac,  # type: ignore[arg-type]
        state=SetupState(options=SetupOptions(), facts={"kernel_path": "/k/vmlinux"}),
        paths=paths,
        interactive=True,
        confirm=lambda q: True,
        log=lambda line: None,
    )


def commands(ac: FakeAC) -> list[str]:
    return [" ".join(str(a) for a in r["argv"]) for r in ac.runs]  # type: ignore[union-attr]


def test_installs_mac_version_from_pypi() -> None:
    assert install_command("1.7.8") == "uv tool install --force 'jailbee[ssh]==1.7.8'"


def test_dev_version_falls_back_to_git_with_warning() -> None:
    warnings: list[str] = []
    cmd = install_command("1.7.9.dev3+gabc1234", warn=warnings.append)
    assert "git+https://github.com/VRTFinland/jailbee" in cmd
    assert warnings and "1.7.9.dev3" in warnings[0]


def test_global_yaml_merge_keeps_user_keys() -> None:
    existing = "defaults:\n  memory: 4GiB\nbrowsers:\n  chrome:\n    enabled: true\n"
    merged = yaml.safe_load(merge_global_yaml(existing))
    assert merged["defaults"] == {"memory": "4GiB", "storage_pool": "jailbee-btrfs"}
    assert merged["browsers"]["chrome"] == {"enabled": True}
    assert merged["browsers"]["firefox"] == {"enabled": True}


def test_global_yaml_merge_does_not_override_choices() -> None:
    existing = "defaults:\n  storage_pool: mine\nbrowsers:\n  firefox:\n    enabled: false\n"
    merged = yaml.safe_load(merge_global_yaml(existing))
    assert merged["defaults"]["storage_pool"] == "mine"
    assert merged["browsers"]["firefox"]["enabled"] is False


def test_jailbee_step_installs_uv_then_jailbee_then_writes_global_yaml(tmp_path: Path) -> None:
    ac = FakeAC()
    JailbeeStep(mac_version="1.7.8").run(make_ctx(tmp_path, ac))
    cmds = commands(ac)
    assert any("command -v uv" in c for c in cmds)
    assert any("uv tool install --force 'jailbee[ssh]==1.7.8'" in c for c in cmds)
    write = next(r for r in ac.runs if "global.yaml" in " ".join(r["argv"]) and r["input"])  # type: ignore[arg-type]
    assert yaml.safe_load(write["input"])["defaults"]["storage_pool"] == "jailbee-btrfs"  # type: ignore[arg-type]
    assert any("jailbee setup --yes --only timer" in c for c in cmds)


def test_jailbee_fingerprint_is_mac_version(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, FakeAC())
    assert JailbeeStep(mac_version="1.7.8").fingerprint(ctx).startswith("1.7.8:")


def test_base_step_reruns_on_jailbee_upgrade_or_new_kernel(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, FakeAC())
    before = BaseImageStep(mac_version="1.7.8").fingerprint(ctx)
    assert BaseImageStep(mac_version="1.7.9").fingerprint(ctx) != before
    ctx.state.facts["kernel_path"] = "/k/other"
    assert BaseImageStep(mac_version="1.7.8").fingerprint(ctx) != before


DOCTOR_OK = """\
  | kernel bridge conntrack | ✓ OK   | enabled |
  | apparmor               | ✓ OK   | active  |
  | git identity           | ✗ FAIL | no user.name |
"""
DOCTOR_BAD = DOCTOR_OK.replace(
    "kernel bridge conntrack | ✓ OK   | enabled", "kernel bridge conntrack | ✗ FAIL | missing"
)


class DoctorAC(FakeAC):
    def __init__(self, doctor: str) -> None:
        super().__init__()
        self._doctor = doctor

    def machine_run(self, name, argv, *, root=False, input=None):  # type: ignore[no-untyped-def]
        result = super().machine_run(name, argv, root=root, input=input)
        if "jailbee doctor" in " ".join(argv):
            return _cp(1, self._doctor)  # doctor exits 1 for unrelated rows (git identity...)
        return result


def test_base_step_builds_in_scratch_dir_as_the_login_user(tmp_path: Path) -> None:
    ac = DoctorAC(DOCTOR_OK)
    BaseImageStep(mac_version="1.7.8").run(make_ctx(tmp_path, ac))
    build = next(r for r in ac.runs if "jailbee base build" in " ".join(r["argv"]))  # type: ignore[arg-type]
    assert build["root"] is True and "jailbee-scratch" in " ".join(build["argv"])  # type: ignore[arg-type]


def test_base_step_ignores_unrelated_doctor_failures(tmp_path: Path) -> None:
    BaseImageStep(mac_version="1.7.8").run(make_ctx(tmp_path, DoctorAC(DOCTOR_OK)))  # no raise


def test_base_step_fails_on_a_kernel_row(tmp_path: Path) -> None:
    import pytest

    from jailbee.macsetup.engine import SetupError

    with pytest.raises(SetupError, match="kernel bridge conntrack"):
        BaseImageStep(mac_version="1.7.8").run(make_ctx(tmp_path, DoctorAC(DOCTOR_BAD)))


def test_install_spec_override_wins_for_testing_a_branch(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(
        "JAILBEE_MAC_INSTALL_SPEC", "/Users/me/dist/jailbee-1.7.8-py3-none-any.whl[ssh]"
    )
    assert install_command("1.7.8") == (
        "uv tool install --force '/Users/me/dist/jailbee-1.7.8-py3-none-any.whl[ssh]'"
    )


def test_user_commands_drop_from_root_to_the_login_user(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A plain machine-run session has no supplementary groups, so no incus-admin."""
    monkeypatch.setattr("getpass.getuser", lambda: "me")
    ac = FakeAC()
    JailbeeStep(mac_version="1.7.8").run(make_ctx(tmp_path, ac))
    installs = [r for r in ac.runs if "uv tool install" in " ".join(r["argv"])]  # type: ignore[arg-type]
    assert installs and installs[0]["root"] is True
    assert installs[0]["argv"][:5] == ["runuser", "-u", "me", "--", "bash"]  # type: ignore[index]


def test_mac_repos_mode_trusts_the_shared_repositories(tmp_path: Path) -> None:
    """On the Mac share, file owners seen in the machine flicker between the login
    user and root (observed on a real Mac), so git reports 'dubious ownership'."""
    ac = FakeAC()
    JailbeeStep(mac_version="1.7.8").run(make_ctx(tmp_path, ac))
    assert any("safe.directory '*'" in c for c in commands(ac))


def test_isolated_mode_does_not_loosen_git(tmp_path: Path) -> None:
    ac = FakeAC()
    ctx = make_ctx(tmp_path, ac)
    ctx.state.options.mode = "isolated"
    JailbeeStep(mac_version="1.7.8").run(ctx)
    assert not any("safe.directory" in c for c in commands(ac))
