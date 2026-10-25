"""The create pipeline behind `jailbee new` / `jailbee fork`, outside the CLI.

`cli.new_cmd` used to hold every step inline. The steps that need no
terminal of their own live here, so a caller other than the CLI can run
them: each takes `Config` + `Incus` (+ `GlobalConfig`) explicitly, reports a
refusal by raising `CreateError` instead of exiting, and asks a human only
through an `Interaction` it is handed.

Contents, in pipeline order:

- errors — `CreateError`, `NeedsOperator`
- `Interaction` — what the caller can ask a human, if anything
- `HostExtras` / `host_extras` — registry mirror and LiteLLM settings
- `ensure_profiles` / `register_and_preflight` / `prepare_host` — the
  implicit first apply, egress registration and cache-pool preflight
- `prepare_fork_source` — `jailbee fork`'s source commit, pinned
- `preflight_background` — the asking a detached worker cannot do
- `CreateStarted` / `launch_background` — the detached `_new-worker`
- `start_create` — all of the above for a background create, in one call
  (the entry point for callers other than the CLI; `cli.new_cmd` calls the
  pieces itself, in its own order)

The CLI maps a `CreateError` to `error(str(e)); raise typer.Exit(e.exit_code)`.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee.docker_daemon import mirror_wanted
from jailbee.tui import info

if TYPE_CHECKING:
    from jailbee.branch_config import EscalationVerdict
    from jailbee.config import Config
    from jailbee.forking import ForkSource
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus
    from jailbee.lifecycle import NewContainerOptions

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CreateError(Exception):
    """A create step refused. `message` is user-facing; `exit_code` is 1 or 2."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code

    def __str__(self) -> str:
        return self.message


