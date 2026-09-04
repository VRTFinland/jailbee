"""Host-integration tool models: GPG, SSH, JetBrains, Chrome and terminal
(kitty) support inside containers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from jailbee.config.common import PathExpanded
from jailbee.config.models_golden import IdeName


class GpgConfig(BaseModel):
    """GPG support inside containers."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(
        default=False,
        description=(
            "When true, RO bind-mounts ~/.gnupg, wires SSH_AUTH_SOCK to the host "
            "gpg-agent's SSH socket, and enables the doctor's gpg-agent socket check. "
            "Off by default — host gpg-agent setup is personal, so this is an explicit "
            "opt-in at the global-config layer rather than ambient-on for every repo."
        ),
    )


class SshConfig(BaseModel):
    """SSH config inside containers."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(
        default=False,
        description=(
            "When true, bind-mounts <shared_dir>/ssh as the container user's ~/.ssh and "
            "enforces 0700 on every `jailbee init` (SSH refuses looser permissions). Off by "
            "default — explicit opt-in lives in ~/.config/jailbee/global.yaml."
        ),
    )
    seed_from_host: bool = Field(
        default=True,
        description=(
            "When true (default), the first `jailbee init` copies host "
            "~/.ssh/{config,known_hosts,config.d/} into the shared dir. Private keys, "
            "authorized_keys, and sockets are never seeded — keys come from the host "
            "gpg-agent instead. Has no effect when `enabled` is false."
        ),
    )


class JetbrainsConfig(BaseModel):
    """JetBrains IDE integration."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(
        default=False,
        description=(
            "Master switch. Off by default; opt in via ~/.config/jailbee/global.yaml. When "
            "true, the strict-mode egress allowlist is auto-extended with JetBrains' "
            "license/plugin/CDN hosts. When false, `jailbee ide` errors out, the autostart "
            "IDE launch is suppressed, and the other fields below (and their egress entries) "
            "are skipped regardless of their own values."
        ),
    )
    ide: IdeName = Field(
        default="idea",
        description=(
            "Which JetBrains binary `jailbee ide` (with no --app) and autostart launch use. "
            "Limited to the launchers listed in the `IdeName` literal."
        ),
    )
    userprefs_from_host: bool = Field(
        default=False,
        description=(
            "Opt-in RW bind-mount of ~/.java/.userPrefs/jetbrains/ (license tokens) into the "
            "container, so host-side JetBrains Account login state is reused across "
            "containers. Off by default — most setups don't need it once license-host "
            "egress is on. Has no effect when `enabled` is false."
        ),
    )
    share_idea: bool = Field(
        default=True,
        description=(
            "Mounts <shared_dir>/jetbrains-idea over ~/<container_prefix>/.idea so project "
            "JetBrains state persists across containers of the same repo. On by default; set "
            "to false if the repo tracks .idea/* files in VCS that shouldn't be shadowed by "
            "the mount. Skipped automatically in --mount mode so the host's .idea wins."
        ),
    )
    ai_enabled: bool = Field(
        default=False,
        description=(
            "Opt-in switch that also auto-extends the strict-mode egress allowlist with the "
            "JetBrains AI Assistant backend hosts. Off by default. Has no effect when "
            "`enabled` is false."
        ),
    )
    autostart: bool = Field(
        default=False,
        description="Launch the IDE after autostart steps complete. Has no effect when "
        "`enabled` is false.",
    )
    toolbox_host_path: PathExpanded | None = Field(
        default_factory=lambda: Path.home() / ".local" / "share" / "JetBrains" / "Toolbox",
        description=(
            "Host path RO-mounted to /opt/jetbrains-toolbox (the container-side path is "
            "hardcoded in gui.open_ide). Set to null to disable the auto-mount."
        ),
    )


# Default host path for the Chrome install. Matches the Debian/Ubuntu
# google-chrome-stable package layout (binary at
# /opt/google/chrome/google-chrome). gui.open_chrome hardcodes the
# container-side path, so the container-side mount target is fixed even
# when the user changes the source path (e.g. to point at chromium).
_DEFAULT_CHROME_HOST_PATH = Path("/opt/google/chrome")


