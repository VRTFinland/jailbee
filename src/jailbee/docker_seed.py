"""Copy tagged Docker images into a disposable golden staging instance."""

from __future__ import annotations

import fnmatch
import json
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from jailbee import golden
from jailbee.golden_sharing import shared_golden_reason_for
from jailbee.incus import IncusError
from jailbee.stopping import stop_container
from jailbee.tui import info, warn

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.global_config import GlobalConfig
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
    try:
        reason = shared_golden_reason_for(cfg)
    except Exception as exc:
        raise SeedPreflightError(f"Cannot check golden ownership: {exc}") from exc
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
    output = incus.exec(
        name, ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"], timeout=timeout
    )
    refs = []
    for line in output.splitlines():
        ref = line.strip()
        if (
            not ref
            or "<none>" in ref
            or (globs and not any(fnmatch.fnmatchcase(ref, g) for g in globs))
        ):
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

    try:
        with Session(get_engine()) as session:
            docker_seed_state.record(
                session,
                alias=cfg.golden.alias,
                container_prefix=cfg.container_prefix,
                seeded_at=datetime.now(UTC),
                source=outcome.source,
                outcome=outcome.outcome,
                image_count=outcome.image_count,
                size_bytes=outcome.size_bytes,
                detail=outcome.detail,
            )
            session.commit()
    except Exception as exc:
        warn(f"Could not record Docker seed outcome: {exc}")


def seed_golden(
    cfg: Config,
    incus: Incus,
    req: SeedRequest,
    *,
    deadline: float,
    source_name: str | None = None,
) -> SeedOutcome:
    """Seed from req's full container name, or task 8's ephemeral source_name.

    source_name supplies the full ephemeral container name without changing req,
    so persisted provenance remains seed-container rather than container:<short>.
    deadline is an absolute time.monotonic() deadline.
    """
    from jailbee.lifecycle import short_name

    source = req.source_container or source_name
    provenance = (
        f"container:{short_name(cfg, req.source_container)}"
        if req.source_container
        else "seed-container"
    )
    alias = cfg.golden.alias
    staging = golden.golden_seeding_name(alias)
    cleanup_incus = incus
    incus = incus.with_deadline(deadline)
    created = False
    result = SeedOutcome("failed", provenance, detail="No source container")
    try:
        if source is None:
            raise ValueError("No source container")
        golden.recover_seed_leftovers(cfg, incus)
        globs = cfg.golden.docker_seed.images if cfg.golden.docker_seed else []
        refs = list_tagged_images(incus, source, globs, deadline=deadline)
        if not refs:
            result = SeedOutcome("skipped", provenance, detail="no images")
        else:
            placement = golden.resolve_golden_placement(cfg, incus)
            created = True
            if placement.instance_mode:
                incus.copy(
                    golden.golden_instance_name(alias),
                    staging,
                    instance_only=True,
                    config={"security.nesting": "true"},
                )
            else:
                info("Non-btrfs storage: each container gets a full copy of the Docker images")
                incus.init(
                    alias, staging, storage_pool=placement.pool, config={"security.nesting": "true"}
                )
            _remaining(deadline)
            incus.start(staging)
            _wait_docker(incus, staging, deadline)
            existing = incus.exec(
                staging, ["docker", "image", "ls", "-aq"], timeout=_remaining(deadline)
            ).split()
            if existing:
                incus.exec(
                    staging,
                    ["docker", "image", "rm", "-f", *existing],
                    timeout=_remaining(deadline),
                )
            incus.exec_pipe(
                source,
                ["docker", "save", *refs],
                staging,
                ["docker", "load"],
                timeout=_remaining(deadline),
            )
            incus.exec(staging, ["docker", "image", "inspect", *refs], timeout=_remaining(deadline))
            size = _image_size(
                incus.exec(
                    staging,
                    ["docker", "system", "df", "--format", "{{json .}}"],
                    timeout=_remaining(deadline),
                )
            )
            incus.exec(
                staging,
                ["systemctl", "stop", "docker.socket", "docker", "containerd"],
                timeout=_remaining(deadline),
            )
            stop_container(
                incus, staging, force_fallback=True, budget=min(30, _remaining(deadline))
            )
            _remaining(deadline)
            if placement.instance_mode:
                golden.promote_instance(incus, staging, alias, cleanup_incus=cleanup_incus)
                created = False
            else:
                golden._strip_local_nics(incus, staging)
                incus.config_unset(staging, "security.nesting")
                golden.promote_image(incus, staging, alias, cleanup_incus=cleanup_incus)
            result = SeedOutcome("seeded", provenance, len(refs), size)
    except (IncusError, TimeoutError, ValueError) as exc:
        result = SeedOutcome("failed", provenance, detail=str(exc))
    finally:
        if created:
            try:
                cleanup_incus.delete(staging, force=True)
            except IncusError as exc:
                warn(f"Could not delete seed staging instance {staging}: {exc}")
    record_outcome(cfg, result)
    return result


