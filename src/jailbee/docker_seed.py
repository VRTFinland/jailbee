"""Copy tagged Docker images into a disposable golden staging instance."""

from __future__ import annotations

import fnmatch
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from jailbee import golden
from jailbee.golden_sharing import shared_golden_reason_for
from jailbee.incus import IncusError
from jailbee.tui import info, warn
from jailbee.stopping import stop_container

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus


@dataclass(frozen=True)
class SeedRequest:
    source_container: str | None
    keep_seed: bool
    explicit: bool


@dataclass(frozen=True)
class SeedOutcome:
    outcome: str
    source: str
    image_count: int = 0
    size_bytes: int = 0
    detail: str = ""


class SeedPreflightError(Exception):
    """The requested seed cannot safely be started."""


def preflight(cfg: Config, incus: Incus, req: SeedRequest) -> None:
    if not golden.repo_uses_docker(cfg):
        raise SeedPreflightError("Docker is not installed in the golden")
    reason = shared_golden_reason_for(cfg)
    if reason:
        raise SeedPreflightError(f"Cannot seed shared golden: {reason}")
    name = req.source_container
    try:
        if name is None:
            if incus.exists(f"{cfg.container_prefix}-docker-seed"):
                raise SeedPreflightError("The docker-seed container already exists")
        else:
            containers = incus.list_containers()
            source = next((c for c in containers if c.get("name") == name), None)
            if source is None:
                raise SeedPreflightError(f"Source container {name} does not exist")
            if str(source.get("status", "")).lower() != "running":
                raise SeedPreflightError(f"Source container {name} is not running")
            incus.exec(name, ["docker", "info"], timeout=15)
    except IncusError as exc:
        raise SeedPreflightError(f"Docker seed preflight failed: {exc}") from exc


def _remaining(deadline: float) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Docker seeding timed out")
    return max(1, math.ceil(remaining))


def list_tagged_images(
    incus: Incus, name: str, globs: list[str], *, deadline: float | None = None
) -> list[str]:
    timeout = _remaining(deadline) if deadline is not None else None
    output = incus.exec(name, ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"], timeout=timeout)
    refs = []
    for line in output.splitlines():
        ref = line.strip()
        if not ref or "<none>" in ref or (globs and not any(fnmatch.fnmatchcase(ref, g) for g in globs)):
            continue
        if ref not in refs:
            refs.append(ref)
    return refs


def _wait_docker(incus: Incus, name: str, deadline: float) -> None:
    for attempt in range(30):
        try:
            incus.exec(name, ["docker", "info"], timeout=min(5, _remaining(deadline)))
            return
        except IncusError:
            if attempt == 29:
                raise
            time.sleep(min(1, _remaining(deadline)))
    raise TimeoutError("Docker did not become ready")


def _image_size(output: str) -> int:
    # Docker's JSON format is one object per line; Size is human-readable.
    for line in output.splitlines():
        row = json.loads(line)
        if row.get("Type") != "Images":
            continue
        match = re.fullmatch(r"\s*([\d.]+)\s*([KMGTPE]?i?B)\s*", row.get("Size", ""), re.I)
        if not match:
            raise ValueError("Cannot parse Docker image size")
        unit = match.group(2).upper()
        powers = {"B": 0, "KB": 1, "MB": 2, "GB": 3, "TB": 4, "PB": 5, "EB": 6}
        binary = "I" in unit
        power = powers[unit.replace("I", "")]
        return int(float(match.group(1)) * (1024 if binary else 1000) ** power)
    raise ValueError("Docker image size missing from system df")


def record_outcome(cfg: Config, outcome: SeedOutcome) -> None:
    from sqlmodel import Session

    from jailbee import docker_seed_state
    from jailbee.db import get_engine

    with Session(get_engine()) as session:
        docker_seed_state.record(session, alias=cfg.golden.alias, container_prefix=cfg.container_prefix,
                                 seeded_at=datetime.now(UTC), source=outcome.source, outcome=outcome.outcome,
                                 image_count=outcome.image_count, size_bytes=outcome.size_bytes,
                                 detail=outcome.detail)
        session.commit()


def seed_golden(
    cfg: Config, incus: Incus, req: SeedRequest, *, deadline: float,
    source_name: str | None = None,
) -> SeedOutcome:
    """Seed from req's full container name, or task 8's ephemeral source_name.

    source_name supplies the full ephemeral container name without changing req,
    so persisted provenance remains seed-container rather than container:<short>.
    deadline is an absolute time.monotonic() deadline.
    """
    from jailbee.lifecycle import short_name

    source = req.source_container or source_name
    provenance = f"container:{short_name(cfg, req.source_container)}" if req.source_container else "seed-container"
    alias = cfg.golden.alias
    staging = golden.golden_seeding_name(alias)
    created = False
    result = SeedOutcome("failed", provenance, detail="No source container")
    try:
        if source is None:
            raise ValueError("No source container")
        if incus.exists(staging):
            warn(f"Removing leftover {staging} from a previous seed")
            incus.delete(staging, force=True)
        globs = cfg.golden.docker_seed.images if cfg.golden.docker_seed else []
        refs = list_tagged_images(incus, source, globs, deadline=deadline)
        if not refs:
            result = SeedOutcome("skipped", provenance, detail="no images")
        else:
            placement = golden.resolve_golden_placement(cfg, incus)
            created = True
            if placement.instance_mode:
                incus.copy(golden.golden_instance_name(alias), staging, instance_only=True,
                           config={"security.nesting": "true"})
            else:
                info("Non-btrfs storage: each container gets a full copy of the Docker images")
                incus.init(alias, staging, storage_pool=placement.pool, config={"security.nesting": "true"})
            _remaining(deadline)
            incus.start(staging)
            _wait_docker(incus, staging, deadline)
            existing = incus.exec(staging, ["docker", "image", "ls", "-aq"], timeout=_remaining(deadline)).split()
            if existing:
                incus.exec(staging, ["docker", "image", "rm", "-f", *existing], timeout=_remaining(deadline))
            incus.exec_pipe(source, ["docker", "save", *refs], staging, ["docker", "load"], timeout=_remaining(deadline))
            incus.exec(staging, ["docker", "image", "inspect", *refs], timeout=_remaining(deadline))
            size = _image_size(incus.exec(staging, ["docker", "system", "df", "--format", "{{json .}}"], timeout=_remaining(deadline)))
            incus.exec(staging, ["systemctl", "stop", "docker.socket", "docker", "containerd"], timeout=_remaining(deadline))
            stop_container(incus, staging, force_fallback=True, budget=min(30, _remaining(deadline)))
            _remaining(deadline)
            if placement.instance_mode:
                golden.promote_instance(incus, staging, alias)
                created = False
            else:
                golden._strip_local_nics(incus, staging)
                incus.config_unset(staging, "security.nesting")
                golden.promote_image(incus, staging, alias)
            result = SeedOutcome("seeded", provenance, len(refs), size)
    except (IncusError, TimeoutError, ValueError) as exc:
        result = SeedOutcome("failed", provenance, detail=str(exc))
    finally:
        if created:
            try:
                incus.delete(staging, force=True)
            except IncusError as exc:
                warn(f"Could not delete seed staging instance {staging}: {exc}")
    record_outcome(cfg, result)
    return result
