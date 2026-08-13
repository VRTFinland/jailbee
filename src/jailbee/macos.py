"""macOS delegation bridge.

jailbee cannot run natively on macOS: the Incus daemon is Linux-only, and Incus
resolves `disk` device sources on the daemon side, so even a native macOS
`incus` client cannot bind-mount a macOS path into a container. This module
re-executes the real jailbee inside a Linux VM (Colima by default) against the repo
shared into that VM. It is the ONLY macOS-specific code; the core is untouched.

Self-contained by design: stdlib + lazy PyYAML + lazy __version__ only. Never
imports cli.py, Typer, or any incus-oriented module.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn


class BridgeError(Exception):
    """Raised when the macOS->VM bridge cannot proceed.

    The message is user-facing remediation (what to run), not a stack trace.
    """


@dataclass
class BridgeConfig:
    transport: list[str] = field(default_factory=lambda: ["colima", "ssh"])
    tty_flag: list[str] = field(default_factory=lambda: ["-t"])
    workdir_flag: str = "--workdir"
    shared_root: Path = field(default_factory=Path.home)


def _config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "jailbee" / "macos.yaml"


def load_bridge_config(path: Path | None = None) -> BridgeConfig:
    """Load the bridge config; return defaults if the file is absent."""
    path = path or _config_path()
    cfg = BridgeConfig()
    if not path.exists():
        return cfg
    import yaml

    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise BridgeError(f"{path}: top level must be a mapping")
    if "transport" in raw:
        cfg.transport = [str(x) for x in raw["transport"]]
        if not cfg.transport:
            raise BridgeError(
                "transport must be a non-empty list; check ~/.config/jailbee/macos.yaml"
            )
    if "tty_flag" in raw:
        cfg.tty_flag = [str(x) for x in raw["tty_flag"]]
    if "workdir_flag" in raw:
        cfg.workdir_flag = str(raw["workdir_flag"])
    if "shared_root" in raw:
        cfg.shared_root = Path(str(raw["shared_root"])).expanduser()
    return cfg


def _run_in_vm(cfg: BridgeConfig, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run `args` inside the VM via the transport, capturing output."""
    return subprocess.run(
        [*cfg.transport, "--", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def preflight(cfg: BridgeConfig, cwd: Path) -> None:
    """Verify the bridge can run; raise BridgeError with remediation if not."""
    tool = cfg.transport[0]
    if shutil.which(tool) is None:
        raise BridgeError(
            f"{tool} not found. Install: brew install colima incus. See docs/macos.md"
        )
    if _run_in_vm(cfg, ["true"]).returncode != 0:
        raise BridgeError(
            "Colima VM not running. Start once:\n"
            "  colima start --runtime=incus --vm-type vz --mount-type virtiofs"
        )
    ver = _run_in_vm(cfg, ["jailbee", "version"])
    if ver.returncode != 0:
        raise BridgeError("jailbee is not installed in the VM. Run once: jailbee mac bootstrap")
    _warn_on_version_mismatch(ver.stdout.strip())
    if not _is_under(cwd, cfg.shared_root):
        raise BridgeError(
            f"Repo must live under {cfg.shared_root} (Colima shares $HOME). "
            "Move the repo or set shared_root in ~/.config/jailbee/macos.yaml"
        )


def _warn_on_version_mismatch(vm_version: str) -> None:
    if not vm_version:
        return
    from jailbee import __version__

    if vm_version != __version__:
        print(
            f"warning: VM jailbee {vm_version} differs from macOS jailbee {__version__}; "
            "run jailbee mac bootstrap to sync",
            file=sys.stderr,
        )


def _delegate(
    cfg: BridgeConfig, argv: list[str], cwd: Path, *, isatty: bool | None = None
) -> NoReturn:
    """Run `jailbee argv` inside the VM (stdio inherited) and propagate its exit code."""
    if isatty is None:
        isatty = sys.stdin.isatty()
    tty = list(cfg.tty_flag) if isatty else []
    cmd = [*cfg.transport, *tty, cfg.workdir_flag, str(cwd), "--", "jailbee", *argv]
    result = subprocess.run(cmd, check=False)
    raise SystemExit(result.returncode)


def maybe_delegate(argv: list[str], *, platform: str = sys.platform) -> None:
    """On macOS, hand off to the VM (or a local `mac` command). No-op elsewhere."""
    if platform != "darwin":
        return
    if argv and argv[0] == "mac":
        raise SystemExit(_run_mac_command(argv[1:]))
    cfg = load_bridge_config()
    try:
        preflight(cfg, Path.cwd())
    except BridgeError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from e
    _delegate(cfg, argv, Path.cwd())


def _run_mac_command(args: list[str]) -> int:
    if not args:
        print("usage: jailbee mac {doctor|bootstrap}", file=sys.stderr)
        return 2
    sub, cfg = args[0], load_bridge_config()
    if sub == "doctor":
        return _mac_doctor(cfg)
    if sub == "bootstrap":
        return _mac_bootstrap(cfg)
    print(f"usage: jailbee mac {{doctor|bootstrap}} (got: {sub})", file=sys.stderr)
    return 2


def _mac_doctor(cfg: BridgeConfig) -> int:
    try:
        preflight(cfg, Path.cwd())
    except BridgeError as e:
        print(str(e), file=sys.stderr)
        return 1
    print("jailbee mac: OK — VM reachable, jailbee installed, cwd under shared mount")
    return 0


_INSTALL_SPEC = "git+https://github.com/VRTFinland/jailbee"


def _mac_bootstrap(cfg: BridgeConfig) -> int:
    """Install/upgrade jailbee inside the already-running VM. Does not touch Colima."""
    # Ensure uv exists (login shell so ~/.local/bin is on PATH), then install jailbee.
    ensure_uv = "command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh"
    subprocess.run([*cfg.transport, "--", "sh", "-lc", ensure_uv], check=False)
    install = subprocess.run(
        [*cfg.transport, "--", "sh", "-lc", f"uv tool install --force {_INSTALL_SPEC}"],
        check=False,
    )
    if install.returncode == 0:
        print(f"jailbee installed in the VM from {_INSTALL_SPEC}")
    else:
        print("jailbee install failed in the VM; check the VM has network egress", file=sys.stderr)
    return install.returncode
