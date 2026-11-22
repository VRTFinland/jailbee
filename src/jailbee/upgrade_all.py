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

import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus

BuildStatus = Literal["skipped", "planned", "built", "shared", "failed"]
ApplyStatus = Literal["skipped", "planned", "applied", "failed", "blocked"]
SeedStatus = Literal["-", "seeded", "skipped", "failed"]


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
    seed: SeedStatus = "-"
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


class _PrefixRouter:
    """A stand-in for a standard stream that prefixes what worker threads print.

    Output from a thread that called `claim(prefix)` is split into lines, each
    written whole as `[prefix] line`, so concurrent builds never share a line.
    Other threads write straight through. Claimed threads report not being a
    terminal, which keeps Rich from drawing spinners that would fight. A
    spinner's own refresh thread is not a claimed thread, so the whole router
    stops reporting a terminal for as long as `quiet_tty` is set.
    """

    def __init__(self, inner: Any, lock: threading.Lock) -> None:
        self._inner = inner
        self._lock = lock
        self._local = threading.local()
        self.quiet_tty = False

    def claim(self, prefix: str) -> None:
        self._local.prefix = prefix
        self._local.pending = ""

    def release(self) -> None:
        pending = getattr(self._local, "pending", "")
        if pending:
            self._emit(pending)
        self._local.prefix = None
        self._local.pending = ""

    def _emit(self, line: str) -> None:
        with self._lock:
            self._inner.write(f"[{self._local.prefix}] {line}\n")
            self._inner.flush()

    def write(self, text: str) -> int:
        if getattr(self._local, "prefix", None) is None:
            with self._lock:
                return int(self._inner.write(text))
        buffered = self._local.pending + text.replace("\r\n", "\n").replace("\r", "\n")
        *lines, self._local.pending = buffered.split("\n")
        for line in lines:
            if line.strip():
                self._emit(line)
        return len(text)

    def flush(self) -> None:
        if getattr(self._local, "prefix", None) is None:
            with self._lock:
                self._inner.flush()

    def isatty(self) -> bool:
        if self.quiet_tty or getattr(self._local, "prefix", None) is not None:
            return False
        return bool(self._inner.isatty())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _BuildInterrupted(KeyboardInterrupt):
    """Ctrl-C during the parallel builds; `finished` is what completed anyway."""

    def __init__(self, finished: dict[str, Exception | None]) -> None:
        super().__init__()
        self.finished = finished


@contextmanager
def _no_spinners() -> Iterator[None]:
    """Make Rich draw no live output on the shared consoles.

    Rich reads TTY_COMPATIBLE / FORCE_COLOR when a console is created, which
    beats `isatty()`, so the stream router alone cannot stop spinners. The
    consoles are module globals read at import, hence the private attribute.
    """
    from jailbee import tui

    consoles = [tui.console, tui.err_console, tui.hint_console]
    saved = [c._force_terminal for c in consoles]
    for c in consoles:
        c._force_terminal = False
    try:
        yield
    finally:
        for c, old in zip(consoles, saved, strict=True):
            c._force_terminal = old


def _build_in_parallel(
    jobs_by_alias: dict[str, Config], incus: Incus, jobs: int
) -> dict[str, Exception | None]:
    """Build each alias's image with up to `jobs` builds at once.

    Returns alias -> the exception its build raised, or None when it worked.
    Every build runs to its end; one failure never cancels the others. On
    Ctrl-C queued builds are cancelled, running ones finish, and
    `_BuildInterrupted` carries the results so far.
    """
    from jailbee.golden import build_golden_image
    from jailbee.init_command import ensure_loose_bridge

    # Once, here: concurrent builds would race to create it ("already exists").
    ensure_loose_bridge(incus, quiet=True)

    lock = threading.Lock()
    out = _PrefixRouter(sys.stdout, lock)
    err = _PrefixRouter(sys.stderr, lock)

    done: dict[str, Exception | None] = {}

    def one(alias: str, cfg: Config) -> Exception | None:
        error = build_one(alias, cfg)
        done[alias] = error
        return error

    def build_one(alias: str, cfg: Config) -> Exception | None:
        out.claim(alias)
        err.claim(alias)
        try:
            build_golden_image(cfg, incus)
        except Exception as e:  # reported in the summary; the others go on
            print(f"build failed: {e}")  # prefixed, so it lands beside its build's output
            return e
        finally:
            out.release()
            err.release()
        return None

    real_out, real_err = sys.stdout, sys.stderr
    out.quiet_tty = err.quiet_tty = True
    sys.stdout, sys.stderr = out, err
    pool = ThreadPoolExecutor(max_workers=jobs)
    try:
        with _no_spinners():
            futures = {a: pool.submit(one, a, c) for a, c in jobs_by_alias.items()}
            try:
                results = {a: f.result() for a, f in futures.items()}
            except KeyboardInterrupt as interrupt:
                try:
                    pool.shutdown(wait=True, cancel_futures=True)  # start no queued build
                except KeyboardInterrupt:
                    pool.shutdown(wait=False, cancel_futures=True)  # second Ctrl-C: stop waiting
                raise _BuildInterrupted(dict(done)) from interrupt
            pool.shutdown(wait=True)
            return results
    finally:
        sys.stdout, sys.stderr = real_out, real_err


