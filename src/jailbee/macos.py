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
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NoReturn


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
    # `apple` = an Apple `container machine` set up by `jailbee mac setup`.
    backend: Literal["colima", "apple"] = "colima"
    machine: str = "jailbee"
    mode: Literal["mac-repos", "isolated"] = "mac-repos"
    gui: bool = False


# `container machine run` joins its arguments with spaces and runs them through a
# non-login `bash -c`, so the command reaches it as ONE pre-quoted string, and the
# tools `uv` installs are not on that shell's PATH.
_APPLE_PRELUDE = (
    'export PATH="$HOME/.local/bin:$PATH"; '
    'export XDG_RUNTIME_DIR="/run/user/$(id -u)"; '
    'export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"; '
)


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
    if raw.get("backend") == "apple":
        _apply_apple(cfg, raw)
    return cfg


def _apply_apple(cfg: BridgeConfig, raw: dict[str, object]) -> None:
    """Fill the Apple-machine defaults; explicit transport/tty/workdir keys still win."""
    cfg.backend = "apple"
    cfg.machine = str(raw.get("machine", cfg.machine))
    cfg.mode = "isolated" if raw.get("mode") == "isolated" else "mac-repos"
    cfg.gui = bool(raw.get("gui", False))
    if "transport" not in raw:
        binary = shutil.which("container") or "container"
        cfg.transport = [binary, "machine", "run", "-n", cfg.machine]
    if "tty_flag" not in raw:
        cfg.tty_flag = ["-i", "-t"]
    if "workdir_flag" not in raw:
        cfg.workdir_flag = ""  # the machine maps the working directory itself


def _remote(cfg: BridgeConfig, args: list[str]) -> list[str]:
    """What follows `--` for `args`: the argv itself, or one quoted string for Apple."""
    if cfg.backend != "apple":
        return args
    return [_APPLE_PRELUDE + shlex.join(args)]


def _run_in_vm(cfg: BridgeConfig, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run `args` inside the VM via the transport, capturing output."""
    return subprocess.run(
        [*cfg.transport, "--", *_remote(cfg, args)],
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
    apple = cfg.backend == "apple"
    tool = cfg.transport[0]
    if shutil.which(tool) is None:
        raise BridgeError(
            "Apple's container not found. Install it, then run: jailbee mac setup. "
            "See docs/apple-container-machine.md"
            if apple
            else f"{tool} not found. Install: brew install colima incus. See docs/macos.md"
        )
    if _run_in_vm(cfg, ["true"]).returncode != 0:
        raise BridgeError(
            f"The jailbee machine is not running. Start it: container machine run -n {cfg.machine} -- true"
            if apple
            else "Colima VM not running. Start once:\n"
            "  colima start --runtime=incus --vm-type vz --mount-type virtiofs"
        )
    ver = _run_in_vm(cfg, ["jailbee", "version"])
    if ver.returncode != 0:
        raise BridgeError(
            "jailbee is not installed in the machine. Run: jailbee mac setup"
            if apple
            else "jailbee is not installed in the VM. Run once: jailbee mac bootstrap"
        )
    _warn_on_version_mismatch(ver.stdout.strip())
    if apple and cfg.mode == "isolated":
        return  # the machine has its own repos; the Mac's cwd means nothing there
    if not _is_under(cwd, cfg.shared_root):
        raise BridgeError(
            f"Repo must live under {cfg.shared_root} ("
            + ("the machine shares your home" if apple else "Colima shares $HOME")
            + "). Move the repo or set shared_root in ~/.config/jailbee/macos.yaml"
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


GUI_COMMANDS = frozenset({"dashboard", "tui", "firefox", "chrome", "browser", "ide", "apps"})
"""Commands whose windows must open on the Mac: they go through `waypipe ssh`."""
GUI_SSH_ALIAS = "jailbee-ssh"


def _cocoa_dir() -> Path:
    return Path(os.environ.get("TMPDIR", "/tmp").rstrip("/") or "/tmp") / "cocoa-way"


def _cocoa_socket(directory: Path) -> Path | None:
    import stat

    for candidate in sorted(directory.glob("wayland-*")) if directory.is_dir() else []:
        if stat.S_ISSOCK(candidate.stat().st_mode):
            return candidate
    return None


def _ensure_cocoa_way() -> dict[str, str]:
    """Start Cocoa-Way (rootless) if it is not running; return the env waypipe needs."""
    directory = _cocoa_dir()
    sock = _cocoa_socket(directory)
    if sock is None:
        subprocess.Popen(
            ["cocoa-way"],
            env={**os.environ, "COCOA_WAY_PRESENTATION": "rootless"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(50):
            time.sleep(0.1)
            sock = _cocoa_socket(directory)
            if sock is not None:
                break
    if sock is None:
        raise BridgeError(
            "Cocoa-Way did not start. Install it: brew install cocoa-way waypipe-darwin "
            "(tap J-x-Z/tap), or run jailbee mac setup again."
        )
    return {"XDG_RUNTIME_DIR": str(directory), "WAYLAND_DISPLAY": sock.name}


def _repo_prefix(cfg: BridgeConfig, cwd: Path) -> str | None:
    """The container prefix of the repo in `cwd`, asked of the machine (None if not a repo)."""
    result = _run_in_vm(cfg, ["jailbee", "config", "show"])
    if result.returncode != 0:
        return None
    import yaml

    try:
        data = yaml.safe_load(result.stdout)
    except yaml.YAMLError:
        return None
    prefix = data.get("container_prefix") if isinstance(data, dict) else None
    return str(prefix) if prefix else None


def _delegate_gui(cfg: BridgeConfig, argv: list[str], cwd: Path, *, isatty: bool) -> NoReturn:
    """Run a GUI command through `waypipe ssh` so its windows open on the Mac.

    The restricted SSH service identifies a waypipe session; it selects repos by
    `--repo`, not by working directory, so the prefix is resolved here.
    """
    env = {**os.environ, **_ensure_cocoa_way()}
    remote = list(argv)
    if cfg.mode == "mac-repos" and "--repo" not in remote and (prefix := _repo_prefix(cfg, cwd)):
        remote += ["--repo", prefix]
    cmd = [
        "waypipe", "--compress=zstd", "ssh",
        "-o", "StreamLocalBindUnlink=yes", "-o", "ControlMaster=no",
        *(["-t"] if isatty else []),
        GUI_SSH_ALIAS, *remote,
    ]  # fmt: skip
    raise SystemExit(subprocess.run(cmd, check=False, env=env).returncode)


def _delegate(
    cfg: BridgeConfig, argv: list[str], cwd: Path, *, isatty: bool | None = None
) -> NoReturn:
    """Run `jailbee argv` inside the VM (stdio inherited) and propagate its exit code."""
    if isatty is None:
        isatty = sys.stdin.isatty()
    if cfg.backend == "apple" and cfg.gui and argv and argv[0] in GUI_COMMANDS:
        _delegate_gui(cfg, argv, cwd, isatty=isatty)
    tty = list(cfg.tty_flag) if isatty else (["-i"] if cfg.backend == "apple" else [])
    workdir = [cfg.workdir_flag, str(cwd)] if cfg.workdir_flag else []
    cmd = [*cfg.transport, *tty, *workdir, "--", *_remote(cfg, ["jailbee", *argv])]
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
