"""Build the Linux kernel the machine boots.

Apple's default kernel is Kata's and lacks `CONFIG_NF_CONNTRACK_BRIDGE` (strict
mode fails without it), AppArmor and btrfs. The kernel is built from the running
default kernel's configuration plus those options, in a throwaway container on
the Mac so no toolchain stays in the VM.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from importlib import resources
from pathlib import Path

from jailbee.macsetup.engine import MacPaths, SetupContext, SetupError

KERNEL_RECIPE = 1
"""Bump when `kernel-build.sh` changes which options it sets, so the kernel is rebuilt."""
BUILD_IMAGE = "ubuntu:26.04"
_SCRIPT = "kernel-build.sh"


def kernel_source_version(release: str) -> str:
    """`6.18.35-197-debug` -> `6.18.35`: the upstream version to download."""
    return release.split("-", 1)[0]


def kernel_path(paths: MacPaths, version: str) -> Path:
    return paths.kernels_dir / f"vmlinux-{version}-jailbee"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class KernelStep:
    id = "kernel"
    title = "Building the Linux kernel (10-20 minutes)"

    def fingerprint(self, ctx: SetupContext) -> str:
        return f"{kernel_source_version(ctx.state.facts['kernel_release'])}+r{KERNEL_RECIPE}"

    def run(self, ctx: SetupContext) -> None:
        version = kernel_source_version(ctx.state.facts["kernel_release"])
        target = kernel_path(ctx.paths, version)
        partial = target.with_name(target.name + ".partial")
        sha_file = target.with_name(target.name + ".sha256")
        ctx.paths.kernels_dir.mkdir(parents=True, exist_ok=True)
        for stale in (partial, sha_file):
            stale.unlink(missing_ok=True)

        script = resources.files("jailbee.provision").joinpath("mac").joinpath(_SCRIPT)
        with tempfile.TemporaryDirectory() as scripts_dir:
            (Path(scripts_dir) / _SCRIPT).write_text(script.read_text())
            code = ctx.ac.run_ephemeral(
                BUILD_IMAGE,
                ["sh", f"/scripts/{_SCRIPT}", version],
                volumes={ctx.paths.kernels_dir: "/out", Path(scripts_dir): "/scripts"},
                cpus=int(ctx.state.facts["cpus"]),
                memory=ctx.state.facts["memory"],
            )
        if code != 0 or not partial.exists() or not sha_file.exists():
            partial.unlink(missing_ok=True)
            raise SetupError(
                "The kernel build did not finish.",
                remedy="Check the output above, make sure there is network access and "
                "free disk space, then re-run.",
            )
        expected = sha_file.read_text().strip()
        if _sha256(partial) != expected:
            partial.unlink(missing_ok=True)
            raise SetupError(
                "The built kernel's checksum does not match; the file was discarded.",
                remedy="Re-run setup to build it again.",
            )
        os.replace(partial, target)
        sha_file.unlink(missing_ok=True)
        ctx.state.facts["kernel_path"] = str(target)
        ctx.state.facts["kernel_sha256"] = expected
