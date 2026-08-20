"""One-shot migration of pre-1.0 `gie` state into the jailbee namespace.

Deprecated on arrival: this module and the `jailbee migrate` command exist
only for installations that predate the rename, and are removed in 2.0.0
together with the rest of the compatibility surface. Keeping it in one file
makes that removal a single `git rm`.

Uses `Incus` for daemon work and plain `subprocess` for git and systemctl,
matching registry.py/maintenance.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jailbee.paths import xdg_data_home
from jailbee.profiles import LOOSE_PROFILE_SUFFIX

if TYPE_CHECKING:
    from jailbee.incus import Incus

OLD_LABEL_PREFIX = "user.gie."
NEW_LABEL_PREFIX = "user.jailbee."
OLD_ENV_KEY = "environment.GIE_BRANCH"
NEW_ENV_KEY = "environment.JAILBEE_BRANCH"
OLD_BRIDGE = "gie-loose"
NEW_BRIDGE = "jailbee-loose"
OLD_MIRROR_CONTAINER = "gie-registry-mirror"
OLD_MIRROR_PROFILE = "gie-registry-mirror-profile"
OLD_UNITS: tuple[str, ...] = ("gie-net-refresh.timer", "gie-net-refresh.service")

# Every refresh timer that can create `<state>/jailbee` behind the
# migrator's back, old namespace and new. The new one is in the list
# because `jailbee net install` — which `make install` runs — enables it
# `--now`, so a dogfooding install from a checkout leaves it ticking
# against state that has not been migrated yet.
REFRESH_TIMERS: tuple[str, ...] = ("gie-net-refresh.timer", "jailbee-net-refresh.timer")
OLD_SKILL_DIRS: tuple[str, ...] = ("gie-usage", "gie-repo-setup")
OLD_SKILLS_LOCK = ".gie-skills.lock"
OLD_JOBS_TABLE = "background_op"
MIGRATION_GUIDE = "docs/migrating-from-gie.md"
_OLD_REF_PREFIXES: tuple[tuple[str, str], ...] = (
    ("refs/gie-sub/", "refs/jailbee-sub/"),
    ("refs/gie/", "refs/jailbee/"),
)


class IncompleteMigrationError(Exception):
    """One step could not be finished; everything before it was applied.

    Raised instead of letting an `IncusError`/`OSError` traceback escape, so
    the CLI can name what is left and exit non-zero. Re-running
    `jailbee migrate` picks up exactly the remainder — every step is
    independently idempotent.
    """


@dataclass(frozen=True)
class DirMove:
    """A host directory to move wholesale.

    ``compat_symlink`` leaves ``src`` behind as a symlink to ``dst`` after
    the move. Set for the data directory: profile disk devices and
    per-container disk devices persist *absolute* source paths under
    ``<data>/gie/shared/...`` (profiles.py, lifecycle.py), and Incus
    validates a disk source at container start. Without the symlink the
    next `jailbee start` of an existing container fails with
    "Missing source path"; per-container devices are attached once at
    creation and not even `jailbee apply` repairs them.
    """

    src: Path
    dst: Path
    compat_symlink: bool = False


@dataclass(frozen=True)
class DirConflict:
    """A destination directory that already exists, blocking its move.

    New code creates these on sight — `db.get_engine()` makes
    ``<state>/jailbee`` on any `jailbee doctor`, `net install` or refresh
    tick — so the destination routinely exists minutes after upgrading and
    long before anyone types `jailbee migrate`. That is not a reason to
    refuse: an ``is_empty`` destination holds nothing anyone can lose, and
    the migrator removes it unprompted. A non-empty one is removed only
    with the user's explicit consent, since merging is the alternative and
    only the user knows which side is real.
    """

    src: Path
    dst: Path
    #: Names directly under ``dst``, for showing the user what is at stake.
    entries: tuple[str, ...]
    #: True when nothing under ``dst`` carries state — see `_holds_no_state`.
    is_empty: bool


@dataclass(frozen=True)
class ContainerRelabel:
    """Old config keys found on one container."""

    name: str
    keys: tuple[str, ...]
    repo_dir: str | None


@dataclass(frozen=True)
class RefRename:
    """One git ref to recreate under the new namespace."""

    repo_dir: Path
    old_ref: str
    new_ref: str
    oid: str


@dataclass(frozen=True)
class LooseRepoint:
    """A `<prefix>-net-loose` profile whose NIC must move to the new bridge."""

    profile: str
    prefix: str


@dataclass(frozen=True)
class MigrationPlan:
    """Everything the migrator will do, computed before it does any of it."""

    dir_moves: tuple[DirMove, ...] = ()
    dir_conflicts: tuple[DirConflict, ...] = ()
    relabels: tuple[ContainerRelabel, ...] = ()
    ref_renames: tuple[RefRename, ...] = ()
    old_units: tuple[str, ...] = ()
    old_skill_paths: tuple[Path, ...] = ()
    migrate_bridge: bool = False
    loose_repoints: tuple[LooseRepoint, ...] = ()
    delete_mirror_container: bool = False
    delete_mirror_profile: bool = False
    blockers: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        """True when there is no pre-1.0 state left to migrate."""
        return not (
            self.dir_moves
            or self.relabels
            or self.ref_renames
            or self.old_units
            or self.old_skill_paths
            or self.migrate_bridge
            or self.delete_mirror_container
            or self.delete_mirror_profile
        )


def _config_base() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else Path.home() / ".config"


def _state_base() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    return Path(xdg) if xdg else Path.home() / ".local" / "state"


def _old_state_db() -> Path:
    """Path of the pre-1.0 state database (may not exist)."""
    return _state_base() / "gie" / "state.sqlite"


def _old_dirs() -> tuple[Path, ...]:
    """The three pre-1.0 host directories, in (config, data, state) order."""
    return tuple(base / "gie" for base in (_config_base(), xdg_data_home(), _state_base()))


def _is_compat_symlink(src: Path, dst: Path) -> bool:
    """True when `src` is the compatibility symlink left by a previous run."""
    return src.is_symlink() and os.path.realpath(src) == os.path.realpath(dst)


# Sidecars SQLite writes beside the database in WAL mode. Their presence
# says nothing about whether the database holds anything.
_DB_SIDECARS = frozenset({"state.sqlite-wal", "state.sqlite-shm"})


def _holds_no_state(path: Path) -> bool:
    """True when `path` holds nothing anyone could lose by deleting it.

    Either an empty directory, or one holding only a freshly bootstrapped
    `state.sqlite` — every table empty but for the single schema-version
    row `db._ensure_schema` writes on creation — plus its WAL sidecars.
    That is exactly the shape `db.get_engine()` leaves when new code runs
    before the migration, which is the case this path exists for.

    Conservative in every ambiguous direction: an unreadable database, an
    unexpected file, or any row anywhere else all count as state.
    """
    import sqlite3

    from jailbee.db.models import SchemaMeta

    entries = {p.name for p in path.iterdir()}
    if not entries:
        return True
    if not entries <= {"state.sqlite"} | _DB_SIDECARS:
        return False
    db_path = path / "state.sqlite"
    if not db_path.is_file():
        return True  # sidecars with no database: nothing readable to lose
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            tables = [
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if row[0] != SchemaMeta.__tablename__
            ]
            return all(
                conn.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() is None
                for table in tables
            )
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _dir_moves() -> tuple[tuple[DirMove, ...], tuple[DirConflict, ...]]:
    """The old→new host directory moves, plus the destinations already taken.

    A destination that already exists is *not* silently skipped:
    `db.get_engine()` creates `<state>/jailbee/` on any `jailbee doctor`,
    `net install` or refresh tick (both the pre-1.0 and the current timer
    fire every 60s and run the freshly installed code), so it routinely
    exists minutes after upgrading and long before anyone types `jailbee
    migrate`. Moving on regardless would drop every RegisteredRepo row, the
    egress pool, GUI state and the job log.

    Those destinations come back as `DirConflict`s rather than as blockers:
    the move is still planned, and `execute_plan` clears the destination
    first — unprompted when it provably holds nothing, and otherwise only
    for a destination the caller has explicitly approved.
    """
    moves: list[DirMove] = []
    conflicts: list[DirConflict] = []
    data_dir = xdg_data_home() / "gie"
    for src in _old_dirs():
        dst = src.parent / "jailbee"
        if _is_compat_symlink(src, dst):
            continue
        if not src.is_dir():
            continue
        if dst.exists():
            conflicts.append(
                DirConflict(
                    src=src,
                    dst=dst,
                    entries=tuple(sorted(p.name for p in dst.iterdir())) if dst.is_dir() else (),
                    is_empty=dst.is_dir() and _holds_no_state(dst),
                )
            )
        moves.append(DirMove(src=src, dst=dst, compat_symlink=src == data_dir))
    return tuple(moves), tuple(conflicts)


def _scan_containers(incus: Incus) -> tuple[ContainerRelabel, ...]:
    """Relabel actions for every container still carrying old config keys."""
    return _relabels_from(incus.list_containers(fast=True))


def _relabels_from(containers: list[dict[str, Any]]) -> tuple[ContainerRelabel, ...]:
    """`_scan_containers` over an already-fetched container list."""
    relabels: list[ContainerRelabel] = []
    for raw in containers:
        config = raw.get("config") or {}
        keys = tuple(
            sorted(k for k in config if k.startswith(OLD_LABEL_PREFIX) or k == OLD_ENV_KEY)
        )
        if not keys:
            continue
        relabels.append(
            ContainerRelabel(
                name=str(raw["name"]),
                keys=keys,
                repo_dir=config.get(f"{OLD_LABEL_PREFIX}repo_dir"),
            )
        )
    return tuple(relabels)


def _used_by_parts(ref: str) -> tuple[str, str]:
    """Split an Incus `used_by` URL into (kind, name).

    ``/1.0/profiles/app-net-loose?project=default`` → ``("profiles",
    "app-net-loose")``. An unrecognised shape yields ``("", ref)`` so the
    caller can still report it verbatim.
    """
    path = ref.split("?", 1)[0].rstrip("/")
    parts = [p for p in path.split("/") if p]
    if len(parts) < 3:
        return "", ref
    return parts[-2], parts[-1]


def _used_by_label(ref: str) -> str:
    """Human-readable form of a `used_by` URL, e.g. ``profile app-net-loose``."""
    kind, name = _used_by_parts(ref)
    singular = {"profiles": "profile", "instances": "container"}.get(kind)
    return f"{singular} {name}" if singular else name


def _loose_repoints(used_by: list[str]) -> tuple[LooseRepoint, ...]:
    """The `<prefix>-net-loose` profiles referencing the old bridge.

    Sourced from the network's own `used_by` rather than from the repos
    discovered via container labels: `jailbee apply` writes the loose
    profile for every initialised repo whether or not it has containers, and
    a repo whose containers were all destroyed would otherwise keep the old
    bridge pinned forever.
    """
    out: list[LooseRepoint] = []
    for ref in used_by:
        kind, name = _used_by_parts(ref)
        if kind == "profiles" and name.endswith(LOOSE_PROFILE_SUFFIX):
            out.append(LooseRepoint(profile=name, prefix=name[: -len(LOOSE_PROFILE_SUFFIX)]))
    return tuple(out)


def _git_lines(repo_dir: Path, args: list[str]) -> list[str]:
    """Run git in `repo_dir`, returning stdout lines ([] if the call fails)."""
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _ref_renames(repo_dirs: tuple[Path, ...]) -> tuple[RefRename, ...]:
    """Old refs, with their object ids, across every discovered repo."""
    out: list[RefRename] = []
    for repo_dir in sorted(set(repo_dirs)):
        lines = _git_lines(
            repo_dir,
            ["for-each-ref", "--format=%(refname) %(objectname)", "refs/gie", "refs/gie-sub"],
        )
        for line in lines:
            refname, _, oid = line.partition(" ")
            for old, new in _OLD_REF_PREFIXES:
                if refname.startswith(old):
                    out.append(
                        RefRename(
                            repo_dir=repo_dir,
                            old_ref=refname,
                            new_ref=new + refname[len(old) :],
                            oid=oid.strip(),
                        )
                    )
                    break
    return tuple(out)


def _old_units() -> tuple[str, ...]:
    """Pre-1.0 unit files still present in the user systemd directory."""
    units_dir = Path.home() / ".config" / "systemd" / "user"
    return tuple(unit for unit in OLD_UNITS if (units_dir / unit).exists())


def _old_skill_paths() -> tuple[Path, ...]:
    """Old bundled-skill directories and lock files under either shared root.

    Checked under both the pre- and post-move data roots, so the plan is
    accurate whether or not `_dir_moves` has run yet. `execute_plan` maps
    each pre-move path through the completed moves before deleting it.
    """
    found: list[Path] = []
    for root in (xdg_data_home() / "gie" / "shared", xdg_data_home() / "jailbee" / "shared"):
        for claude_dir in sorted(root.glob("*/claude")):
            found.extend(
                claude_dir / "skills" / name
                for name in OLD_SKILL_DIRS
                if (claude_dir / "skills" / name).is_dir()
            )
            lock = claude_dir / OLD_SKILLS_LOCK
            if lock.exists():
                found.append(lock)
    return tuple(found)


def _pending_jobs() -> tuple[str, ...]:
    """Container names recorded in the pre-1.0 state DB.

    Reads the *old* database directly: `db.state_dir()` already points at the
    new location, where opening an engine would create an empty file and
    report no jobs — a false all-clear.
    """
    db_path = _old_state_db()
    if not db_path.is_file():
        return ()
    from sqlalchemy.exc import SQLAlchemyError
    from sqlmodel import Session, create_engine

    from jailbee.background import list_all_jobs

    try:
        with Session(create_engine(f"sqlite:///{db_path}")) as session:
            return tuple(sorted(list_all_jobs(session)))
    except SQLAlchemyError:
        # No background_op table (older schema) means no jobs to lose.
        return ()


def _registered_repo_dirs() -> tuple[Path, ...]:
    """Repo roots recorded in the pre-1.0 state DB's `registered_repo` table.

    A second, label-independent source of repos: container labels are unset
    part-way through `execute_plan`, and a repo whose containers were all
    destroyed has no labels at all — either way its `refs/gie/*` would never
    be found. Read from the old database for the same reason as
    `_pending_jobs`.
    """
    db_path = _old_state_db()
    if not db_path.is_file():
        return ()
    from sqlalchemy.exc import SQLAlchemyError
    from sqlmodel import Session, create_engine, select

    from jailbee.db.models import RegisteredRepo

    try:
        with Session(create_engine(f"sqlite:///{db_path}")) as session:
            return tuple(Path(r.repo_root) for r in session.exec(select(RegisteredRepo)).all())
    except SQLAlchemyError:
        # No registered_repo table (older schema) — nothing to add.
        return ()


def _repo_dirs_from(containers: list[dict[str, Any]]) -> tuple[Path, ...]:
    """Repo roots labelled on containers, under either namespace."""
    dirs: list[Path] = []
    for raw in containers:
        config = raw.get("config") or {}
        for key in (f"{OLD_LABEL_PREFIX}repo_dir", f"{NEW_LABEL_PREFIX}repo_dir"):
            value = config.get(key)
            if value:
                dirs.append(Path(str(value)))
    return tuple(dirs)


def stop_refresh_timers() -> None:
    """Stop every refresh timer that could race the migration.

    Called before the plan is built, because the window this closes opens
    before it: a tick landing while the confirmation prompt waits recreates
    `<state>/jailbee` after `build_plan` has already looked, and the move
    loop's own guard then aborts a migration that had been about to work.

    A bare `stop` and nothing more. Unlike `_replace_units`, which disables
    and deletes the pre-1.0 units, this cannot leave a machine permanently
    without a refresh timer: `_replace_units` installs the replacement
    later in the same run, and an aborted run leaves a timer that
    `systemctl --user start` brings straight back. Failures are ignored for
    the same reason — a timer that is not installed, not running, or on a
    host without systemd is not a problem to report.
    """
    for unit in REFRESH_TIMERS:
        subprocess.run(
            ["systemctl", "--user", "stop", unit],
            check=False,
            capture_output=True,
        )


def build_plan(incus: Incus) -> MigrationPlan:
    """Inspect the host and return everything that needs migrating.

    Pure with respect to the machine: reads only. `blockers` being non-empty
    means the caller must refuse to execute.
    """
    relabels = _scan_containers(incus)
    repo_dirs = tuple(Path(r.repo_dir) for r in relabels if r.repo_dir) + _registered_repo_dirs()
    dir_moves, dir_conflicts = _dir_moves()

    migrate_bridge = incus.network_exists(OLD_BRIDGE)
    used_by = incus.network_used_by(OLD_BRIDGE) if migrate_bridge else []
    delete_mirror_container = incus.exists(OLD_MIRROR_CONTAINER)
    # A container this plan is going to delete is not a reason to refuse the
    # plan. The pre-1.0 registry mirror is one of the old bridge's users by
    # design, and `execute_plan` deletes it *before* the network work for
    # exactly that reason — so counting it here blocked the migration on its
    # own cleanup. It was also unactionable: the blocker tells you to run
    # `jailbee net strict <name>` from the container's own repo, and the
    # mirror is a host-level singleton with no repo to run it from, which
    # left no legitimate way forward at all.
    attached = sorted(
        name
        for kind, name in map(_used_by_parts, used_by)
        if kind == "instances" and not (delete_mirror_container and name == OLD_MIRROR_CONTAINER)
    )

    blockers: list[str] = []
    jobs = _pending_jobs()
    if jobs:
        quoted = ", ".join(f"'{name}'" for name in jobs)
        blockers.append(
            f"background jobs are recorded for: {', '.join(jobs)} — let them finish, "
            f"then re-run. `jailbee job clear` reads the new database and will not "
            f"see them; clear stale rows in the pre-1.0 one directly: "
            f'sqlite3 {_old_state_db()} "delete from {OLD_JOBS_TABLE} '
            f'where container_name in ({quoted})"'
        )
    if attached:
        blockers.append(
            f"containers still attached to {OLD_BRIDGE}: {', '.join(attached)} — "
            f"run `jailbee net strict <name>` on each from that container's own "
            f"repo (the command loads a repo config), switch them back to loose "
            f"once the migration is done, then re-run"
        )

    return MigrationPlan(
        dir_moves=dir_moves,
        dir_conflicts=dir_conflicts,
        relabels=relabels,
        ref_renames=_ref_renames(repo_dirs),
        old_units=_old_units(),
        old_skill_paths=_old_skill_paths(),
        migrate_bridge=migrate_bridge,
        loose_repoints=_loose_repoints(used_by),
        delete_mirror_container=delete_mirror_container,
        delete_mirror_profile=incus.profile_exists(OLD_MIRROR_PROFILE),
        blockers=tuple(blockers),
    )


def leftovers(incus: Incus) -> tuple[str, ...]:
    """Every piece of pre-1.0 state still on this host, named.

    Deliberately independent of `build_plan`: a plan reports only what it is
    willing to *do*, so anything it refuses (a directory whose target already
    exists) or cannot see would read as clean. `doctor` needs the state
    itself.

    The `<data>/gie` compatibility symlink is not leftover state — it is part
    of the 1.0 compatibility surface and is removed in 2.0.0.
    """
    found: list[str] = []
    for src in _old_dirs():
        if _is_compat_symlink(src, src.parent / "jailbee"):
            continue
        if src.is_dir():
            found.append(f"directory {src}")
    containers = incus.list_containers(fast=True)
    found.extend(f"old labels on container {r.name}" for r in _relabels_from(containers))
    if incus.network_exists(OLD_BRIDGE):
        found.append(f"network {OLD_BRIDGE}")
    if incus.exists(OLD_MIRROR_CONTAINER):
        found.append(f"container {OLD_MIRROR_CONTAINER}")
    if incus.profile_exists(OLD_MIRROR_PROFILE):
        found.append(f"profile {OLD_MIRROR_PROFILE}")
    found.extend(f"systemd unit {unit}" for unit in _old_units())
    found.extend(f"stale skill path {path}" for path in _old_skill_paths())

    repo_dirs = _repo_dirs_from(containers) + _registered_repo_dirs()
    per_repo = Counter(rename.repo_dir for rename in _ref_renames(repo_dirs))
    found.extend(
        f"{count} pre-1.0 git ref(s) in {repo}" for repo, count in sorted(per_repo.items())
    )
    return tuple(found)


def render_plan(plan: MigrationPlan) -> str:
    """Human-readable plan, suitable for --dry-run and the confirmation prompt."""
    if plan.is_empty and not plan.blockers:
        return "Nothing to migrate — no pre-1.0 gie state found."

    lines: list[str] = ["Migration plan:"]
    for conflict in plan.dir_conflicts:
        if conflict.is_empty:
            lines.append(f"  clear   {conflict.dst} (created by newer code, holds no state)")
        else:
            lines.append(
                f"  CLEAR?  {conflict.dst} already exists and holds "
                f"{', '.join(conflict.entries)} — you will be asked before it is deleted"
            )
    for move in plan.dir_moves:
        lines.append(f"  move    {move.src} -> {move.dst}")
        if move.compat_symlink:
            lines.append(f"  symlink {move.src} -> {move.dst} (compatibility, removed in 2.0.0)")
    for relabel in plan.relabels:
        lines.append(f"  relabel {relabel.name}: {', '.join(relabel.keys)}")
    for rename in plan.ref_renames:
        lines.append(f"  ref     {rename.repo_dir}: {rename.old_ref} -> {rename.new_ref}")
    for unit in plan.old_units:
        lines.append(f"  unit    remove {unit}, install its jailbee- equivalent")
    for path in plan.old_skill_paths:
        lines.append(f"  delete  {path}")
    if plan.delete_mirror_container:
        lines.append(
            f"  delete  container {OLD_MIRROR_CONTAINER} "
            f"(re-create it with `jailbee registry up` when next needed)"
        )
    if plan.delete_mirror_profile:
        lines.append(f"  delete  profile {OLD_MIRROR_PROFILE}")
    if plan.migrate_bridge:
        lines.append(f"  network ensure {NEW_BRIDGE} exists")
        for repoint in plan.loose_repoints:
            lines.append(f"  profile repoint {repoint.profile} at {NEW_BRIDGE}")
        lines.append(f"  network delete {OLD_BRIDGE}")

    if plan.blockers:
        lines.append("")
        lines.append("BLOCKED — nothing has been changed:")
        lines.extend(f"  - {reason}" for reason in plan.blockers)
    return "\n".join(lines)


def execute_plan(
    plan: MigrationPlan, incus: Incus, *, approved_removals: frozenset[Path] = frozenset()
) -> None:
    """Apply `plan`. Raises RuntimeError if the plan is blocked.

    Ordered so that a failure part-way leaves a re-runnable state, highest
    value first: the directory moves (atomic renames), then git refs, then
    the per-key-idempotent container relabels, then stale skills. Only then
    come the steps that can fail — unit replacement (`check=True` systemctl
    calls) and, last, the network work, whose final step reports rather than
    raises when something still holds the old bridge. Nothing that can raise
    sits between the high-value mutations and `_replace_units`, and the
    registry mirror is deleted before the network work because its profile
    is one of the old bridge's users.

    ``approved_removals`` names the non-empty `DirConflict` destinations the
    caller has consent to delete. Consent is the CLI's to obtain, never this
    function's to assume: a conflict that provably holds no state is cleared
    regardless, and any other one not named here raises rather than deleting
    something the user never agreed to lose.
    """
    if plan.blockers:
        raise RuntimeError("migration is blocked: " + "; ".join(plan.blockers))

    for conflict in plan.dir_conflicts:
        if not conflict.is_empty and conflict.dst not in approved_removals:
            raise IncompleteMigrationError(
                f"{conflict.dst} already exists and holds state that was not approved "
                f"for deletion, so nothing was moved. Merge {conflict.src} into it by "
                f"hand, or re-run `jailbee migrate` and approve the deletion."
            )
        shutil.rmtree(conflict.dst)

    for move in plan.dir_moves:
        # Re-checked here, not just in `build_plan`: `shutil.move` into an
        # existing directory *nests* rather than fails, so `<state>/gie` would
        # become `<state>/jailbee/gie` and the empty database somebody else
        # created would stay the live one. The window is not theoretical — the
        # pre-1.0 `gie-net-refresh.timer` fires every 60s and, after the
        # upgrade, runs the new code, which creates `<state>/jailbee` on the
        # spot. That can happen while the confirmation prompt is waiting.
        if move.dst.exists():
            raise IncompleteMigrationError(
                f"{move.dst} appeared after this migration was planned, so "
                f"{move.src} was not moved (moving it now would nest it as "
                f"{move.dst / move.src.name} and leave the new, empty state live). "
                f"Stop the pre-1.0 timer first — `systemctl --user stop "
                f"gie-net-refresh.timer` — then remove {move.dst} if it is empty, "
                f"or merge {move.src} into it by hand, and re-run `jailbee migrate`."
            )
        move.dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(move.src), str(move.dst))
        if move.compat_symlink:
            move.src.symlink_to(move.dst)

    # Refs before relabels: `build_plan` derives repo directories partly from
    # the labels the relabel loop unsets, so an interrupted run that had done
    # it the other way round would rebuild a plan with no repos and no refs,
    # orphaning `refs/gie/*` permanently.
    _rename_refs(plan.ref_renames)

    for relabel in plan.relabels:
        for old_key in relabel.keys:
            value = incus.config_get(relabel.name, old_key)
            if value is None:
                continue
            new_key = (
                NEW_ENV_KEY
                if old_key == OLD_ENV_KEY
                else NEW_LABEL_PREFIX + old_key[len(OLD_LABEL_PREFIX) :]
            )
            incus.config_set(relabel.name, new_key, value)
            incus.config_unset(relabel.name, old_key)

    for path in plan.old_skill_paths:
        # Paths were resolved before the moves, so a first run planned them
        # under the *old* data root that no longer exists.
        target = _after_moves(path, plan.dir_moves)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()

    if plan.old_units:
        _replace_units(plan.old_units)

    if plan.delete_mirror_container:
        incus.delete(OLD_MIRROR_CONTAINER, force=True)
    if plan.delete_mirror_profile:
        incus.profile_delete(OLD_MIRROR_PROFILE)

    if plan.migrate_bridge:
        _migrate_bridge(plan.loose_repoints, incus)


def _after_moves(path: Path, moves: tuple[DirMove, ...]) -> Path:
    """`path` rewritten through any completed `DirMove` containing it."""
    for move in moves:
        if path == move.src or move.src in path.parents:
            return move.dst / path.relative_to(move.src)
    return path


def _rename_refs(renames: tuple[RefRename, ...]) -> None:
    """Recreate each old ref under the new namespace, then drop the old one.

    The delete is conditional on the create having succeeded: a D/F conflict
    against an existing `refs/jailbee/...`, a stale lock or a read-only repo
    all make `update-ref` exit non-zero, and deleting the old ref anyway
    would destroy the only copy of the anchor.
    """
    from jailbee.tui import warn

    for rename in renames:
        created = subprocess.run(
            ["git", "-C", str(rename.repo_dir), "update-ref", rename.new_ref, rename.oid],
            capture_output=True,
            text=True,
            check=False,
        )
        if created.returncode != 0:
            detail = (created.stderr or "").strip()
            warn(
                f"could not create {rename.new_ref} in {rename.repo_dir} "
                f"({detail or 'git update-ref failed'}) — keeping {rename.old_ref}"
            )
            continue
        subprocess.run(
            ["git", "-C", str(rename.repo_dir), "update-ref", "-d", rename.old_ref],
            check=False,
        )


def _migrate_bridge(repoints: tuple[LooseRepoint, ...], incus: Incus) -> None:
    """Create the new bridge, repoint the loose profiles, drop the old bridge.

    Renaming `gie-loose` cannot work: Incus refuses while any instance *or
    profile* uses it, and every initialised repo's `<prefix>-net-loose`
    profile does, whether or not a container is currently loose. So the new
    bridge is created alongside, each loose profile is rewritten to point at
    it, and only then is the old bridge deleted.
    """
    from jailbee.incus import IncusError
    from jailbee.init_command import ensure_loose_bridge
    from jailbee.profiles import loose_net_profile_yaml

    # This is the last step, so any Incus failure here means everything else
    # is already done — report it as such rather than as a traceback.
    try:
        ensure_loose_bridge(incus)
        for repoint in repoints:
            incus.profile_set_yaml(repoint.profile, loose_net_profile_yaml(repoint.prefix))

        remaining = incus.network_used_by(OLD_BRIDGE)
        if remaining:
            held_by = ", ".join(_used_by_label(ref) for ref in remaining)
            raise IncompleteMigrationError(
                f"everything except the old bridge was migrated. {OLD_BRIDGE} is still "
                f"in use by: {held_by} — point each at {NEW_BRIDGE} (or remove it), "
                f"then re-run `jailbee migrate` to delete {OLD_BRIDGE}."
            )
        incus.network_delete(OLD_BRIDGE)
    except IncusError as e:
        raise IncompleteMigrationError(
            f"everything except the old bridge was migrated. {OLD_BRIDGE} could not be "
            f"replaced by {NEW_BRIDGE}: {e} — fix that, then re-run `jailbee migrate`."
        ) from e


def _replace_units(old_units: tuple[str, ...]) -> None:
    """Disable and delete the pre-1.0 units, then install the current ones.

    Refuses up front when `jailbee` is not on PATH: `install_systemd_units`
    only warns in that case and installs nothing, which — after the unlink
    below — would leave the machine with no refresh timer at all, silently
    stopping egress-pool refreshes and TTL-driven loose reverts.
    """
    from jailbee.init_command import install_systemd_units

    if shutil.which("jailbee") is None:
        raise IncompleteMigrationError(
            "`jailbee` is not on PATH, so the replacement net-refresh units cannot "
            "be installed — the pre-1.0 units were left in place and everything "
            "before this step is done. Install jailbee, then re-run `jailbee migrate`."
        )

    units_dir = Path.home() / ".config" / "systemd" / "user"
    for unit in old_units:
        subprocess.run(["systemctl", "--user", "disable", "--now", unit], check=False)
        (units_dir / unit).unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    install_systemd_units()
