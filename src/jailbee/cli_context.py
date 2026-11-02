"""Context detection shared by the host and container entry points."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

Context = Literal["host", "container"]
MARKER = Path("/etc/jailbee/container")
APP_MOUNT = "/opt/jailbee/app"
EXIT_SKEW = 86
EXIT_DEPS = 87
EXIT_NOT_MOUNTED = 88
SHARED_COMMANDS = frozenset({"config", "version"})
CONTAINER_COMMANDS: frozenset[str] = frozenset()
HOST_COMMANDS: frozenset[str] = frozenset(
    {
        "_autostart-worker",
        "_boot-worker",
        "_broker",
        "_destroy-worker",
        "_new-worker",
        "_remote-console",
        "_state-service",
        "account",
        "apply",
        "apps",
        "autostart",
        "base",
        "branch",
        "browser",
        "checkout",
        "chrome",
        "chrome-pool",
        "claude",
        "console",
        "dashboard",
        "destroy",
        "diff",
        "disk-usage",
        "dismiss",
        "display",
        "doctor",
        "egress",
        "exec",
        "fetch",
        "firefox",
        "fork",
        "git",
        "gui",
        "ide",
        "init",
        "issue",
        "job",
        "list",
        "litellm",
        "ls",
        "mem",
        "merge",
        "mount",
        "net",
        "new",
        "orchestrate",
        "orphan",
        "outbox",
        "pool",
        "port",
        "pr",
        "prune",
        "pull",
        "push",
        "registry",
        "remote",
        "rename",
        "restart",
        "retarget",
        "review",
        "setup",
        "shell",
        "snapshot",
        "start",
        "stop",
        "submodule",
        "tmux",
        "tui",
        "unmount",
        "upgrade",
    }
)


def detect(
    env: Mapping[str, str] | None = None,
    marker: Path = MARKER,
    package_file: str | None = None,
) -> Context:
    override = (os.environ if env is None else env).get("JAILBEE_CONTEXT")
    if override == "host":
        return "host"
    if override == "container":
        return "container"
    path = Path(package_file if package_file is not None else __file__)
    if marker.exists() and path.is_relative_to(APP_MOUNT):
        return "container"
    return "host"


def wrong_side_message(name: str, context: Context) -> str | None:
    if context == "container" and name in HOST_COMMANDS:
        return f"`jailbee {name}` runs on the host, not inside a container."
    if context == "host" and name in CONTAINER_COMMANDS:
        return f"`jailbee {name}` is only available inside a jailbee container."
    return None
