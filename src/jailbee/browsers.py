"""Builtin browser specs: Chrome and Firefox as registry entries.

The per-browser differences live here and nowhere else — where the binary
sits for each `source`, how each one is told to go dark, and which profile
pool keeps two containers from fighting over one profile directory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee.apps import AppSpec
from jailbee.gui import host_is_wayland

if TYPE_CHECKING:
    from jailbee.config import Config

BROWSER_BINARIES: dict[tuple[str, str], str] = {
    ("chrome", "host"): "/opt/google/chrome/google-chrome",
    ("chrome", "image"): "/usr/bin/google-chrome-stable",
    ("firefox", "host"): "/opt/firefox/firefox",
    ("firefox", "image"): "/usr/bin/firefox",
}
"""Container-side binary per (browser, source).

A host-sourced browser is reached through its bind-mount target, which is
fixed by `Config.effective_host_mounts` regardless of where the host install
actually lives. An image-sourced one is reached through the path its apt
package installs to.
"""

BROWSER_POOLS: dict[str, str] = {"chrome": "chrome-profile", "firefox": "firefox-profile"}


def builtin_specs(cfg: Config) -> list[AppSpec]:
    """One `AppSpec` per enabled browser, in registry order."""
    specs: list[AppSpec] = []
    for name in cfg.browsers.enabled_names():
        browser = getattr(cfg.browsers, name)
        command = [BROWSER_BINARIES[(name, browser.source)]]
        env: dict[str, str] = {}
        if name == "chrome":
            if host_is_wayland():
                # Chrome defaults to X11 even with WAYLAND_DISPLAY set; the
                # Ozone backend has to be named explicitly.
                command.append("--ozone-platform=wayland")
            if browser.dark_mode:
                command += ["--force-dark-mode", "--enable-features=WebContentsForceDark"]
        elif name == "firefox" and browser.dark_mode:
            env["GTK_THEME"] = "Adwaita:dark"
        specs.append(
            AppSpec(
                name=name,
                command=command,
                cwd="home",
                env=env,
                pool=BROWSER_POOLS[name],
                top_level=True,
                autostart=browser.autostart,
                source="builtin",
                description=f"{name.capitalize()} ({browser.source})",
                accepts_url=True,
                # Not baked into `command`: `apps.launch` appends this only
                # when no explicit URL is given at launch time, so a caller
                # who does pass one replaces it instead of joining it.
                # `effective_url`, not `browser.url`: the shared
                # `browsers.url` applies to whichever browsers do not name
                # one of their own.
                default_url=cfg.browsers.effective_url(name),
            )
        )
    return specs
