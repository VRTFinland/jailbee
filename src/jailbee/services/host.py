"""Host-side service operations; definitions always come from the container."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from jailbee.config import CONTAINER_USERNAME

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus


def _metadata(cfg: Config, incus: Incus, full: str, args: Sequence[str]) -> Any:
    from jailbee.container_app import ContainerJailbeeError, run_in_container
    from jailbee.lifecycle import container_repo_dir

    repo = container_repo_dir(cfg, incus, full)
    result = run_in_container(
        cfg, incus, full, ["service", "--repo", repo, *args], uid=cfg.container_user.uid,
        cwd=repo, env={"HOME": f"/home/{CONTAINER_USERNAME}"}, init_groups=True,
    )
    if result.returncode:
        raise ContainerJailbeeError(result.stderr.decode(errors="replace").strip() or "container service command failed")
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ContainerJailbeeError("invalid service metadata from container") from exc


def service_names(cfg: Config, incus: Incus, full: str) -> list[str]:
    return [str(item["name"]) for item in _metadata(cfg, incus, full, ["ls", "--json"])]


def elevated_refresh_refusal(cfg: Config, incus: Incus, full: str, names: Sequence[str]) -> str | None:
    payload = _metadata(cfg, incus, full, ["plan", *names, "--op", "refresh", "--json"])
    elevated = []
    for service in payload["services"]:
        access = service["elevation"]
        if not service["will_prepare"]:
            continue
        details = []
        if access["network"] == "loose":
            details.append("network loose")
        if access["mounts"]:
            details.append("mounts " + ", ".join(access["mounts"]))
        if details:
            elevated.append(f"{service['name']}: {'; '.join(details)}")
    if elevated:
        return f"elevated refresh ({'; '.join(elevated)}) is not available yet — it needs the agent stopped and resumed (planned)"
    return None


def run_op(cfg: Config, incus: Incus, full: str, op: str, names: Sequence[str], *, extra: Sequence[str] = ()) -> int:
    from jailbee.container_app import run_in_container_interactive
    from jailbee.lifecycle import container_repo_dir

    repo = container_repo_dir(cfg, incus, full)
    return run_in_container_interactive(cfg, incus, full, ["service", "--repo", repo, op, *names, *extra], uid=cfg.container_user.uid, cwd=repo)
