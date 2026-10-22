"""The machine itself: create it, prepare its Linux host, initialise Incus."""

from __future__ import annotations

import getpass
import hashlib
import shutil
from importlib import resources
from pathlib import Path

from jailbee.macsetup.engine import SetupContext, SetupError

GIB = 1024**3
POOL = "jailbee-btrfs"
_MIN_POOL_GIB, _MAX_POOL_GIB = 20, 200


def _provision(name: str) -> str:
    return resources.files("jailbee.provision").joinpath("mac").joinpath(name).read_text()


def _packages() -> str:
    lines = (line.strip() for line in _provision("packages.txt").splitlines())
    return " ".join(line for line in lines if line and not line.startswith("#"))


def _vm(ctx: SetupContext, argv: list[str], *, input: str | None = None) -> str:
    """Run `argv` as root in the machine; a failure becomes a SetupError with its stderr."""
    result = ctx.ac.machine_run(ctx.state.options.machine, argv, root=True, input=input)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SetupError(
            f"`{' '.join(argv[:3])}` failed in the machine: {detail}" if detail else
            f"`{' '.join(argv[:3])}` failed in the machine.",
            remedy="Check the message above, then re-run.",
        )
    return result.stdout


def _wait_ready(ctx: SetupContext) -> None:
    ctx.ac.wait_ready(ctx.state.options.machine)


class MachineStep:
    id = "machine"
    title = "Creating the machine"

    def fingerprint(self, ctx: SetupContext) -> str:
        return f"{ctx.state.facts['kernel_path']}|{ctx.state.options.mode}"

    def run(self, ctx: SetupContext) -> None:
        opts, facts = ctx.state.options, ctx.state.facts
        name, kernel = opts.machine, facts["kernel_path"]
        home_mount = "rw" if opts.mode == "mac-repos" else "none"
        info = ctx.ac.machine(name)
        if info is None:
            ctx.ac.machine_create(
                name,
                facts["image"],
                cpus=int(facts["cpus"]),
                memory=facts["memory"],
                home_mount=home_mount,
                kernel=Path(kernel),
            )
            facts["machine"] = name
        else:
            if info.home_mount != home_mount:
                raise SetupError(
                    f"The machine shares the Mac home as '{info.home_mount}' but this setup "
                    f"wants '{home_mount}'; the mode cannot change on an existing machine.",
                    remedy="Run: jailbee mac uninstall, then set up again in the other mode.",
                )
            if info.kernel != kernel:
                ctx.ac.machine_set(name, kernel=kernel)
                ctx.ac.machine_stop(name)
                ctx.ac.machine_run(name, ["true"])  # boots it with the new kernel
        _wait_ready(ctx)


class PrepareStep:
    id = "prepare"
    title = "Preparing the machine's Linux host"

    def __init__(self, dev_user: str | None = None, packages: str | None = None) -> None:
        self._user = dev_user or getpass.getuser()
        self._packages = packages if packages is not None else _packages()

    def fingerprint(self, ctx: SetupContext) -> str:
        data = _provision("root-prepare.sh") + "\0" + self._packages
        return hashlib.sha256(data.encode()).hexdigest()[:12]

    def run(self, ctx: SetupContext) -> None:
        _wait_ready(ctx)
        env = [f"DEV_USER={self._user}", f"PACKAGES={self._packages}"]
        _vm(ctx, ["env", *env, "bash", "-s"], input=_provision("root-prepare.sh"))


def default_pool_gib() -> int:
    """Half the Mac's free disk, within 20..200 GiB. A ceiling: the pool file is sparse."""
    free_gib = shutil.disk_usage(Path.home()).free // GIB
    return max(_MIN_POOL_GIB, min(_MAX_POOL_GIB, free_gib // 2))


class IncusStep:
    id = "incus"
    title = "Setting up Incus and its btrfs pool"

    def __init__(self, pool_gib: int | None = None) -> None:
        self._pool_gib = pool_gib

    def fingerprint(self, ctx: SetupContext) -> str:
        return "1"

    def run(self, ctx: SetupContext) -> None:
        _wait_ready(ctx)
        pools = _vm(ctx, ["incus", "storage", "list", "-f", "csv"])
        if not pools.strip():
            _vm(ctx, ["incus", "admin", "init", "--auto", "--storage-backend", "dir"])
        if f"{POOL}," not in pools:
            size = self._pool_gib if self._pool_gib is not None else default_pool_gib()
            _vm(ctx, ["incus", "storage", "create", POOL, "btrfs", f"size={size}GiB"])
        release = _vm(ctx, ["uname", "-r"]).strip()
        lsm = _vm(ctx, ["cat", "/sys/kernel/security/lsm"])
        if not release.endswith("-jailbee") or "apparmor" not in lsm:
            raise SetupError(
                f"The machine is running kernel {release}, not the custom kernel with AppArmor.",
                remedy="Re-run setup; if it persists, run: jailbee mac doctor",
            )