def create_seed_container(cfg: Config, incus: Incus, gcfg: GlobalConfig) -> str:
    from jailbee import create_flow, git, lifecycle

    sha = git.rev_parse_remote(cfg.repo_root, cfg.upstream_remote, cfg.default_branch)
    sha = sha or git.rev_parse(cfg.repo_root, cfg.default_branch)
    if not sha:
        raise ValueError(f"Cannot resolve default branch {cfg.default_branch}")
    extras = create_flow.host_extras(cfg, incus, gcfg, network=cfg.defaults.network)
    for message in extras.warnings:
        warn(message)
    create_flow.prepare_host(
        cfg, incus, gcfg, interaction=create_flow.Interaction.none(), on_warning=warn
    )
    opts = lifecycle.NewContainerOptions(
        container_branch="docker-seed",
        name=None,
        network=cfg.defaults.network,
        memory=None,
        cpu=cfg.defaults.cpu,
        from_base=cfg.golden.alias,
        clone=True,
        clone_commit=sha,
        autostart_override="wait",
        skip_agents=True,
        autostart_services=[],
        autostart_profile=cfg.golden.docker_seed.autostart_profile
        if cfg.golden.docker_seed
        else None,
        mirror_endpoint=extras.mirror_endpoint,
        mirror_ca_path=extras.mirror_ca_path,
        litellm_payload=extras.litellm_payload,
        storage_pool=cfg.defaults.storage_pool,
    )
    name = f"{cfg.container_prefix}-docker-seed"

    def before_autostart(phase: str) -> None:
        if phase != "autostart":
            return
        # Reseeding starts from the previous golden, but only this run's
        # autostart output belongs in the new image set.
        for _attempt in range(30):
            try:
                incus.exec(name, ["docker", "info"], timeout=5)
                break
            except IncusError:
                if _attempt == 29:
                    raise
                time.sleep(1)
        inherited = incus.exec(name, ["docker", "image", "ls", "-aq"]).split()
        if inherited:
            incus.exec(name, ["docker", "image", "rm", "-f", *dict.fromkeys(inherited)])

    return lifecycle.new_container(
        cfg, incus, opts, confirm_fn=lambda _message: False, on_phase=before_autostart
    )


def run_seed(cfg: Config, incus: Incus, gcfg: GlobalConfig, req: SeedRequest) -> SeedOutcome:
    from jailbee import create_flow, lifecycle

    timeout = cfg.golden.docker_seed.timeout if cfg.golden.docker_seed else 60
    deadline = time.monotonic() + timeout * 60
    if req.source_container:
        return seed_golden(cfg, incus, req, deadline=deadline)

    name = f"{cfg.container_prefix}-docker-seed"
    owned = False
    try:
        if incus.exists(name):
            raise ValueError(f"The docker-seed container {name} already exists")
        owned = True

        def stop_on_timeout() -> None:
            try:
                # The work deadline has expired; give the watchdog its own
                # short budget so joining it cannot hang on an unresponsive daemon.
                watchdog = incus.with_deadline(time.monotonic() + 30)
                if watchdog.exists(name):
                    watchdog.stop(name, force=True)
            except IncusError as exc:
                warn(f"Could not stop timed-out seed container {name}: {exc}")

        timer = threading.Timer(max(0, deadline - time.monotonic()), stop_on_timeout)
        timer.start()
        try:
            name = create_seed_container(cfg, incus.with_deadline(deadline), gcfg)
        finally:
            timer.cancel()
            timer.join()
        _remaining(deadline)
        result = seed_golden(cfg, incus, req, deadline=deadline, source_name=name)
        _remaining(deadline)
        return result
    except (IncusError, RuntimeError, ValueError, TimeoutError, create_flow.CreateError) as exc:
        detail = str(exc)
        if time.monotonic() >= deadline:
            detail = "Docker seeding timed out"
        result = SeedOutcome("failed", "seed-container", detail=detail)
        record_outcome(cfg, result)
        return result
    finally:
        if owned:
            try:
                if incus.exists(name):
                    if req.keep_seed:
                        info(f"Keeping seed container {name}")
                    else:
                        lifecycle.destroy_container(cfg, incus, name, force=True)
            except (IncusError, RuntimeError, ValueError) as exc:
                warn(f"Could not destroy seed container {name}: {exc}")
