"""Tests for the kernel build step. Fully mocked — no real build."""

from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path

import pytest

from jailbee.macsetup.engine import MacPaths, SetupContext, SetupError, SetupOptions, SetupState
from jailbee.macsetup.steps_kernel import (
    KERNEL_RECIPE,
    KernelStep,
    kernel_path,
    kernel_source_version,
)

IMAGE_BYTES = b"fake-arm64-image"


class FakeAC:
    """run_ephemeral plays the kernel-build container: it writes into the /out volume."""

    def __init__(self, scenario: str = "ok") -> None:
        self.scenario = scenario
        self.calls: list[dict[str, object]] = []

    def run_ephemeral(self, image, argv, *, volumes, cpus, memory) -> int:  # type: ignore[no-untyped-def]
        self.calls.append({"image": image, "argv": argv, "volumes": dict(volumes), "cpus": cpus})
        out = next(host for host, target in volumes.items() if target == "/out")
        version = argv[-1]
        partial = out / f"vmlinux-{version}-jailbee.partial"
        sha = out / f"vmlinux-{version}-jailbee.sha256"
        if self.scenario == "crash":
            partial.write_bytes(IMAGE_BYTES[:4])  # half-written
            return 1
        partial.write_bytes(IMAGE_BYTES)
        digest = hashlib.sha256(IMAGE_BYTES).hexdigest()
        sha.write_text(("0" * 64 if self.scenario == "badsha" else digest) + "\n")
        return 0


def make_ctx(tmp_path: Path, ac: FakeAC) -> SetupContext:
    paths = MacPaths(tmp_path / "cfg", tmp_path / "cfg" / "s.json", tmp_path / "kernels", tmp_path / "ssh")
    return SetupContext(
        ac=ac,  # type: ignore[arg-type]
        state=SetupState(
            options=SetupOptions(),
            facts={"kernel_release": "6.18.35-197-debug", "cpus": "4", "memory": "8G"},
        ),
        paths=paths,
        interactive=True,
        confirm=lambda q: True,
        log=lambda line: None,
    )


def test_source_version_strips_build_suffix() -> None:
    assert kernel_source_version("6.18.35-197-debug") == "6.18.35"
    assert kernel_source_version("6.18.35") == "6.18.35"


def test_kernel_path_is_under_kernels_dir_with_version(tmp_path: Path) -> None:
    paths = MacPaths(tmp_path, tmp_path / "s", tmp_path / "k", tmp_path / "ssh")
    assert kernel_path(paths, "6.18.35") == tmp_path / "k" / "vmlinux-6.18.35-jailbee"


def test_fingerprint_follows_source_version_and_recipe(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, FakeAC())
    assert KernelStep().fingerprint(ctx) == f"6.18.35+r{KERNEL_RECIPE}"
    ctx.state.facts["kernel_release"] = "6.18.40-200"
    assert KernelStep().fingerprint(ctx) == f"6.18.40+r{KERNEL_RECIPE}"


def test_successful_build_renames_partial_and_records_facts(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, FakeAC())
    KernelStep().run(ctx)
    final = kernel_path(ctx.paths, "6.18.35")
    assert final.read_bytes() == IMAGE_BYTES
    assert ctx.state.facts["kernel_path"] == str(final)
    assert ctx.state.facts["kernel_sha256"] == hashlib.sha256(IMAGE_BYTES).hexdigest()
    assert not list(ctx.paths.kernels_dir.glob("*.partial"))


def test_build_runs_in_throwaway_container_with_resolved_resources(tmp_path: Path) -> None:
    ac = FakeAC()
    KernelStep().run(make_ctx(tmp_path, ac))
    call = ac.calls[0]
    assert call["image"] == "ubuntu:26.04"
    assert call["cpus"] == 4
    assert call["argv"][-1] == "6.18.35"  # type: ignore[index]


def test_partial_file_is_never_selected(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, FakeAC("crash"))
    with pytest.raises(SetupError, match="did not finish"):
        KernelStep().run(ctx)
    assert "kernel_path" not in ctx.state.facts
    assert not kernel_path(ctx.paths, "6.18.35").exists()


def test_sha_mismatch_fails(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, FakeAC("badsha"))
    with pytest.raises(SetupError, match="checksum"):
        KernelStep().run(ctx)
    assert "kernel_path" not in ctx.state.facts
    assert not kernel_path(ctx.paths, "6.18.35").exists()


def test_script_enables_the_four_options_and_checks_them() -> None:
    script = (
        resources.files("jailbee.provision").joinpath("mac").joinpath("kernel-build.sh").read_text()
    )
    for option in ("NF_CONNTRACK_BRIDGE", "SECURITY_APPARMOR", "BTRFS_FS", "BTRFS_FS_POSIX_ACL"):
        assert f"--enable {option}" in script
        assert option in script.split("make olddefconfig", 1)[1].split("make -j", 1)[0]
