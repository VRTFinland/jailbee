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
    """GPG support inside containers.

    When enabled, jailbee RO bind-mounts ~/.gnupg, sets SSH_AUTH_SOCK in
    the base profile to the host gpg-agent's SSH socket, and runs the
    doctor check for that socket.

    Defaults to disabled — host gpg-agent setup is personal, so opt-in
    at the global-config layer rather than ambient-on for every repo.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False


class SshConfig(BaseModel):
    """SSH config inside containers.

    When enabled, jailbee bind-mounts <shared_dir>/ssh as the container
    user's ~/.ssh and enforces 0700 on every `jailbee init`.
    `seed_from_host` (default true) controls whether the first init
    copies host ~/.ssh/{config,known_hosts,config.d/} into the
    shared dir. Private keys, authorized_keys and sockets are
    NEVER seeded — keys come from the host gpg-agent.

    Defaults to disabled — explicit opt-in lives in
    ~/.config/jailbee/global.yaml.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    seed_from_host: bool = True


class JetbrainsConfig(BaseModel):
    """JetBrains IDE integration.

    - `enabled`: master switch. Defaults to false; opt-in via
      ~/.config/jailbee/global.yaml. When true, the strict-mode egress
      allowlist is auto-extended with JetBrains' license/plugin/CDN
      hosts so account activation and plugin updates work out of the
      box. When false, `jailbee ide` errors out, the autostart launch is
      suppressed, and `userprefs_from_host` / `toolbox_host_path`
      auto-mounts and all JetBrains egress entries are skipped
      regardless of their individual values.
    - `ide`: which JetBrains binary `jailbee ide` (no --app) and autostart
      launch. The `IdeName` Literal lists supported launchers.
    - `userprefs_from_host`: opt-in RW bind-mount of
      ~/.java/.userPrefs/jetbrains/ (license tokens) into the
      container. Defaults to false — most users don't need it once
      license-host egress is on. Set to true to reuse host-side
      JetBrains Account login state across containers.
    - `share_idea`: opt-out shared-cache mount that persists project
      JetBrains state (.idea/) across containers of the same source
      repo. Defaults to true. Mounts <shared_dir>/jetbrains-idea over
      ~/<container_prefix>/.idea inside each container. Set to false
      if the source repo tracks .idea/* files in VCS that should not
      be shadowed by the mount. Skipped automatically in --mount mode
      so the host's .idea wins.
    - `ai_enabled`: opt-in switch for JetBrains AI Assistant egress
      hosts. Defaults to false. Has no effect when `enabled` is false.
    - `autostart`: launch the IDE after autostart steps complete.
    - `toolbox_host_path`: host path RO-mounted to /opt/jetbrains-toolbox
      (the container-side path is hardcoded in gui.open_ide). Set to
      None to disable the auto-mount.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    ide: IdeName = "idea"
    userprefs_from_host: bool = False
    share_idea: bool = True
    ai_enabled: bool = False
    autostart: bool = False
    toolbox_host_path: PathExpanded | None = Field(
        default_factory=lambda: Path.home() / ".local" / "share" / "JetBrains" / "Toolbox"
    )


# Default host path for the Chrome install. Matches the Debian/Ubuntu
# google-chrome-stable package layout (binary at
# /opt/google/chrome/google-chrome). gui.open_chrome hardcodes the
# container-side path, so the container-side mount target is fixed even
# when the user changes the source path (e.g. to point at chromium).
_DEFAULT_CHROME_HOST_PATH = Path("/opt/google/chrome")


class ChromeConfig(BaseModel):
    """Chrome integration.

    - `enabled`: master switch. Defaults to false; opt-in via
      ~/.config/jailbee/global.yaml. When false, `jailbee chrome` errors out
      and the autostart launch is suppressed regardless of `autostart`.
    - `url`: URL Chrome opens on launch. None = launch with no URL.
      `jailbee chrome <name> <URL>` overrides this per-call.
    - `dark_mode`: pass --force-dark-mode + --enable-features=
      WebContentsForceDark regardless of host GTK theme.
    - `autostart`: launch Chrome after autostart steps complete.
    - `host_path`: host path RO-mounted to /opt/google/chrome (the
      container-side path is hardcoded in gui.open_chrome). Defaults
      to /opt/google/chrome — set to a different path for non-standard
      installs (e.g. chromium), or None to disable the auto-mount.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    url: str | None = None
    dark_mode: bool = False
    autostart: bool = False
    host_path: PathExpanded | None = Field(default_factory=lambda: _DEFAULT_CHROME_HOST_PATH)


class TerminalKittyConfig(BaseModel):
    """Kitty terminal integration (host-side opt-in, container-side terminfo).

    When a developer runs `jailbee shell` / `jailbee tmux` from a kitty terminal on
    the host, `TERM=xterm-kitty` propagates into the container via `incus
    exec`. The base image's terminfo database doesn't ship the `xterm-kitty`
    entry, so curses-aware tools emit `WARNING: terminal is not fully
    functional` and degrade. This block, when active, RO bind-mounts the
    host's `xterm-kitty` terminfo file into every container so the entry
    resolves naturally.

    - `enabled`: ``"auto"`` (default) activates iff the host terminfo file
      can be located. ``True`` activates and fails validation if no file is
      found. ``False`` disables the integration unconditionally.
    - `host_terminfo_path`: explicit host path. When ``None`` (default),
      autodetect probes ``/usr/share/terminfo/x/xterm-kitty``,
      ``~/.local/kitty.app/lib/kitty/terminfo/x/xterm-kitty``, and
      ``~/.terminfo/x/xterm-kitty`` in that order.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: Literal["auto", True, False] = "auto"
    host_terminfo_path: PathExpanded | None = None


class TerminalConfig(BaseModel):
    """Container of terminal-emulator integrations. Currently just kitty."""

    model_config = ConfigDict(extra="forbid")
    kitty: TerminalKittyConfig = TerminalKittyConfig()


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