class NeedsOperator(CreateError):  # noqa: N818 -- a request for a human, named by the plan interface
    """A decision that needs a human, and the `Interaction` cannot ask.

    `reason` is a short machine-readable tag for what was to be asked.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message, 2)
        self.reason = reason


# ---------------------------------------------------------------------------
# Interaction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Interaction:
    """What the caller can ask a human while creating a container.

    `confirm=None` means nobody can be asked: a step that would prompt
    reports and changes nothing, or raises `NeedsOperator`.
    `autofetch=None` defers to `cfg.new.autofetch`.
    """

    confirm: Callable[[str], bool] | None
    accept_escalation: Callable[[EscalationVerdict], bool] | None = None
    autofetch: bool | None = None

    @classmethod
    def terminal(cls) -> Interaction:
        """The foreground CLI: confirm on a terminal, never without one."""
        from jailbee import prompting, tui

        return cls(confirm=tui.default_confirm if prompting.is_interactive() else None)

    @classmethod
    def none(cls) -> Interaction:
        """No human at all: never asks, never fetches on its own."""
        return cls(confirm=None, autofetch=False)


# ---------------------------------------------------------------------------
# Host extras: registry mirror and LiteLLM
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostExtras:
    """Host-side settings a new container is created with.

    `warnings` holds every message the CLI used to print with `warn` while
    resolving these; the caller decides how to surface them.
    """

    mirror_endpoint: tuple[str, int] | None
    mirror_ca_path: Path | None
    litellm_payload: dict[str, object] | None
    warnings: tuple[str, ...]


def host_extras(
    cfg: Config,
    incus: Incus,
    gcfg: GlobalConfig,
    *,
    network: str | None = None,
) -> HostExtras:
    """Resolve the registry mirror and LiteLLM settings for a new container.

    `network` is the new container's network mode (default
    `cfg.defaults.network`): an unusable mirror is fatal in strict mode
    (`CreateError`, exit 1) and a warning in loose mode.
    """
    from jailbee.incus import IncusError

    net_mode = network or cfg.defaults.network
    warnings: list[str] = []

    mirror_endpoint: tuple[str, int] | None = None
    mirror_ca_path: Path | None = None
    if mirror_wanted(cfg, gcfg):
        from jailbee.docker_daemon import compute_mirror_endpoint

        problem: str | None = None
        try:
            mirror_endpoint = compute_mirror_endpoint(incus, gcfg)
        except ValueError as e:
            problem = str(e)
        else:
            ca = gcfg.docker_registry_mirror.data_dir / "ca" / "ca.crt"
            if ca.is_file():
                mirror_ca_path = ca
            else:
                problem = (
                    f"Mirror CA cert not found at {ca}. "
                    f"Run 'jailbee registry up' and wait for the proxy to start."
                )
        if problem is not None:
            # Half a mirror is not a mirror: without the CA the container
            # cannot trust the proxy, so drop the endpoint too and treat both
            # failures identically.
            mirror_endpoint = None
            if net_mode == "strict":
                raise CreateError(problem, 1)
            warnings.append(
                f"{problem} Continuing without it — in loose mode the mirror is "
                f"only a pull cache. Enable it later with "
                f"'jailbee registry up && jailbee apply'."
            )

    from jailbee import litellm

    litellm_payload: dict[str, object] | None
    try:
        litellm_payload = litellm.container_sync_payload(incus, gcfg, view=cfg.litellm_view())
    except IncusError as e:
        warnings.append(f"Could not resolve LiteLLM settings: {e}; run `jailbee apply` to retry.")
        litellm_payload = None
    unserved = litellm.unserved_warning(litellm_payload)
    if unserved is not None:
        warnings.append(unserved)

    return HostExtras(
        mirror_endpoint=mirror_endpoint,
        mirror_ca_path=mirror_ca_path,
        litellm_payload=litellm_payload,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Host preparation
# ---------------------------------------------------------------------------


def _record_apply(cfg: Config) -> None:
    """Record an `apply` upgrade watermark for this repo.

    The same bookkeeping `cli._record_upgrade_action(cfg, "apply")` does, for
    callers that are not the CLI. A courtesy: a state-DB problem must never
    fail a create, which `upgrade.record_action` guarantees.
    """
    from datetime import UTC, datetime

    from jailbee import __version__
    from jailbee.upgrade import record_action

    record_action(cfg, "apply", __version__, now=datetime.now(UTC))


def ensure_profiles(
    cfg: Config,
    incus: Incus,
    gcfg: GlobalConfig,
    *,
    record_apply: Callable[[Config], None] | None = None,
) -> bool:
    """Create a scratch directory's profile set on its first container.

    Returns True when the implicit apply ran. `record_apply` records the
    `apply` upgrade watermark afterwards (default: `_record_apply`).
    """
    # A scratch directory has run neither `jb init` nor `jb apply`, so its
    # profile set does not exist yet and `new_container`'s `profile_assign`
    # would fail. `run_apply` and not `init_command`: the latter refuses when
    # profiles already exist, and a second definition of what a profile set
    # contains is exactly what must not exist. Unattended and unprompted —
    # unlike the image build this is seconds and needs no network.
    if not cfg.is_synthetic():
        return False
    from jailbee.apply import run_apply
    from jailbee.profiles import profile_names

    names = profile_names(cfg)
    wanted = (names.base, names.binds, names.net_strict, names.net_loose)
    if not any(not incus.profile_exists(p) for p in wanted):
        return False
    info(f"→ First container in {cfg.repo_root}: creating its profiles...")
    # no_restart: this repo has no containers yet, so there is nothing
    # a restart could apply to. `run_apply` re-registers the repo and
    # refreshes the egress pool itself (apply.py:165-173); the
    # `register_repo`/`refresh_pool` call further below runs again
    # regardless — both are idempotent, and skipping it here would
    # change behaviour for configured repos, which this block must not.
    run_apply(cfg, incus, gcfg, assume_yes=True, no_restart=True)
    # Same reasoning as `init`'s call: this writes what `apply`
    # writes, so it satisfies an `apply` upgrade note just as `apply`
    # does. Without it the directory is told, for the rest of its
    # life, to run the `apply` that just ran.
    (record_apply or _record_apply)(cfg)
    return True


def preflight_cache_pools(cfg: Config, *, confirm: Callable[[str], bool] | None) -> None:
    """Bring every cache pool root into a usable state, or raise exit 2.

    Runs before anything is created or booted; see
    `cli._preflight_cache_pools` for why. `confirm=None` reports and changes
    nothing.
    """
    from jailbee.pool import preflight_pools

    unresolved = preflight_pools(cfg, confirm=confirm)
    if not unresolved:
        return
    raise CreateError(
        f"Cache pool {', '.join(unresolved)} cannot be used until its root holds "
        "only the pool layout. Re-run in a terminal to be offered a move, or "
        "move the loose entries out of the pool root yourself. Nothing was created.",
        2,
    )


def register_and_preflight(
    cfg: Config,
    incus: Incus,
    gcfg: GlobalConfig,
    *,
    interaction: Interaction,
    on_warning: Callable[[str], None] = log.warning,
) -> None:
    """Register the repo, resolve its egress pool, preflight the cache pools.

    `CreateError` exit 1 when the initial resolution fails outright, exit 2
    when a cache pool stays unusable. A partial resolution is only a warning,
    handed to `on_warning` as it happens (the CLI passes `tui.warn`; the
    default logs it) — before the pool preflight, which may still refuse.
    """
    # Register this repo with the refresh timer and resolve the pool
    # *before* creating the container so that the new container's
    # first /etc/hosts pin (done inside new_container) sees fresh IPs.
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import egress_pool
    from jailbee.db import get_engine

    with Session(get_engine()) as session:
        egress_pool.register_repo(session, cfg)
        refresh_result = egress_pool.refresh_pool(
            cfg,
            gcfg,
            incus,
            session,
            now=datetime.now(UTC),
        )
    if refresh_result.status == "dns_error":
        raise CreateError(f"Initial egress pool resolution failed: {refresh_result.error}", 1)
    if refresh_result.status == "partial":
        on_warning(f"Some egress hostnames failed to resolve: {refresh_result.error}")

    # Before either path creates anything: a polluted cache pool root would
    # otherwise stop `new_container` half-way, with the container already
    # created and its GUI sockets and port forwards attached.
    preflight_cache_pools(cfg, confirm=interaction.confirm)


def prepare_host(
    cfg: Config,
    incus: Incus,
    gcfg: GlobalConfig,
    *,
    interaction: Interaction,
    record_apply: Callable[[Config], None] | None = None,
    on_warning: Callable[[str], None] = log.warning,
) -> None:
    """Everything the host needs before a container can be created.

    `ensure_profiles` then `register_and_preflight`. The CLI calls the two
    separately, to print its upgrade advice between them.
    """
    ensure_profiles(cfg, incus, gcfg, record_apply=record_apply)
    register_and_preflight(cfg, incus, gcfg, interaction=interaction, on_warning=on_warning)


# ---------------------------------------------------------------------------
# Fork source
# ---------------------------------------------------------------------------


def prepare_fork_source(cfg: Config, incus: Incus, source: str, new_full_name: str) -> ForkSource:
    """Fetch `source`'s HEAD to the host and pin it under `new_full_name`.

    `jailbee fork`'s first step. `CreateError` exit 2 for a `ForkError` or an
    existing `new_full_name`, exit 1 for a `GitError`.
    """
    from jailbee import forking
    from jailbee import git as git_helpers
    from jailbee.lifecycle import short_name

    try:
        fork_source = forking.prepare_fork(cfg, incus, source)
    except forking.ForkError as e:
        # `sync`'s detached-HEAD message suggests `--branch`, which on
        # `jailbee fork` names the *new* branch, not the source's.
        raise CreateError(str(e).removesuffix(" Use --branch <name> to specify."), 2) from e
    except git_helpers.GitError as e:
        raise CreateError(str(e), 1) from e
    # Pin the commit under the fork's own name before anything else can
    # go wrong: the source's ref dies with the source, and the fork's
    # `--shared` clone needs the objects for as long as the fork lives.
    # Checked free first, or an existing fork's pin would be moved.
    if incus.exists(new_full_name):
        raise CreateError(f"Container '{short_name(cfg, new_full_name)}' already exists", 2)
    try:
        forking.pin_fork_commit(cfg, new_full_name, fork_source.commit)
    except git_helpers.GitError as e:
        raise CreateError(str(e), 1) from e
    return fork_source


# ---------------------------------------------------------------------------
# Background preflight
# ---------------------------------------------------------------------------


def preflight_background(
    cfg: Config,
    opts: NewContainerOptions,
    *,
    interaction: Interaction,
) -> NewContainerOptions:
    """Do the asking a detached worker cannot, before there is one.

    A background `jailbee new` hands provisioning to a worker with no stdin. Any
    confirmation left for that worker can only ever be answered "no", and the
    user finds out from a log file after the job has already failed — which is
    how a checkout lagging `origin/<default_branch>` used to break `jailbee new`
    outright. So the host-side work that can raise a question happens here, in
    the terminal the user is still sitting at: resolve the clone ref (fetching
    if configured), read the target branch's autostart, report both
    comparisons, and ask about a genuine privilege widening.

    Who answers is `interaction`'s: `accept_escalation` when set (a policy;
    a refusal raises `CreateError` with the escalation's reasons), else
    `confirm`, else nobody (`NeedsOperator`). `interaction.autofetch`
    overrides `cfg.new.autofetch` when not None.

    Returns `opts` carrying what was settled: the accepted ref (so the worker
    does not ask again — nor treat the answer as covering a commit that landed
    in the meantime) and `autofetch_done`, so the fetch is not repeated.
    Raises `CreateError` exit 2 on decline, having created nothing and
    recorded no job.
    """
    from jailbee.branch_config import format_escalation
    from jailbee.lifecycle import (
        assess_branch_autostart,
        resolve_clone_ref,
    )

    autofetch = cfg.new.autofetch if interaction.autofetch is None else interaction.autofetch
    try:
        clone_ref = resolve_clone_ref(cfg, opts, autofetch=autofetch)
        assessment = assess_branch_autostart(cfg, opts, clone_ref)
    except ValueError as e:
        # Same failures `new_container` raises for a bad ref or a failed
        # fetch — better here, in the terminal, than in a worker log.
        raise CreateError(str(e), 2) from e

    verdict = assessment.verdict
    approved_ref: str | None = None
    if verdict is not None and verdict.prompts:
        from jailbee.remote_ssh.session import escalation_refusal, is_remote_session

        if is_remote_session():
            raise CreateError(escalation_refusal(verdict.baseline_source), 2)
    if verdict is not None and verdict.prompts and not opts.assume_yes:
        if interaction.accept_escalation is not None:
            if not interaction.accept_escalation(verdict):
                raise CreateError(format_escalation(verdict), 2)
        elif interaction.confirm is None:
            raise NeedsOperator(
                "The target branch's autostart config widens privileges beyond "
                f"{verdict.baseline_source}, which needs confirmation — and there is "
                "no terminal to ask on. Re-run with --yes to accept it, or edit the "
                "branch's .jailbee/config.yaml.",
                reason="escalation",
            )
        elif not interaction.confirm("Provision with the branch's widened privileges?"):
            raise CreateError(
                "Aborted: declined the target branch's autostart config. Nothing was created.",
                2,
            )
        approved_ref = assessment.ref

    return replace(opts, approved_autostart_ref=approved_ref, autofetch_done=True)


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreateStarted:
    """A background create that has been handed to its detached worker."""

    container_name: str
    pid: int
    log_path: Path


def launch_background(
    cfg: Config,
    opts: NewContainerOptions,
    *,
    container_name: str,
    config_path: Path | None,
) -> CreateStarted:
    """Write the job file, spawn the detached `_new-worker`, record the job row.

    `config_path` is passed to the worker as `--config` when not None.
    """
    from datetime import datetime as _dt

    from jailbee import background as bg
    from jailbee.db import state_dir

    log_dir = state_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"{container_name}-{stamp}.log"

    job = bg.op_to_job(opts, container_name=container_name, log_path=str(log_path))
    job_file = log_dir / f"{container_name}-{stamp}.job.json"
    job_file.write_text(json.dumps(job))

    # `--config` only when there is a file: a scratch directory has none,
    # and the worker re-synthesizes the same config from the `cwd` below.
    # Same shape as `jailbee.dashboard.model.RepoTarget.flags()`.
    worker_argv = [
        sys.executable,
        "-m",
        "jailbee",
        "_new-worker",
        "--job",
        str(job_file),
        *(["--config", str(config_path)] if config_path is not None else []),
    ]
    # Deliberately not closed here: the handle is inherited by the
    # detached child as its stdout/stderr; the parent returns immediately
    # and the fd is released on exit.
    log_fh = open(log_path, "w")
    proc = subprocess.Popen(
        worker_argv,
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(cfg.repo_root),
    )

    # Guarded like the worker's own writes, and for the same reason with
    # one twist: the worker is already running by now, so a failed insert
    # must cost the user only `jailbee ls`'s JOB column — never the
    # container that is being built behind it.
    bg.track_job(
        bg.job_engine(),
        lambda s: bg.start_job(
            s,
            container_name=container_name,
            container_prefix=cfg.container_prefix,
            branch=None if opts.mount else opts.container_branch,
            pid=proc.pid,
            log_path=str(log_path),
            now=_dt.now().astimezone(),
        ),
        "record the new job row",
    )
    return CreateStarted(container_name=container_name, pid=proc.pid, log_path=log_path)


# ---------------------------------------------------------------------------
# All in one
# ---------------------------------------------------------------------------


def start_create(
    cfg: Config,
    incus: Incus,
    gcfg: GlobalConfig,
    opts: NewContainerOptions,
    *,
    interaction: Interaction,
    config_path: Path | None,
) -> CreateStarted:
    """Start a background create without a CLI: host prep through launch.

    `host_extras` (its warnings logged) fills the mirror and LiteLLM fields
    of `opts`; `prepare_host`; the name is checked free (`CreateError`
    exit 2); `preflight_background`; `launch_background`. An unusable
    `opts.initial_prompt` is refused first (exit 2). Every refusal is a
    `CreateError`, raised before anything is created.
    """
    from jailbee.lifecycle import derive_container_name, short_name

    if opts.initial_prompt is not None:
        from jailbee import initial_prompt

        # Before any host work: a prompt the primary agent cannot take would be
        # written and left `pending`, reopening its window on every start.
        try:
            initial_prompt.validate_prompt_text(opts.initial_prompt)
        except initial_prompt.PromptError as e:
            raise CreateError(str(e), 2) from e
        problem = initial_prompt.prompt_problem(cfg, opts.runner)
        if problem is not None:
            raise CreateError(problem, 2)

    extras = host_extras(cfg, incus, gcfg, network=opts.network)
    for message in extras.warnings:
        log.warning(message)
    opts = replace(
        opts,
        mirror_endpoint=extras.mirror_endpoint,
        mirror_ca_path=extras.mirror_ca_path,
        litellm_payload=extras.litellm_payload,
    )
    prepare_host(cfg, incus, gcfg, interaction=interaction)

    container_name = opts.name or derive_container_name(cfg, opts.container_branch)
    if incus.exists(container_name):
        raise CreateError(f"Container '{short_name(cfg, container_name)}' already exists", 2)

    opts = preflight_background(cfg, opts, interaction=interaction)
    return launch_background(cfg, opts, container_name=container_name, config_path=config_path)
