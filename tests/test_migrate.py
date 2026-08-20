"""Unit tests for the pre-1.0 state migrator. Deleted with it in 2.0.0."""

from __future__ import annotations

from pathlib import Path

import pytest


def _container(name: str, config: dict[str, str], profiles: list[str] | None = None) -> dict:
    return {
        "name": name,
        "status": "Running",
        "profiles": profiles or [],
        "config": config,
        "state": None,
    }


def _incus(
    mocker,
    *,
    containers: list[dict] | None = None,
    networks: tuple[str, ...] = (),
    used_by: tuple[str, ...] = (),
    instances: tuple[str, ...] = (),
    profiles: tuple[str, ...] = (),
):
    """A mock `Incus` whose reads answer from the given host inventory.

    Every read `build_plan`/`leftovers` performs is answered here, so a test
    only has to describe the state it cares about — and, crucially, so an
    unconfigured method never returns a truthy MagicMock that would make an
    assertion pass for the wrong reason.
    """
    incus = mocker.MagicMock()
    incus.list_containers.return_value = containers or []
    incus.network_exists.side_effect = lambda name: name in networks
    incus.network_used_by.side_effect = lambda name: list(used_by) if name in networks else []
    incus.exists.side_effect = lambda name: name in instances
    incus.profile_exists.side_effect = lambda name: name in profiles
    return incus


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    """Point all three XDG bases at tmp_path and return them.

    Also repoints HOME: `_old_units()` reads `Path.home() / ".config" /
    "systemd" / "user"` directly (matching real systemd, which ignores
    `$XDG_CONFIG_HOME`), so a real machine with genuine leftover pre-1.0
    `gie` units would otherwise leak into every test in this module.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_STATE_HOME", "state"),
    ):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(var, str(tmp_path / sub))
    return tmp_path


@pytest.fixture
def on_path(mocker):
    """Pretend `jailbee` is installed on PATH (see `_replace_units`)."""
    return mocker.patch("jailbee.migrate.shutil.which", return_value="/usr/bin/jailbee")


def test_build_plan_is_empty_when_nothing_old_exists(xdg, mocker):
    from jailbee.migrate import build_plan

    plan = build_plan(_incus(mocker))

    assert plan.is_empty
    assert plan.blockers == ()


def test_build_plan_collects_dir_moves(xdg, mocker):
    from jailbee.migrate import build_plan

    (xdg / "config" / "gie").mkdir()
    (xdg / "state" / "gie").mkdir()

    plan = build_plan(_incus(mocker))

    assert [(m.src.name, m.dst.name) for m in plan.dir_moves] == [
        ("gie", "jailbee"),
        ("gie", "jailbee"),
    ]
    assert {m.src.parent.name for m in plan.dir_moves} == {"config", "state"}
    assert not plan.is_empty


def test_build_plan_plans_to_clear_an_empty_target_dir_instead_of_blocking(xdg, mocker):
    """A pre-existing target must never turn into a silent skip — but an
    empty one is also not a reason to refuse.

    `db.get_engine()` creates `<state>/jailbee` on any `doctor`, `net
    install` or refresh tick, so this is the *normal* state a minute after
    upgrading. Skipping would drop every RegisteredRepo row with no output;
    blocking made the migration unrunnable after `make install`.
    """
    from jailbee.migrate import build_plan

    (xdg / "state" / "gie").mkdir()
    (xdg / "state" / "jailbee").mkdir()

    plan = build_plan(_incus(mocker))

    assert plan.blockers == ()
    assert [(m.src, m.dst) for m in plan.dir_moves] == [
        (xdg / "state" / "gie", xdg / "state" / "jailbee")
    ]
    conflict = plan.dir_conflicts[0]
    assert conflict.dst == xdg / "state" / "jailbee"
    assert conflict.is_empty is True


def test_build_plan_treats_a_freshly_bootstrapped_db_as_empty(xdg, mocker):
    """The shape `db.get_engine()` leaves is the whole point of the check.

    A brand-new state.sqlite has every table created and only the single
    schema-version row in it. Counting that as state would make the
    auto-clear path unreachable in exactly the case it was written for.
    """
    from jailbee.db import get_engine
    from jailbee.migrate import build_plan

    (xdg / "state" / "gie").mkdir()
    get_engine()  # creates <state>/jailbee/state.sqlite, schema only

    plan = build_plan(_incus(mocker))

    assert (xdg / "state" / "jailbee" / "state.sqlite").is_file()
    assert plan.blockers == ()
    assert plan.dir_conflicts[0].is_empty is True


def test_build_plan_treats_a_db_with_real_rows_as_state(xdg, mocker):
    """One registered repo is enough to make the directory worth keeping."""
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee.db import get_engine
    from jailbee.db.models import RegisteredRepo
    from jailbee.migrate import build_plan

    (xdg / "state" / "gie").mkdir()
    engine = get_engine()
    with Session(engine) as session:
        session.add(
            RegisteredRepo(
                container_prefix="app",
                repo_root=str(xdg / "repo"),
                registered_at=datetime.now(UTC),
            )
        )
        session.commit()

    plan = build_plan(_incus(mocker))

    conflict = plan.dir_conflicts[0]
    assert conflict.is_empty is False
    assert "state.sqlite" in conflict.entries


def test_build_plan_ignores_the_data_compat_symlink(xdg, mocker):
    """`<data>/gie -> <data>/jailbee` is compatibility surface, not leftovers.

    A completed migration leaves it behind on purpose (C3). Treating it as an
    un-migrated directory would block every subsequent `jailbee migrate` and
    make `doctor` permanently unhappy.
    """
    from jailbee.migrate import build_plan, leftovers

    (xdg / "data" / "jailbee").mkdir()
    (xdg / "data" / "gie").symlink_to(xdg / "data" / "jailbee")

    incus = _incus(mocker)
    plan = build_plan(incus)

    assert plan.dir_moves == ()
    assert plan.blockers == ()
    assert plan.is_empty
    assert leftovers(incus) == ()


def test_build_plan_marks_only_the_data_dir_for_the_compat_symlink(xdg, mocker):
    """Only the data dir is referenced by absolute path from Incus devices."""
    from jailbee.migrate import build_plan

    for sub in ("config", "data", "state"):
        (xdg / sub / "gie").mkdir()

    plan = build_plan(_incus(mocker))

    assert {m.src: m.compat_symlink for m in plan.dir_moves} == {
        xdg / "config" / "gie": False,
        xdg / "data" / "gie": True,
        xdg / "state" / "gie": False,
    }


def test_build_plan_collects_relabels_and_ignores_clean_containers(xdg, mocker):
    from jailbee.migrate import build_plan

    # app-feat has a repo_dir, so build_plan reaches _ref_renames, which
    # shells out to `git for-each-ref`. Mock subprocess.run so this test
    # never spawns a real git process — the previous version of this test
    # relied on "/home/u/app" not existing on the test machine, which is an
    # accident, not a guarantee. See docs/testing conventions: every test in
    # this suite is fully mocked.
    run = mocker.patch("subprocess.run")
    run.return_value = mocker.MagicMock(returncode=0, stdout="")
    incus = _incus(
        mocker,
        containers=[
            _container("app-feat", {"user.gie.branch": "feat", "user.gie.repo_dir": "/home/u/app"}),
            _container("app-done", {"user.jailbee.branch": "done"}),
            _container("app-env", {"environment.GIE_BRANCH": "x"}),
        ],
    )

    plan = build_plan(incus)

    assert [r.name for r in plan.relabels] == ["app-feat", "app-env"]
    assert plan.relabels[0].keys == ("user.gie.branch", "user.gie.repo_dir")
    assert plan.relabels[0].repo_dir == "/home/u/app"
    assert plan.relabels[1].keys == ("environment.GIE_BRANCH",)
    run.assert_called_once_with(
        [
            "git",
            "-C",
            "/home/u/app",
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/gie",
            "refs/gie-sub",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert plan.ref_renames == ()


def test_build_plan_finds_repos_recorded_in_the_old_state_db(xdg, mocker):
    """Ref discovery must not depend on labels the migrator itself unsets.

    A repo whose containers were destroyed — or a re-run after the relabel
    step already ran — has no `user.gie.repo_dir` anywhere, yet its
    `refs/gie/*` still need renaming.
    """
    from datetime import UTC, datetime

    from sqlmodel import Session, create_engine

    from jailbee.db import _ensure_schema
    from jailbee.db.models import RegisteredRepo
    from jailbee.migrate import build_plan

    old_state = xdg / "state" / "gie"
    old_state.mkdir()
    engine = create_engine(f"sqlite:///{old_state / 'state.sqlite'}")
    _ensure_schema(engine)
    with Session(engine) as session:
        session.add(
            RegisteredRepo(
                container_prefix="app",
                repo_root="/home/u/app",
                registered_at=datetime(2026, 8, 12, tzinfo=UTC),
            )
        )
        session.commit()

    run = mocker.patch("subprocess.run")
    run.return_value = mocker.MagicMock(
        returncode=0, stdout="refs/gie/host/feat abc123\n", stderr=""
    )

    plan = build_plan(_incus(mocker))

    assert run.call_args.args[0][:3] == ["git", "-C", "/home/u/app"]
    assert [(r.old_ref, r.new_ref) for r in plan.ref_renames] == [
        ("refs/gie/host/feat", "refs/jailbee/host/feat")
    ]


def test_build_plan_blocks_when_a_container_still_holds_the_old_bridge(xdg, mocker):
    from jailbee.migrate import build_plan

    incus = _incus(
        mocker,
        containers=[_container("app-feat", {"user.gie.branch": "feat"}, ["app-net-loose"])],
        networks=("gie-loose",),
        used_by=("/1.0/instances/app-feat", "/1.0/profiles/app-net-loose"),
    )

    plan = build_plan(incus)

    assert any("app-feat" in b and "net strict" in b for b in plan.blockers)
    assert plan.migrate_bridge is True


def test_build_plan_does_not_block_when_only_profiles_hold_the_old_bridge(xdg, mocker):
    """A profile reference is repointed by the migrator, not by the user.

    Every initialised repo's `<prefix>-net-loose` profile references the old
    bridge whether or not any container is loose right now, so blocking on
    those would block every machine, forever.
    """
    from jailbee.migrate import build_plan

    incus = _incus(
        mocker,
        containers=[_container("app-feat", {"user.gie.branch": "feat"}, ["app-net-strict"])],
        networks=("gie-loose",),
        used_by=("/1.0/profiles/app-net-loose", "/1.0/profiles/other-net-loose"),
    )

    plan = build_plan(incus)

    assert plan.blockers == ()
    assert plan.migrate_bridge is True
    assert [(r.profile, r.prefix) for r in plan.loose_repoints] == [
        ("app-net-loose", "app"),
        ("other-net-loose", "other"),
    ]


def test_build_plan_does_not_block_on_the_registry_mirror_it_deletes(xdg, mocker):
    """The mirror holds the old bridge by design and the plan deletes it.

    `execute_plan` deletes the pre-1.0 mirror *before* the network work for
    precisely this reason, so counting it as a user-actionable obstruction
    blocked the migration on its own cleanup — and unactionably, since the
    blocker's remedy (`jailbee net strict <name>`, run from the container's
    own repo) has no repo to run from for a host-level singleton.
    """
    from jailbee.migrate import build_plan

    incus = _incus(
        mocker,
        networks=("gie-loose",),
        used_by=("/1.0/instances/gie-registry-mirror", "/1.0/profiles/app-net-loose"),
        instances=("gie-registry-mirror",),
        profiles=("gie-registry-mirror-profile",),
    )

    plan = build_plan(incus)

    assert plan.blockers == ()
    assert plan.delete_mirror_container is True
    assert plan.delete_mirror_profile is True


def test_build_plan_still_blocks_on_a_branch_container_beside_the_mirror(xdg, mocker):
    """Excusing the mirror must not excuse anything else on the bridge."""
    from jailbee.migrate import build_plan

    incus = _incus(
        mocker,
        containers=[_container("app-feat", {"user.gie.branch": "feat"}, ["app-net-loose"])],
        networks=("gie-loose",),
        used_by=("/1.0/instances/gie-registry-mirror", "/1.0/instances/app-feat"),
        instances=("gie-registry-mirror",),
    )

    plan = build_plan(incus)

    blocker = next(b for b in plan.blockers if "net strict" in b)
    assert "app-feat" in blocker
    assert "gie-registry-mirror" not in blocker


def test_build_plan_has_no_bridge_work_once_the_old_bridge_is_gone(xdg, mocker):
    from jailbee.migrate import build_plan

    incus = _incus(mocker, networks=("jailbee-loose",))

    plan = build_plan(incus)

    assert plan.migrate_bridge is False
    assert plan.loose_repoints == ()
    assert plan.is_empty


def test_build_plan_blocks_on_jobs_recorded_in_the_old_state_db(xdg, mocker):
    from datetime import UTC, datetime

    from sqlmodel import Session, create_engine

    from jailbee.db import _ensure_schema
    from jailbee.db.models import BackgroundJob
    from jailbee.migrate import build_plan

    old_state = xdg / "state" / "gie"
    old_state.mkdir()
    db_path = old_state / "state.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    _ensure_schema(engine)
    with Session(engine) as session:
        session.add(
            BackgroundJob(
                container_name="app-feat",
                container_prefix="app",
                phase="provision",
                pid=1234,
                log_path="/tmp/x.log",
                started_at=datetime(2026, 8, 12, tzinfo=UTC),
                updated_at=datetime(2026, 8, 12, tzinfo=UTC),
            )
        )
        session.commit()

    blockers = build_plan(_incus(mocker)).blockers
    jobs_blocker = next(b for b in blockers if "app-feat" in b)

    # The remedy has to name the *old* database: `jailbee job clear` opens the
    # new one, reports no such job, exits 1 — and creates the new state dir on
    # its way, which then blocks the state-dir move permanently.
    assert str(db_path) in jobs_blocker
    assert "background_op" in jobs_blocker


def test_build_plan_plans_mirror_deletion(xdg, mocker):
    from jailbee.migrate import build_plan

    incus = _incus(
        mocker,
        instances=("gie-registry-mirror",),
        profiles=("gie-registry-mirror-profile",),
    )

    plan = build_plan(incus)

    assert plan.delete_mirror_container is True
    assert plan.delete_mirror_profile is True


def test_render_plan_lists_every_action_and_marks_blockers(xdg, mocker):
    from jailbee.migrate import build_plan, render_plan

    (xdg / "config" / "gie").mkdir()
    (xdg / "state" / "gie").mkdir()
    (xdg / "state" / "jailbee").mkdir()
    incus = _incus(
        mocker,
        containers=[_container("app-feat", {"user.gie.branch": "feat"}, ["app-net-loose"])],
        networks=("gie-loose",),
        # The container itself holds the bridge, which is still a blocker.
        used_by=("/1.0/profiles/app-net-loose", "/1.0/instances/app-feat"),
    )

    text = render_plan(build_plan(incus))

    assert "config/gie" in text
    assert "app-feat" in text
    assert "repoint app-net-loose" in text
    assert "delete gie-loose" in text
    assert "BLOCKED" in text
    # The empty `<state>/jailbee` is reported as a clear, not as an obstruction.
    assert "clear   " in text and "holds no state" in text


def test_render_plan_flags_a_populated_target_as_needing_consent(xdg, mocker):
    """A destination holding state must read as a question, not as a step."""
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee.db import get_engine
    from jailbee.db.models import RegisteredRepo
    from jailbee.migrate import build_plan, render_plan

    (xdg / "state" / "gie").mkdir()
    with Session(get_engine()) as session:
        session.add(
            RegisteredRepo(
                container_prefix="app",
                repo_root=str(xdg / "repo"),
                registered_at=datetime.now(UTC),
            )
        )
        session.commit()

    text = render_plan(build_plan(_incus(mocker)))

    assert "CLEAR?" in text
    assert "state.sqlite" in text
    assert "you will be asked" in text


def test_render_plan_shows_the_compat_symlink(xdg, mocker):
    from jailbee.migrate import DirMove, MigrationPlan, render_plan

    text = render_plan(
        MigrationPlan(
            dir_moves=(DirMove(src=Path("/d/gie"), dst=Path("/d/jailbee"), compat_symlink=True),)
        )
    )

    assert "symlink /d/gie -> /d/jailbee" in text


def test_execute_plan_moves_dirs_and_relabels(xdg, mocker, on_path):
    from jailbee.migrate import ContainerRelabel, DirMove, MigrationPlan, execute_plan

    src = xdg / "config" / "gie"
    src.mkdir()
    (src / "global.yaml").write_text("x: 1\n")
    incus = _incus(mocker)
    incus.config_get.return_value = "feat"
    mocker.patch("jailbee.init_command.install_systemd_units")
    mocker.patch("subprocess.run")

    plan = MigrationPlan(
        dir_moves=(DirMove(src=src, dst=xdg / "config" / "jailbee"),),
        relabels=(ContainerRelabel(name="app-feat", keys=("user.gie.branch",), repo_dir=None),),
    )
    execute_plan(plan, incus)

    assert (xdg / "config" / "jailbee" / "global.yaml").read_text() == "x: 1\n"
    assert not src.exists()
    incus.config_set.assert_called_once_with("app-feat", "user.jailbee.branch", "feat")
    incus.config_unset.assert_called_once_with("app-feat", "user.gie.branch")


def test_execute_plan_leaves_a_compat_symlink_for_the_data_dir(xdg, mocker, on_path):
    """Incus disk devices persist absolute `<data>/gie/shared/...` sources.

    Profile devices and per-container devices alike; the per-container ones
    are attached once at creation and never refreshed, so without this
    symlink the next `jailbee start` of an existing container fails with
    "Missing source path" and the in-flight work in it is stranded.
    """
    from jailbee.migrate import DirMove, MigrationPlan, execute_plan

    src = xdg / "data" / "gie"
    (src / "shared" / "app" / "caches").mkdir(parents=True)
    mocker.patch("jailbee.init_command.install_systemd_units")
    mocker.patch("subprocess.run")

    execute_plan(
        MigrationPlan(
            dir_moves=(DirMove(src=src, dst=xdg / "data" / "jailbee", compat_symlink=True),)
        ),
        _incus(mocker),
    )

    assert src.is_symlink()
    assert src.resolve() == (xdg / "data" / "jailbee").resolve()
    assert (src / "shared" / "app" / "caches").is_dir()


def test_execute_plan_refuses_when_the_target_dir_appeared_after_planning(xdg, mocker, on_path):
    """The plan-time blocker is not enough — the window is 60 seconds wide.

    The pre-1.0 `gie-net-refresh.timer` has `OnUnitActiveSec=60s` and, after
    the upgrade, its ExecStart runs the *new* code, which creates
    `<state>/jailbee` the moment it opens the database. So the target can
    appear while `Apply this migration?` is waiting for an answer — after
    `build_plan` has already passed.

    `shutil.move` into an existing directory nests instead of failing, which
    is why this asserts the source is untouched and nothing was nested, not
    merely that something was raised.
    """
    from jailbee.migrate import IncompleteMigrationError, build_plan, execute_plan

    src = xdg / "state" / "gie"
    src.mkdir()
    (src / "state.sqlite").write_text("the real database\n")
    incus = _incus(mocker)
    mocker.patch("jailbee.init_command.install_systemd_units")
    mocker.patch("subprocess.run")

    plan = build_plan(incus)
    assert [m.src for m in plan.dir_moves] == [src]

    # ... the timer fires while the user is still reading the plan.
    dst = xdg / "state" / "jailbee"
    dst.mkdir()
    (dst / "state.sqlite").write_text("")

    with pytest.raises(IncompleteMigrationError, match="appeared after"):
        execute_plan(plan, incus)

    assert (src / "state.sqlite").read_text() == "the real database\n"
    assert not (dst / "gie").exists(), "shutil.move nested the old directory inside the new one"


def test_execute_plan_does_not_symlink_dirs_that_were_not_marked(xdg, mocker, on_path):
    from jailbee.migrate import DirMove, MigrationPlan, execute_plan

    src = xdg / "config" / "gie"
    src.mkdir()
    mocker.patch("jailbee.init_command.install_systemd_units")
    mocker.patch("subprocess.run")

    execute_plan(
        MigrationPlan(dir_moves=(DirMove(src=src, dst=xdg / "config" / "jailbee"),)),
        _incus(mocker),
    )

    assert not src.exists()


def test_execute_plan_refuses_when_blocked(xdg, mocker):
    """A blocked plan must do nothing, even when it also carries real work.

    Populating dir_moves/relabels/bridge work alongside the blocker is
    deliberate: a plan with every other field defaulted to empty would pass
    this test even if the blockers guard were deleted or moved to the end
    of execute_plan, since there would be no work left to (not) do either
    way.
    """
    from jailbee.migrate import ContainerRelabel, DirMove, MigrationPlan, execute_plan

    src = xdg / "config" / "gie"
    src.mkdir()
    (src / "global.yaml").write_text("x: 1\n")
    incus = _incus(mocker)

    plan = MigrationPlan(
        dir_moves=(DirMove(src=src, dst=xdg / "config" / "jailbee"),),
        relabels=(ContainerRelabel(name="app-feat", keys=("user.gie.branch",), repo_dir=None),),
        migrate_bridge=True,
        blockers=("jobs pending",),
    )
    with pytest.raises(RuntimeError, match="blocked"):
        execute_plan(plan, incus)

    assert src.exists()
    assert not (xdg / "config" / "jailbee").exists()
    incus.config_set.assert_not_called()
    incus.network_delete.assert_not_called()


def test_execute_plan_renames_refs_via_update_ref(xdg, mocker, on_path):
    from jailbee.migrate import MigrationPlan, RefRename, execute_plan

    run = mocker.patch("subprocess.run")
    run.return_value = mocker.MagicMock(returncode=0, stderr="")
    mocker.patch("jailbee.init_command.install_systemd_units")
    plan = MigrationPlan(
        ref_renames=(
            RefRename(
                repo_dir=Path("/home/u/app"),
                old_ref="refs/gie/host/feat",
                new_ref="refs/jailbee/host/feat",
                oid="abc123",
            ),
        ),
    )
    execute_plan(plan, _incus(mocker))

    calls = [c.args[0] for c in run.call_args_list]
    assert ["git", "-C", "/home/u/app", "update-ref", "refs/jailbee/host/feat", "abc123"] in calls
    assert ["git", "-C", "/home/u/app", "update-ref", "-d", "refs/gie/host/feat"] in calls


def test_execute_plan_keeps_the_old_ref_when_the_new_one_cannot_be_created(xdg, mocker, on_path):
    """A failed `update-ref` create must not be followed by the delete.

    A D/F conflict against an existing `refs/jailbee/...`, a stale lock or a
    read-only repo all exit non-zero — deleting the old ref then destroys the
    only copy of the fetch-proof anchor.
    """
    from jailbee.migrate import MigrationPlan, RefRename, execute_plan

    run = mocker.patch("subprocess.run")
    run.return_value = mocker.MagicMock(returncode=1, stderr="cannot lock ref")
    mocker.patch("jailbee.init_command.install_systemd_units")

    execute_plan(
        MigrationPlan(
            ref_renames=(
                RefRename(
                    repo_dir=Path("/home/u/app"),
                    old_ref="refs/gie/host/feat",
                    new_ref="refs/jailbee/host/feat",
                    oid="abc123",
                ),
            ),
        ),
        _incus(mocker),
    )

    calls = [c.args[0] for c in run.call_args_list]
    assert ["git", "-C", "/home/u/app", "update-ref", "refs/jailbee/host/feat", "abc123"] in calls
    assert ["git", "-C", "/home/u/app", "update-ref", "-d", "refs/gie/host/feat"] not in calls


def test_execute_plan_renames_refs_before_unsetting_the_labels(xdg, mocker, on_path):
    """Order matters: the labels are where the repos were discovered.

    If the relabel ran first and the process died before the refs, a re-run
    would find no `user.gie.repo_dir` anywhere, plan zero ref renames, and
    leave `refs/gie/*` orphaned while reporting itself clean.
    """
    from jailbee.migrate import ContainerRelabel, MigrationPlan, RefRename, execute_plan

    run = mocker.patch("subprocess.run")
    run.return_value = mocker.MagicMock(returncode=0, stderr="")
    mocker.patch("jailbee.init_command.install_systemd_units")
    incus = _incus(mocker)
    incus.config_get.return_value = "/home/u/app"

    order = mocker.MagicMock()
    order.attach_mock(run, "git")
    order.attach_mock(incus.config_unset, "config_unset")

    execute_plan(
        MigrationPlan(
            relabels=(
                ContainerRelabel(
                    name="app-feat", keys=("user.gie.repo_dir",), repo_dir="/home/u/app"
                ),
            ),
            ref_renames=(
                RefRename(
                    repo_dir=Path("/home/u/app"),
                    old_ref="refs/gie/host/feat",
                    new_ref="refs/jailbee/host/feat",
                    oid="abc123",
                ),
            ),
        ),
        incus,
    )

    names = [name for name, _, _ in order.mock_calls if name in {"git", "config_unset"}]
    assert names.index("git") < names.index("config_unset")


def test_execute_plan_deletes_old_skill_paths_planned_under_the_pre_move_root(xdg, mocker, on_path):
    """Skill paths are resolved before the moves, so they must be remapped.

    On a first run `_old_skill_paths()` finds them under `<data>/gie/shared`,
    which `execute_plan` then moves out from under itself — leaving the stale
    `gie-usage`/`gie-repo-setup` skills alive under the new root while
    reporting "Migration complete".

    `compat_symlink=False` on purpose: with the symlink in place the old
    paths keep resolving and the deletion appears to work whether or not the
    remap exists. The remap is what must do the work, because the symlink is
    compatibility surface that 2.0.0 removes.
    """
    from jailbee.migrate import DirMove, MigrationPlan, execute_plan

    claude = xdg / "data" / "gie" / "shared" / "app" / "claude"
    skills = claude / "skills" / "gie-usage"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("old\n")
    lock = claude / ".gie-skills.lock"
    lock.write_text("")
    mocker.patch("jailbee.init_command.install_systemd_units")
    mocker.patch("subprocess.run")

    execute_plan(
        MigrationPlan(
            dir_moves=(
                DirMove(
                    src=xdg / "data" / "gie",
                    dst=xdg / "data" / "jailbee",
                    compat_symlink=False,
                ),
            ),
            old_skill_paths=(skills, lock),
        ),
        _incus(mocker),
    )

    moved_claude = xdg / "data" / "jailbee" / "shared" / "app" / "claude"
    assert not (moved_claude / "skills" / "gie-usage").exists()
    assert not (moved_claude / ".gie-skills.lock").exists()


def test_execute_plan_deletes_old_skill_paths_already_under_the_new_root(xdg, mocker, on_path):
    from jailbee.migrate import MigrationPlan, execute_plan

    skills = xdg / "data" / "jailbee" / "shared" / "app" / "claude" / "skills" / "gie-usage"
    skills.mkdir(parents=True)
    lock = skills.parent.parent / ".gie-skills.lock"
    lock.write_text("")
    mocker.patch("jailbee.init_command.install_systemd_units")
    mocker.patch("subprocess.run")

    execute_plan(MigrationPlan(old_skill_paths=(skills, lock)), _incus(mocker))

    assert not skills.exists()
    assert not lock.exists()


def test_execute_plan_replaces_old_units(xdg, mocker, on_path):
    from jailbee.migrate import MigrationPlan, execute_plan

    units_dir = xdg / ".config" / "systemd" / "user"
    units_dir.mkdir(parents=True)
    (units_dir / "gie-net-refresh.timer").write_text("")
    (units_dir / "gie-net-refresh.service").write_text("")
    run = mocker.patch("subprocess.run")
    install = mocker.patch("jailbee.init_command.install_systemd_units")

    execute_plan(
        MigrationPlan(old_units=("gie-net-refresh.timer", "gie-net-refresh.service")),
        _incus(mocker),
    )

    calls = [c.args[0] for c in run.call_args_list]
    assert ["systemctl", "--user", "disable", "--now", "gie-net-refresh.timer"] in calls
    assert ["systemctl", "--user", "disable", "--now", "gie-net-refresh.service"] in calls
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert not (units_dir / "gie-net-refresh.timer").exists()
    assert not (units_dir / "gie-net-refresh.service").exists()
    install.assert_called_once_with()


def test_execute_plan_replaces_the_units_before_touching_the_bridge(xdg, mocker, on_path):
    """The bridge is deliberately last, because it can end in a report.

    `_migrate_bridge` raises `IncompleteMigrationError` when something still
    holds `gie-loose`. If unit replacement ran after it, that report would
    strand the machine on the pre-1.0 refresh timer — silently stopping
    egress-pool refreshes and TTL-driven loose reverts — for a reason that
    has nothing to do with systemd.
    """
    from jailbee.migrate import LooseRepoint, MigrationPlan, execute_plan

    units_dir = xdg / ".config" / "systemd" / "user"
    units_dir.mkdir(parents=True)
    (units_dir / "gie-net-refresh.timer").write_text("")
    mocker.patch("subprocess.run")
    install = mocker.patch("jailbee.init_command.install_systemd_units")
    ensure = mocker.patch("jailbee.init_command.ensure_loose_bridge")
    incus = _incus(mocker)

    order = mocker.MagicMock()
    order.attach_mock(install, "install_units")
    order.attach_mock(ensure, "ensure_bridge")
    order.attach_mock(incus.profile_set_yaml, "repoint")
    order.attach_mock(incus.network_delete, "network_delete")

    execute_plan(
        MigrationPlan(
            old_units=("gie-net-refresh.timer",),
            migrate_bridge=True,
            loose_repoints=(LooseRepoint(profile="app-net-loose", prefix="app"),),
        ),
        incus,
    )

    names = [name for name, _, _ in order.mock_calls]
    assert names.index("install_units") < names.index("ensure_bridge")
    assert names.index("install_units") < names.index("repoint")
    assert names.index("install_units") < names.index("network_delete")


def test_execute_plan_refuses_to_unlink_units_when_jailbee_is_not_on_path(xdg, mocker):
    """`install_systemd_units` only warns when the binary is missing.

    Unlinking first would leave the machine with no refresh timer at all —
    egress-pool refreshes and TTL-driven loose reverts stop silently.
    """
    from jailbee.migrate import IncompleteMigrationError, MigrationPlan, execute_plan

    units_dir = xdg / ".config" / "systemd" / "user"
    units_dir.mkdir(parents=True)
    (units_dir / "gie-net-refresh.timer").write_text("")
    mocker.patch("jailbee.migrate.shutil.which", return_value=None)
    run = mocker.patch("subprocess.run")
    install = mocker.patch("jailbee.init_command.install_systemd_units")

    with pytest.raises(IncompleteMigrationError, match="not on PATH"):
        execute_plan(MigrationPlan(old_units=("gie-net-refresh.timer",)), _incus(mocker))

    assert (units_dir / "gie-net-refresh.timer").exists()
    install.assert_not_called()
    run.assert_not_called()


def test_execute_plan_deletes_mirror_container_and_profile(xdg, mocker, on_path):
    from jailbee.migrate import MigrationPlan, execute_plan

    incus = _incus(mocker)
    mocker.patch("jailbee.init_command.install_systemd_units")
    mocker.patch("subprocess.run")

    execute_plan(
        MigrationPlan(delete_mirror_container=True, delete_mirror_profile=True),
        incus,
    )

    incus.delete.assert_called_once_with("gie-registry-mirror", force=True)
    incus.profile_delete.assert_called_once_with("gie-registry-mirror-profile")


def test_execute_plan_skips_a_relabel_key_whose_value_is_none(xdg, mocker, on_path):
    """A key that reads back `None` must not be written as an empty string.

    `incus.config_get` returns `None` for the first key and a real value
    for the second; only the second key may be written.
    """
    from jailbee.migrate import ContainerRelabel, MigrationPlan, execute_plan

    incus = _incus(mocker)
    incus.config_get.side_effect = lambda name, key: None if key == "user.gie.repo_dir" else "feat"
    mocker.patch("jailbee.init_command.install_systemd_units")
    mocker.patch("subprocess.run")

    plan = MigrationPlan(
        relabels=(
            ContainerRelabel(
                name="app-feat",
                keys=("user.gie.repo_dir", "user.gie.branch"),
                repo_dir="/home/u/app",
            ),
        ),
    )
    execute_plan(plan, incus)

    incus.config_set.assert_called_once_with("app-feat", "user.jailbee.branch", "feat")
    incus.config_unset.assert_called_once_with("app-feat", "user.gie.branch")
    for call in incus.config_set.call_args_list:
        assert call.args[1] != "user.jailbee.repo_dir"


# ---- the loose bridge (create, repoint, delete) ----------------------------


def test_execute_plan_creates_the_bridge_repoints_profiles_then_deletes_the_old(
    xdg, mocker, on_path
):
    """`gie-loose` cannot be renamed — every loose profile pins it.

    Renaming exits non-zero and, before this fix, aborted the whole migration
    with a traceback at exactly the same point on every re-run.
    """
    from jailbee.migrate import LooseRepoint, MigrationPlan, execute_plan
    from jailbee.profiles import loose_net_profile_yaml

    incus = _incus(mocker)
    ensure = mocker.patch("jailbee.init_command.ensure_loose_bridge")
    mocker.patch("jailbee.init_command.install_systemd_units")
    mocker.patch("subprocess.run")

    execute_plan(
        MigrationPlan(
            migrate_bridge=True,
            loose_repoints=(LooseRepoint(profile="app-net-loose", prefix="app"),),
            delete_mirror_container=True,
            delete_mirror_profile=True,
        ),
        incus,
    )

    ensure.assert_called_once_with(incus)
    incus.profile_set_yaml.assert_called_once_with("app-net-loose", loose_net_profile_yaml("app"))
    incus.network_delete.assert_called_once_with("gie-loose")
    incus.network_rename.assert_not_called()

    # Ordering: the mirror profile is one of the old bridge's users, and the
    # repoint has to land before the delete or the delete fails.
    names = [c[0] for c in incus.mock_calls]
    assert names.index("profile_delete") < names.index("network_delete")
    assert names.index("profile_set_yaml") < names.index("network_delete")


def test_execute_plan_reports_remaining_users_instead_of_deleting_the_bridge(xdg, mocker, on_path):
    """Anything still holding `gie-loose` is named, not turned into a traceback."""
    from jailbee.migrate import IncompleteMigrationError, LooseRepoint, MigrationPlan, execute_plan

    incus = _incus(mocker, networks=("gie-loose",), used_by=("/1.0/instances/legacy-box",))
    mocker.patch("jailbee.init_command.ensure_loose_bridge")
    mocker.patch("jailbee.init_command.install_systemd_units")
    mocker.patch("subprocess.run")

    with pytest.raises(IncompleteMigrationError, match="legacy-box"):
        execute_plan(
            MigrationPlan(
                migrate_bridge=True,
                loose_repoints=(LooseRepoint(profile="app-net-loose", prefix="app"),),
            ),
            incus,
        )

    incus.profile_set_yaml.assert_called_once()
    incus.network_delete.assert_not_called()


def test_execute_plan_turns_an_incus_failure_on_the_bridge_into_a_report(xdg, mocker, on_path):
    """The bridge is the last step, so an IncusError there means the rest is done.

    Letting it escape gives the user a raw traceback from a Typer command with
    no global handler, hiding that the migration otherwise succeeded.
    """
    from jailbee.incus import IncusError
    from jailbee.migrate import IncompleteMigrationError, LooseRepoint, MigrationPlan, execute_plan

    incus = _incus(mocker)
    incus.profile_set_yaml.side_effect = IncusError("`incus profile edit` failed: nope")
    mocker.patch("jailbee.init_command.ensure_loose_bridge")
    mocker.patch("jailbee.init_command.install_systemd_units")
    mocker.patch("subprocess.run")

    with pytest.raises(IncompleteMigrationError, match="nope"):
        execute_plan(
            MigrationPlan(
                migrate_bridge=True,
                loose_repoints=(LooseRepoint(profile="app-net-loose", prefix="app"),),
            ),
            incus,
        )

    incus.network_delete.assert_not_called()


# ---- leftovers() (what `doctor` reports) -----------------------------------


def test_leftovers_reports_the_old_directory_beside_an_existing_target(xdg, mocker):
    """Doctor must surface `<state>/gie` even when the target already exists.

    `leftovers` reads the filesystem rather than the plan, so it stays
    correct however the plan chooses to handle the collision.
    """
    from jailbee.migrate import leftovers

    (xdg / "state" / "gie").mkdir()
    (xdg / "state" / "jailbee").mkdir()

    assert leftovers(_incus(mocker)) == (f"directory {xdg / 'state' / 'gie'}",)


def test_leftovers_reports_an_orphaned_old_bridge(xdg, mocker):
    """`jailbee-loose` already existing used to make the plan skip the rename.

    The old bridge then survived as a stray managed network — its own subnet,
    dnsmasq and firewall rules — while the plan reported itself empty.
    """
    from jailbee.migrate import leftovers

    incus = _incus(mocker, networks=("gie-loose", "jailbee-loose"))

    assert "network gie-loose" in leftovers(incus)


def test_leftovers_reports_labels_units_mirror_and_skills(xdg, mocker):
    from jailbee.migrate import leftovers

    units_dir = xdg / ".config" / "systemd" / "user"
    units_dir.mkdir(parents=True)
    (units_dir / "gie-net-refresh.timer").write_text("")
    skills = xdg / "data" / "jailbee" / "shared" / "app" / "claude" / "skills" / "gie-repo-setup"
    skills.mkdir(parents=True)
    incus = _incus(
        mocker,
        containers=[_container("app-feat", {"user.gie.branch": "feat"})],
        instances=("gie-registry-mirror",),
        profiles=("gie-registry-mirror-profile",),
    )

    found = leftovers(incus)

    assert "old labels on container app-feat" in found
    assert "container gie-registry-mirror" in found
    assert "profile gie-registry-mirror-profile" in found
    assert "systemd unit gie-net-refresh.timer" in found
    assert f"stale skill path {skills}" in found


def test_leftovers_reports_old_refs_in_a_repo_with_no_old_labels(xdg, mocker):
    """Post-relabel, the refs are the only pre-1.0 state left in a repo."""
    from jailbee.migrate import leftovers

    run = mocker.patch("subprocess.run")
    run.return_value = mocker.MagicMock(
        returncode=0, stdout="refs/gie/host/feat abc123\nrefs/gie-sub/x def456\n"
    )
    incus = _incus(
        mocker,
        containers=[_container("app-feat", {"user.jailbee.repo_dir": "/home/u/app"})],
    )

    assert "2 pre-1.0 git ref(s) in /home/u/app" in leftovers(incus)


def test_leftovers_is_empty_on_a_clean_host(xdg, mocker):
    from jailbee.migrate import leftovers

    assert leftovers(_incus(mocker)) == ()


# ---- destination conflicts: clear the empty, ask before the populated ----


def test_execute_clears_an_empty_target_and_completes_the_move(xdg, mocker, on_path):
    """The end-to-end shape of the `make install` case: new code created the
    destination, the migrator clears it and the old state lands there."""
    from jailbee.migrate import build_plan, execute_plan

    (xdg / "state" / "gie").mkdir()
    (xdg / "state" / "gie" / "state.sqlite").write_text("old state")
    (xdg / "state" / "jailbee").mkdir()

    plan = build_plan(_incus(mocker))
    execute_plan(plan, _incus(mocker))

    assert not (xdg / "state" / "gie").exists()
    assert (xdg / "state" / "jailbee" / "state.sqlite").read_text() == "old state"


def test_execute_refuses_a_populated_target_without_approval(xdg, mocker, on_path):
    """The guard that makes `approved_removals` meaningful.

    Without it the parameter would be advisory: a caller that forgot to ask
    would silently delete the user's state, which is the one outcome the
    whole consent path exists to prevent.
    """
    import pytest

    from jailbee.migrate import IncompleteMigrationError, build_plan, execute_plan

    (xdg / "state" / "gie").mkdir()
    (xdg / "state" / "jailbee").mkdir()
    (xdg / "state" / "jailbee" / "something.db").write_text("real state")

    plan = build_plan(_incus(mocker))
    with pytest.raises(IncompleteMigrationError, match="not approved"):
        execute_plan(plan, _incus(mocker))

    # Nothing moved, and the state that was there is still there.
    assert (xdg / "state" / "gie").is_dir()
    assert (xdg / "state" / "jailbee" / "something.db").read_text() == "real state"


def test_execute_clears_a_populated_target_once_approved(xdg, mocker, on_path):
    from jailbee.migrate import build_plan, execute_plan

    (xdg / "state" / "gie").mkdir()
    (xdg / "state" / "gie" / "keep.txt").write_text("old state")
    (xdg / "state" / "jailbee").mkdir()
    (xdg / "state" / "jailbee" / "something.db").write_text("real state")

    plan = build_plan(_incus(mocker))
    execute_plan(plan, _incus(mocker), approved_removals=frozenset({xdg / "state" / "jailbee"}))

    assert (xdg / "state" / "jailbee" / "keep.txt").read_text() == "old state"
    assert not (xdg / "state" / "jailbee" / "something.db").exists()


def test_stop_refresh_timers_stops_both_namespaces(mocker):
    """`jailbee net install` (which `make install` runs) enables the *new*
    timer `--now`, so stopping only the pre-1.0 one leaves a ticking process
    that recreates the destination the migrator is about to move onto."""
    from jailbee.migrate import stop_refresh_timers

    run = mocker.patch("jailbee.migrate.subprocess.run")

    stop_refresh_timers()

    stopped = [call.args[0][-1] for call in run.call_args_list]
    assert stopped == ["gie-net-refresh.timer", "jailbee-net-refresh.timer"]
    assert all(call.kwargs["check"] is False for call in run.call_args_list)
