"""Setup preflight: refuse early, in plain words, before anything is built."""

from __future__ import annotations

import os
import platform
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from jailbee.macsetup.engine import SetupContext, SetupError, SetupOptions, ask

MIN_CONTAINER = (1, 6, 0)
RELEASE_URL = "https://github.com/apple/container/releases"
MIN_KERNEL = (6, 18)
"""Older default kernels lack nf_tables (apple/container#2120), which strict mode needs."""
GIB = 1024**3
FAIL_BELOW_GIB = 15
WARN_BELOW_GIB = 40


@dataclass(frozen=True)
class HostFacts:
    arch: str
    ncpu: int
    memsize: int
    free_bytes: int

    @classmethod
    def detect(cls) -> HostFacts:
        pages, page_size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
        return cls(
            arch=platform.machine(),
            ncpu=os.cpu_count() or 2,
            memsize=pages * page_size,
            free_bytes=shutil.disk_usage(Path.home()).free,
        )


def resolve_resources(opts: SetupOptions, ncpu: int, memsize: int) -> tuple[int, str]:
    """Machine CPUs and memory: the user's choice, else half the Mac (floors 2 and 4G)."""
    cpus = opts.cpus if opts.cpus is not None else max(2, ncpu // 2)
    memory = opts.memory if opts.memory is not None else f"{max(4, memsize // GIB // 2)}G"
    return cpus, memory


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _kernel_tuple(release: str) -> tuple[int, ...]:
    head = release.split("-", 1)[0]
    return tuple(int(part) for part in head.split(".")[:2])


class PreflightStep:
    id = "preflight"
    title = "Checking the Mac"

    def __init__(self, host: HostFacts | None = None) -> None:
        self._host = host

    def fingerprint(self, ctx: SetupContext) -> str:
        return uuid.uuid4().hex  # cheap, and a changed Mac must never be skipped

    def run(self, ctx: SetupContext) -> None:
        host = self._host or HostFacts.detect()
        if host.arch != "arm64":
            raise SetupError(
                "Apple's container needs an Apple silicon Mac.",
                remedy="Use the Colima setup instead: docs/macos.md#setup-b-a-linux-vm-on-the-mac-colima",
            )
        self._check_cli(ctx)
        self._check_kernel(ctx)
        self._check_disk(ctx, host)
        self._check_machine_name(ctx)
        cpus, memory = resolve_resources(ctx.state.options, host.ncpu, host.memsize)
        ctx.state.facts["cpus"], ctx.state.facts["memory"] = str(cpus), memory

    def _check_cli(self, ctx: SetupContext) -> None:
        version = ctx.ac.version()
        if version is None:
            raise SetupError(
                "Apple's container is not installed.",
                remedy=f"Install the signed package from {RELEASE_URL}, then re-run.",
            )
        if _version_tuple(version) < MIN_CONTAINER:
            needed = ".".join(map(str, MIN_CONTAINER))
            raise SetupError(
                f"Apple's container {version} is too old; {needed} or newer is needed.",
                remedy=f"Install a newer package from {RELEASE_URL}.",
            )
        if not ctx.ac.system_running():
            raise SetupError(
                "Apple's container service is not running.",
                remedy="Run: container system start --enable-kernel-install",
            )

    def _check_kernel(self, ctx: SetupContext) -> None:
        release = ctx.ac.default_kernel_release()
        if release is None or _kernel_tuple(release) < MIN_KERNEL:
            command = "container system kernel set --recommended --force"
            if not ask(
                ctx,
                f"Apple's default kernel ({release or 'none'}) is out of date. "
                "Replace it with the recommended one? This changes the default for every "
                "container on this Mac.",
                command,
            ):
                raise SetupError("Setup needs the recommended kernel as a starting point.", command)
            ctx.ac.set_default_kernel_recommended()
            release = ctx.ac.default_kernel_release()
        if release is None:
            raise SetupError("Apple's default kernel is still missing after the update.")
        ctx.state.facts["kernel_release"] = release

    def _check_disk(self, ctx: SetupContext, host: HostFacts) -> None:
        free_gib = host.free_bytes // GIB
        if free_gib < FAIL_BELOW_GIB:
            raise SetupError(
                f"Only {free_gib} GiB free; setup needs at least {FAIL_BELOW_GIB} GiB "
                f"({WARN_BELOW_GIB} GiB or more is comfortable).",
                remedy="Free some disk space and re-run.",
            )
        if free_gib < WARN_BELOW_GIB:
            ctx.log(f"warning: only {free_gib} GiB free; {WARN_BELOW_GIB} GiB is comfortable")

    def _check_machine_name(self, ctx: SetupContext) -> None:
        name = ctx.state.options.machine
        if ctx.ac.machine(name) is not None and ctx.state.facts.get("machine") != name:
            raise SetupError(
                f"A machine named {name} already exists and was not created by jailbee setup.",
                remedy=f"Pick another name: jailbee mac setup --machine {name}-jb",
            )