class ChromeConfig(BaseModel):
    """Chrome integration."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(
        default=False,
        description=(
            "Master switch. Off by default; opt in via ~/.config/jailbee/global.yaml. When "
            "false, `jailbee chrome` errors out and the autostart launch is suppressed "
            "regardless of `autostart`."
        ),
    )
    url: str | None = Field(
        default=None,
        description=(
            "URL Chrome opens on launch. None launches with no URL; "
            "`jailbee chrome <name> <URL>` overrides this per call."
        ),
    )
    dark_mode: bool = Field(
        default=False,
        description=(
            "Pass --force-dark-mode and --enable-features=WebContentsForceDark so Chrome "
            "ignores the host's own GTK theme."
        ),
    )
    autostart: bool = Field(
        default=False,
        description="Launch Chrome after autostart steps complete. Has no effect when "
        "`enabled` is false.",
    )
    host_path: PathExpanded | None = Field(
        default_factory=lambda: _DEFAULT_CHROME_HOST_PATH,
        description=(
            "Host path RO-mounted to /opt/google/chrome (the container-side path is "
            "hardcoded in gui.open_chrome). Defaults to the standard google-chrome-stable "
            "install path — point elsewhere for a non-standard install (e.g. chromium), or "
            "set to null to disable the auto-mount."
        ),
    )


class TerminalKittyConfig(BaseModel):
    """Kitty terminal integration (host-side opt-in, container-side terminfo).

    When a developer runs `jailbee shell` / `jailbee tmux` from a kitty terminal on
    the host, `TERM=xterm-kitty` propagates into the container via `incus
    exec`. The base image's terminfo database doesn't ship the `xterm-kitty`
    entry, so curses-aware tools emit `WARNING: terminal is not fully
    functional` and degrade. This block, when active, RO bind-mounts the
    host's `xterm-kitty` terminfo file into every container so the entry
    resolves naturally.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: Literal["auto", True, False] = Field(
        default="auto",
        description=(
            '"auto" (default) activates iff the host terminfo file can be located. `True` '
            "activates and fails validation if no file is found. `False` disables the "
            "integration unconditionally."
        ),
    )
    host_terminfo_path: PathExpanded | None = Field(
        default=None,
        description=(
            "Explicit host path to the xterm-kitty terminfo file. When None (default), "
            "autodetect probes /usr/share/terminfo/x/xterm-kitty, "
            "~/.local/kitty.app/lib/kitty/terminfo/x/xterm-kitty, and ~/.terminfo/x/xterm-kitty "
            "in that order."
        ),
    )


class TerminalConfig(BaseModel):
    """Container of terminal-emulator integrations. Currently just kitty."""

    model_config = ConfigDict(extra="forbid")
    kitty: TerminalKittyConfig = Field(
        default=TerminalKittyConfig(),
        description=(
            "Kitty terminal integration settings — the only terminal emulator support so far."
        ),
    )


def _kitty_terminfo_candidates() -> list[Path]:
    """Ordered list of host paths jailbee probes for the kitty terminfo entry.

    1. Distro package (``kitty-terminfo`` on Debian/Ubuntu/Fedora).
    2. Kitty's official ``installer.sh`` user-local layout.
    3. User-installed via ``tic``.
    """
    home = Path.home()
    return [
        Path("/usr/share/terminfo/x/xterm-kitty"),
        home / ".local/kitty.app/lib/kitty/terminfo/x/xterm-kitty",
        home / ".terminfo/x/xterm-kitty",
    ]


def resolve_kitty_terminfo_path(*, explicit: Path | None) -> Path | None:
    """Return an existing host terminfo file path, or None.

    Explicit-path mode: returns the path iff it exists. Autodetect mode:
    returns the first existing candidate from ``_kitty_terminfo_candidates``.
    """
    if explicit is not None:
        return explicit if explicit.exists() else None
    for cand in _kitty_terminfo_candidates():
        if cand.exists():
            return cand
    return None