def _record(
    cfg: Config, action: Literal["base_build", "apply"], version: str, now: datetime
) -> None:
    from sqlmodel import Session

    from jailbee.db import get_engine
    from jailbee.upgrade import record

    with Session(get_engine()) as session:
        record(session, cfg.container_prefix, action, version, now=now)


def _record_interrupted(
    plans: list[RepoPlan], finished: dict[str, Exception | None], version: str, now: datetime
) -> None:
    """Keep the builds that finished before Ctrl-C, so the next run skips them."""
    from jailbee.tui import warn

    results: list[RepoResult] = []
    for plan in plans:
        cfg = plan.cfg
        if cfg is None or not plan.build or cfg.golden.alias not in finished:
            continue
        error = finished[cfg.golden.alias]
        result = RepoResult(root=plan.root, prefix=cfg.container_prefix, reasons=plan.reasons)
        if error is None:
            result.build = "built"
            _record(cfg, "base_build", version, now)
        else:
            result.build = "failed"
            result.error = str(error)
        results.append(result)
    warn("Interrupted: builds that finished are recorded; the rest were not started or recorded.")
    _print_summary(results, dry_run=False)


def run_upgrade_all(
    roots: list[Path],
    incus: Incus,
    gcfg: GlobalConfig,
    *,
    force: bool,
    dry_run: bool,
    version: str,
    now: datetime,
    docker_seed: bool = True,
    jobs: int = 1,
) -> list[RepoResult]:
    """Upgrade every repo in `roots`; return one result per repo, in order.

    With `jobs` > 1 the distinct base images are built up front, up to `jobs`
    at a time, each build's output prefixed with its alias; `apply` and seeding
    then run serially per repo as before. `jobs` = 1 builds and applies repo by
    repo, exactly as it always did.
    """
    from jailbee.apply import run_apply
    from jailbee.golden import build_golden_image
    from jailbee.tui import error_plain, info_plain, success

    plans = plan_repos(roots, force=force, version=version, now=now)
    # alias -> whether this run's build of it worked. Repos that share an
    # alias (every scratch repo does) share one image, so it is built once.
    built: dict[str, bool] = {}
    # Parallel mode: alias -> (repo whose build it was, the error or None).
    prebuilt: dict[str, tuple[Path, Exception | None]] = {}
    if jobs > 1 and not dry_run:
        wanted: dict[str, tuple[Path, Config]] = {}
        for plan in plans:
            if plan.cfg is not None and plan.build:
                wanted.setdefault(plan.cfg.golden.alias, (plan.root, plan.cfg))
        if wanted:
            info_plain(f"== building {len(wanted)} base image(s), up to {jobs} at a time ==")
            try:
                errors = _build_in_parallel({a: c for a, (_, c) in wanted.items()}, incus, jobs)
            except _BuildInterrupted as interrupted:
                _record_interrupted(plans, interrupted.finished, version, now)
                raise
            prebuilt = {a: (wanted[a][0], errors[a]) for a in wanted}
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
            if alias in prebuilt:
                owner, build_error = prebuilt[alias]
                built[alias] = build_error is None
                if build_error is not None:
                    result.build = "failed"
                    result.error = (
                        str(build_error)
                        if owner == plan.root
                        else f"base image '{alias}' failed to build earlier in this run"
                    )
                else:
                    result.build = "built" if owner == plan.root else "shared"
            elif alias in built:
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

        seed_cfg = cfg.golden.docker_seed
        if (
            docker_seed
            and seed_cfg is not None
            and seed_cfg.enabled
            and result.build == "built"
            and result.apply not in {"failed", "blocked"}
        ):
            from jailbee.docker_seed import (
                SeedOutcome,
                SeedPreflightError,
                SeedRequest,
                preflight,
                record_outcome,
                run_seed,
            )
            from jailbee.tui import warn

            request = SeedRequest(None, False, False)
            try:
                try:
                    preflight(cfg, incus, request)
                except SeedPreflightError as e:
                    result.seed = "skipped"
                    warn(f"Docker seeding skipped for {cfg.container_prefix}: {e}")
                    record_outcome(cfg, SeedOutcome("skipped", "seed-container", detail=str(e)))
                else:
                    outcome = run_seed(cfg, incus, gcfg, request)
                    if outcome.outcome == "seeded":
                        result.seed = "seeded"
                    elif outcome.outcome == "skipped":
                        result.seed = "skipped"
                    elif outcome.outcome == "failed":
                        result.seed = "failed"
                    if result.seed == "failed":
                        warn(f"Docker seeding failed for {cfg.container_prefix}: {outcome.detail}")
            except Exception as e:
                result.seed = "failed"
                warn(f"Docker seeding failed for {cfg.container_prefix}: {e}")
                record_outcome(cfg, SeedOutcome("failed", "seed-container", detail=str(e)))

    _print_summary(results, dry_run=dry_run)
    if all(r.ok for r in results):
        success("Dry run complete." if dry_run else "Upgrade complete; no container was restarted.")
    return results


def _print_summary(results: list[RepoResult], *, dry_run: bool) -> None:
    from jailbee.tui import info_plain

    info_plain("")
    info_plain("Summary" + (" (dry run)" if dry_run else ""))
    for r in results:
        line = f"  {r.prefix or r.root}: base build {r.build}, apply {r.apply}, seed {r.seed}"
        if r.error:
            line += f" — {r.error}"
        info_plain(line)
