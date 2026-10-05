"""`jailbee upgrade`: bring every registered repo up to date after an upgrade.

Runs `base build` and `apply` per repo, never restarting a container. What a
repo is owed comes from `upgrade.pending` (the `UPGRADE_NOTES` manifest and the
repo's watermarks); `force` ignores that and runs both everywhere, which is the
safe choice when a release forgot its manifest entry.

Failure is per repo: a repo that cannot load, build or apply is reported in the
summary and the sweep carries on. A failed build blocks that repo's `apply`,
because an apply against the old image would claim an upgrade that did not
happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus

BuildStatus = Literal["skipped", "planned", "built", "shared", "failed"]
ApplyStatus = Literal["skipped", "planned", "applied", "failed", "blocked"]


@dataclass
class RepoPlan:
    """What one repo is owed. `cfg` is None when its config did not load."""

    root: Path
    cfg: Config | None = None
    build: bool = False
    apply: bool = False
    reasons: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class RepoResult:
    root: Path
    prefix: str | None
    build: BuildStatus = "skipped"
    apply: ApplyStatus = "skipped"
    reasons: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.error is None
            and self.build != "failed"
            and self.apply not in {"failed", "blocked"}
        )


def plan_repos(roots: list[Path], *, force: bool, version: str, now: datetime) -> list[RepoPlan]:
    """Decide, per repo, which of `base build` and `apply` to run.

    Dismissals are deliberately not consulted: `jb dismiss` silences a hint,
    while this command is the user asking for the upgrade outright.
    """
    from sqlmodel import Session

    from jailbee.config import ConfigError, load_repo_config
    from jailbee.db import get_engine
    from jailbee.upgrade import load_or_backfill, pending

    plans: list[RepoPlan] = []
    for root in roots:
        try:
            cfg = load_repo_config(root)
        except ConfigError as e:
            plans.append(RepoPlan(root=root, error=str(e)))
            continue
        plan = RepoPlan(root=root, cfg=cfg)
        if force:
            plan.build = plan.apply = True
            plan.reasons = ["forced"]
        else:
            with Session(get_engine()) as session:
                marks = load_or_backfill(session, cfg.container_prefix, version, now=now)
            owed = pending(version, marks)
            for item in owed.actions:
                if item.action == "base_build":
                    plan.build = True
                else:
                    plan.apply = True
                for reason in item.reasons:
                    if reason not in plan.reasons:
                        plan.reasons.append(reason)
        plans.append(plan)
    return plans


def _record(
    cfg: Config, action: Literal["base_build", "apply"], version: str, now: datetime
) -> None:
    from sqlmodel import Session

    from jailbee.db import get_engine
    from jailbee.upgrade import record

    with Session(get_engine()) as session:
        record(session, cfg.container_prefix, action, version, now=now)


def run_upgrade_all(
    roots: list[Path],
    incus: Incus,
    gcfg: GlobalConfig,
    *,
    force: bool,
    dry_run: bool,
    version: str,
    now: datetime,
) -> list[RepoResult]:
    """Upgrade every repo in `roots`; return one result per repo, in order."""
    from jailbee.apply import run_apply
    from jailbee.golden import build_golden_image
    from jailbee.tui import error_plain, info_plain, success

    plans = plan_repos(roots, force=force, version=version, now=now)
    # alias -> whether this run's build of it worked. Repos that share an
    # alias (every scratch repo does) share one image, so it is built once.
    built: dict[str, bool] = {}
    results: list[RepoResult] = []

    for plan in plans:
        cfg = plan.cfg
        result = RepoResult(
            root=plan.root,
            prefix=cfg.container_prefix if cfg else None,
            reasons=plan.reasons,
            error=plan.error,
        )
        results.append(result)
        info_plain(f"== {plan.root} ==")
        if cfg is None:
            error_plain(f"Config did not load: {plan.error}")
            continue

        if dry_run:
            result.build = "planned" if plan.build else "skipped"
            result.apply = "planned" if plan.apply else "skipped"
            for reason in plan.reasons:
                info_plain(f"  - {reason}")
            continue

        if plan.build:
            alias = cfg.golden.alias
            if alias in built:
                ok = built[alias]
                result.build = "shared" if ok else "failed"
                if not ok:
                    result.error = f"base image '{alias}' failed to build earlier in this run"
            else:
                try:
                    build_golden_image(cfg, incus)
                except Exception as e:  # reported in the summary; the sweep goes on
                    built[alias] = False
                    result.build = "failed"
                    result.error = str(e)
                else:
                    built[alias] = True
                    result.build = "built"
            if built[alias]:
                _record(cfg, "base_build", version, now)

        if plan.apply:
            if result.build == "failed":
                result.apply = "blocked"
                continue
            try:
                applied = run_apply(cfg, incus, gcfg, assume_yes=True, no_restart=True)
            except Exception as e:  # reported in the summary; the sweep goes on
                result.apply = "failed"
                result.error = str(e)
                continue
            # Same rule as `jailbee apply`: returning at all means the config
            # writing steps succeeded, so the watermark moves even when a port
            # forward failed.
            _record(cfg, "apply", version, now)
            result.apply = "applied"
            if not applied.fully_successful:
                result.error = "; ".join(f"{n}: {m}" for n, m in applied.port_failures)

    _print_summary(results, dry_run=dry_run)
    if all(r.ok for r in results):
        success("Dry run complete." if dry_run else "Upgrade complete; no container was restarted.")
    return results


def _print_summary(results: list[RepoResult], *, dry_run: bool) -> None:
    from jailbee.tui import info_plain

    info_plain("")
    info_plain("Summary" + (" (dry run)" if dry_run else ""))
    for r in results:
        line = f"  {r.prefix or r.root}: base build {r.build}, apply {r.apply}"
        if r.error:
            line += f" — {r.error}"
        info_plain(line)
