"""Host-integration tool models: GPG, SSH, JetBrains, Chrome and terminal
(kitty) support inside containers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
            "hardcoded in ide.resolve_launcher). Set to null to disable the auto-mount."
        ),
    )


# Default host path for a host-sourced Chrome. Matches the Debian/Ubuntu
# google-chrome-stable package layout (binary at
# /opt/google/chrome/google-chrome). The container-side mount target is
# fixed at /opt/google/chrome even when the source path differs, so
# `browsers.py` can derive one binary path per source.
_DEFAULT_CHROME_HOST_PATH = Path("/opt/google/chrome")

BrowserSource = Literal["host", "image"]
"""Where a browser's binary comes from.

`host` RO-bind-mounts an install from the host, the way Chrome has always
worked. `image` installs the browser during `jailbee base build` — the only
source that works for Firefox on Ubuntu, where the host's Firefox is a snap.
"""


def _backfill_chrome_default_host_path(v: object) -> object:
    """Fill in Chrome's default `host_path` when a raw dict omits it.

    A submodel field's own `default_factory` only fires when the whole key
    (e.g. `chrome:`) is absent from the input entirely — a partial dict such
    as `{"enabled": true}` validates straight against `BrowserConfig`, whose
    own `host_path` default is `None` (shared with Firefox, which has no
    host default at all). Without this, `{"chrome": {"enabled": true}}` —
    the ordinary "just turn Chrome on" config — would silently lose the
    standard google-chrome-stable mount.

    Skips the backfill when the dict explicitly sets `source: "image"`: an
    explicit switch away from the host source must not gain a `host_path`,
    or runtime validation would reject the config for setting `host_path`
    under `source: image`.

    Called from `BrowsersConfig.chrome`'s before-validator — its only
    caller now that `Config` no longer has a `chrome:` field of its own
    (see `Config.chrome`, now a read-only property delegating to
    `browsers.chrome`). Kept as a standalone function rather than folded
    into the validator so a future caller could reuse it without needing
    a `BrowsersConfig` instance.
    """
    if not isinstance(v, dict):
        return v
    if v.get("source", "host") != "host":
        return v
    if "host_path" in v:
        return v
    return {**v, "host_path": _DEFAULT_CHROME_HOST_PATH}


def _backfill_firefox_default_source(v: object) -> object:
    """Fill in Firefox's default `source` when a raw dict omits it.

    Firefox defaults to `source: "image"` because on Ubuntu, the host's Firefox
    is a snap and cannot be mounted into a container. This backfill ensures that
    a partial dict such as `{"enabled": true}` — the ordinary "just turn Firefox
    on" config — preserves the image source default rather than falling back to
    the generic BrowserConfig default of "host".

    Similar to `_backfill_chrome_default_host_path`, this compensates for
    Pydantic's behavior: a submodel field's `default_factory` only fires when
    the whole key is absent; a partial dict validates straight against
    BrowserConfig, bypassing the BrowsersConfig.firefox default_factory.
    """
    if not isinstance(v, dict):
        return v
    if "source" in v:
        return v
    return {**v, "source": "image"}


class BrowserConfig(BaseModel):
    """One browser inside containers."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(
        default=False,
        description=(
            "Master switch for this browser. Off by default; opt in via "
            "~/.config/jailbee/global.yaml. When false, the browser's command errors "
            "out, it is hidden from `jailbee apps ls`, and its autostart launch is "
            "suppressed regardless of `autostart`."
        ),
    )
    source: BrowserSource = Field(
        default="host",
        description=(
            "`host` RO-mounts an existing host install (see `host_path`); `image` "
            "installs the browser into the golden image during `jailbee base build`, "
            "which needs no host install at all. Changing this needs "
            "`jailbee base build` (image) or `jailbee apply` (host) to take effect."
        ),
    )
    host_path: PathExpanded | None = Field(
        default=None,
        description=(
            "Host path RO-mounted into the container when `source: host`. Must be "
            "null when `source: image`. Chrome defaults to the standard "
            "google-chrome-stable install path; Firefox has no default because on "
            "Ubuntu the host's Firefox is a snap and is not usefully mountable."
        ),
    )
    url: str | None = Field(
        default=None,
        description=(
            "URL the browser opens on launch. None launches with no URL; passing a "
            "URL on the command line overrides this per call."
        ),
    )
    dark_mode: bool = Field(
        default=False,
        description=(
            "Force a dark browser theme. Chrome gets --force-dark-mode and "
            "--enable-features=WebContentsForceDark, which darkens page content too. "
            "Firefox has no equivalent flag, so it gets GTK_THEME=Adwaita:dark, which "
            "darkens the browser UI only — pages stay as the site renders them."
        ),
    )
    autostart: bool = Field(
        default=False,
        description=(
            "Launch this browser after autostart steps complete. Has no effect when "
            "`enabled` is false."
        ),
    )


class BrowsersConfig(BaseModel):
    """Browsers available inside containers."""

    model_config = ConfigDict(extra="forbid")
    default: Literal["chrome", "firefox"] | None = Field(
        default=None,
        description=(
            "Which browser `jailbee browser` opens. None (default) resolves at command "
            "time: the single enabled browser if there is exactly one, otherwise the "
            "command asks you to set this. Naming a disabled browser is a config error."
        ),
    )
    chrome: BrowserConfig = Field(
        default_factory=lambda: BrowserConfig(host_path=_DEFAULT_CHROME_HOST_PATH),
        description=(
            "Google Chrome. Sourced from a host mount by default, matching the "
            "standard google-chrome-stable install path."
        ),
    )
    firefox: BrowserConfig = Field(
        default_factory=lambda: BrowserConfig(source="image"),
        description=(
            "Mozilla Firefox. Sourced from the golden image by default, because on "
            "Ubuntu the host's Firefox is a snap and cannot be mounted into a container."
        ),
    )

    def enabled_names(self) -> list[str]:
        """Enabled browsers in registry order — never in YAML key order.

        `jailbee apps ls`, the dashboard action menu and the implicit
        `browsers.default` all read this, so the order must not depend on
        how a user happened to write their YAML.
        """
        return [n for n in ("chrome", "firefox") if getattr(self, n).enabled]

    @field_validator("chrome", mode="before")
    @classmethod
    def _default_chrome_host_path(cls, v: object) -> object:
        return _backfill_chrome_default_host_path(v)

    @field_validator("firefox", mode="before")
    @classmethod
    def _default_firefox_source(cls, v: object) -> object:
        return _backfill_firefox_default_source(v)


# Kept as a name for one release so `from jailbee.config import ChromeConfig`
# keeps working while `chrome:` is still an accepted alias. Retire in 1.4.0
# together with the alias itself.
ChromeConfig = BrowserConfig


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
