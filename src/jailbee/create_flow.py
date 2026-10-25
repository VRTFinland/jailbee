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

The CLI maps a `CreateError` to `error(str(e)); raise typer.Exit(e.exit_code)`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jailbee.docker_daemon import mirror_wanted
from jailbee.tui import info, warn

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.branch_config import EscalationVerdict
    from jailbee.config import Config
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus


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
) -> None:
    """Register the repo, resolve its egress pool, preflight the cache pools.

    `CreateError` exit 1 when the initial resolution fails outright, exit 2
    when a cache pool stays unusable.
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
        warn(f"Some egress hostnames failed to resolve: {refresh_result.error}")

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
) -> None:
    """Everything the host needs before a container can be created.

    `ensure_profiles` then `register_and_preflight`. The CLI calls the two
    separately, to print its upgrade advice between them.
    """
    ensure_profiles(cfg, incus, gcfg, record_apply=record_apply)
    register_and_preflight(cfg, incus, gcfg, interaction=interaction)
