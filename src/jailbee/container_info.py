"""A snapshot of a container's identity, written inside it at creation.

An agent in a container has no `jailbee` binary and no Incus socket, yet it
often has to tell the operator which `jailbee` command to run on the host.
`/etc/jailbee/container.json` gives it the name and the few facts those
commands are built from.

It is a snapshot of `jailbee new` time, never refreshed. State that changes
later (network mode, alias, credential group) is deliberately left out
rather than risk it going stale.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from jailbee.lifecycle import short_name

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

CONTAINER_INFO_FILE = "/etc/jailbee/container.json"
_VERSION = 1


def build_payload(
    cfg: Config,
    name: str,
    *,
    mode: str,
    branch: str | None,
    base_branch: str,
    repo_dir: str,
    pr: int | None,
    fork_of: str | None,
) -> dict[str, Any]:
    """The JSON document for ``name``; ``branch`` is None in mount mode."""
    return {
        "version": _VERSION,
        "name": name,
        "short_name": short_name(cfg, name),
        "container_prefix": cfg.container_prefix,
        "mode": mode,
        "branch": branch,
        "base_branch": base_branch,
        "repo_dir": repo_dir,
        "pr": pr,
        "fork_of": fork_of,
    }


def write_container_info(incus: Incus, name: str, payload: dict[str, Any]) -> None:
    """Install ``payload`` as `/etc/jailbee/container.json`, world-readable."""
    body = json.dumps(payload, indent=2)
    script = "\n".join(
        [
            "set -euo pipefail",
            "mkdir -p /etc/jailbee",
            "tmp=$(mktemp)",
            "cat > \"$tmp\" <<'JB_EOF'",
            body,
            "JB_EOF",
            f'chmod 0644 "$tmp"; mv "$tmp" {CONTAINER_INFO_FILE}',
        ]
    )
    incus.exec_with_input(name, ["bash", "-s"], script + "\n", timeout=30)
