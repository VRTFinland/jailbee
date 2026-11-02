"""Stage the host package for the in-container launcher."""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee import __version__
from jailbee.cli_context import EXIT_NOT_MOUNTED, EXIT_SKEW
from jailbee.paths import xdg_data_home

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import ExecResult, Incus


@dataclass(frozen=True)
class StageResult:
    path: Path
    changed: bool


class ContainerJailbeeError(RuntimeError):
    """The in-container launcher cannot run the requested version."""


def staging_root() -> Path:
    return xdg_data_home() / "jailbee" / "container-app"


@cache
def code_hash() -> str:
    package = Path(__file__).parent
    digest = hashlib.sha256()
    digest.update(__version__.encode() + b"\0")
    for path in sorted(package.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            digest.update(str(path.relative_to(package)).encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def stage(*, force: bool = False, root: Path | None = None) -> StageResult:
    root = root if root is not None else staging_root()
    root.mkdir(parents=True, exist_ok=True)
    name = "app-" + code_hash()
    target = root / name
    with (root / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = root / "current"
        previous = root / os.readlink(current) if current.is_symlink() else None
        already = previous == target and target.is_dir()
        if already and not force:
            return StageResult(target, False)
        if not target.exists():
            temporary = Path(tempfile.mkdtemp(prefix=name + ".tmp-", dir=root))
            try:
                shutil.copytree(
                    Path(__file__).parent,
                    temporary / "jailbee",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
                (temporary / "VERSION").write_text(f"{__version__} {code_hash()}\n")
                for path in temporary.rglob("*"):
                    path.chmod(path.stat().st_mode | (0o555 if path.is_dir() else 0o444))
                temporary.chmod(0o755)
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        link = root / (".current-" + uuid.uuid4().hex)
        try:
            link.symlink_to(name)
            os.replace(link, current)
        finally:
            link.unlink(missing_ok=True)
        versions = sorted(
            (p for p in root.glob("app-*") if p.is_dir() and ".tmp-" not in p.name and p != target),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
        keep = previous if previous in versions else (versions[0] if versions else None)
        for old in versions:
            if old != keep:
                shutil.rmtree(old)
        return StageResult(target, not already)


def profile_device() -> dict[str, dict[str, str]]:
    return {
        "jailbee-app": {
            "type": "disk",
            "source": str(staging_root()),
            "path": "/opt/jailbee/app",
            "readonly": "true",
        }
    }


def run_in_container(
    cfg: Config, incus: Incus, full: str, args: Sequence[str], *, uid: int | None
) -> ExecResult:
    expected = f"{__version__} {code_hash()}"
    command = ["jailbee", "--expect-version", expected, *args]
    result = incus.exec_bytes(full, command, uid=uid)
    if result.returncode == EXIT_SKEW:
        stage(force=True)
        from jailbee.agent_skills import sync_agent_skills

        sync_agent_skills(cfg)
        result = incus.exec_bytes(full, command, uid=uid)
        if result.returncode == EXIT_SKEW:
            their = result.stderr.decode(errors="replace").strip() or "unknown"
            raise ContainerJailbeeError(
                f"container still sees jailbee {their} after restaging {expected}"
            )
    if result.returncode == EXIT_NOT_MOUNTED or b"not mounted" in result.stderr.lower():
        raise ContainerJailbeeError("in-container jailbee is not mounted - run `jb apply` on the host")
    if result.returncode == 127:
        raise ContainerJailbeeError(
            "this container's image has no in-container jailbee - run `jb base build`, "
            "then recreate the container"
        )
    return result
