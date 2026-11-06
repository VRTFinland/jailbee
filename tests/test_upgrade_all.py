"""`jailbee upgrade`: base build + apply across every registered repo."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pytest_mock import MockerFixture
from sqlmodel import Session

from jailbee.apply import ApplyResult
from jailbee.config import Config
from jailbee.db import get_engine
from jailbee.global_config import GlobalConfig
from jailbee.incus import Incus, IncusError
from jailbee.upgrade import UpgradeNote, load_or_backfill, record
from jailbee.upgrade_all import plan_repos, run_upgrade_all

NOW = datetime(2026, 10, 3, tzinfo=UTC)
VERSION = "2.0.0"


def _cfg(make_cfg, tmp_path: Path, prefix: str, alias: str | None = None) -> Config:
    cfg = make_cfg(tmp_path / prefix)
    object.__setattr__(cfg.golden, "alias", alias or f"{prefix}-base")
    return cfg


def _result(**overrides: object) -> ApplyResult:
    defaults: dict[str, object] = dict(
        profiles_changed=[],
        profiles_unchanged=[],
        acl_changed=False,
        hosts_repinned=[],
        docker_restarted=[],
        docker_restart_pending=[],
        restarted=[],
        restart_failures=[],
    )
    defaults.update(overrides)
    return ApplyResult(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def fakes(mocker: MockerFixture):
    """Patch the two long operations; the loader is patched per test."""
    build = mocker.patch("jailbee.golden.build_golden_image")
    apply = mocker.patch("jailbee.apply.run_apply", return_value=_result())
    return build, apply


def _load(mocker: MockerFixture, cfgs: dict[Path, Config]) -> None:
    mocker.patch("jailbee.config.load_repo_config", side_effect=lambda root: cfgs[root])


def _observe(prefix: str, version: str = VERSION) -> None:
    """Mark both actions as run at `version` — nothing owed."""
    with Session(get_engine()) as session:
        record(session, prefix, "base_build", version, now=NOW)
        record(session, prefix, "apply", version, now=NOW)


def _notes(mocker: MockerFixture, *actions: str) -> None:
    mocker.patch(
        "jailbee.upgrade.UPGRADE_NOTES",
        (UpgradeNote(version=(2, 0, 0), actions=frozenset(actions), reason="r"),),  # type: ignore[arg-type]
    )


def _run(roots, *, force=False, dry_run=False, docker_seed=True):
    return run_upgrade_all(
        roots,
        Incus(),
        GlobalConfig(),
        force=force,
        dry_run=dry_run,
        version=VERSION,
        now=NOW,
        docker_seed=docker_seed,
    )


def test_upgrade_seeds_only_a_built_alias_after_apply(make_cfg, tmp_path, mocker, fakes) -> None:
    build, apply = fakes
    a = _cfg(make_cfg, tmp_path, "aa")
    b = _cfg(make_cfg, tmp_path, "bb", alias=a.golden.alias)
    for cfg in (a, b):
        object.__setattr__(cfg.golden, "docker_seed", type("SeedConfig", (), {"enabled": True})())
    _load(mocker, {a.repo_root: a, b.repo_root: b})
    _observe("aa", "1.0.0")
    _observe("bb", "1.0.0")
    _notes(mocker, "base_build", "apply")
    preflight = mocker.patch("jailbee.docker_seed.preflight")
    run_seed = mocker.patch(
        "jailbee.docker_seed.run_seed",
        create=True,
        return_value=type("Outcome", (), {"outcome": "seeded", "detail": ""})(),
    )

    results = _run([a.repo_root, b.repo_root])

    assert [r.build for r in results] == ["built", "shared"]
    assert [r.seed for r in results] == ["seeded", "-"]
    assert build.call_count == 1
    assert apply.call_count == 2
    preflight.assert_called_once()
    run_seed.assert_called_once()


def test_seed_preflight_failure_records_skipped_without_blocking_repo(
    make_cfg, tmp_path, mocker, fakes
) -> None:
    from jailbee.docker_seed import SeedPreflightError

    a = _cfg(make_cfg, tmp_path, "aa")
    object.__setattr__(a.golden, "docker_seed", type("SeedConfig", (), {"enabled": True})())
    _load(mocker, {a.repo_root: a})
    preflight = mocker.patch(
        "jailbee.docker_seed.preflight", side_effect=SeedPreflightError("no docker")
    )
    record_outcome = mocker.patch("jailbee.docker_seed.record_outcome", create=True)
    run_seed = mocker.patch("jailbee.docker_seed.run_seed", create=True)
    warn = mocker.patch("jailbee.tui.warn")

    (result,) = _run([a.repo_root], force=True)

    assert result.seed == "skipped"
    assert result.ok
    preflight.assert_called_once()
    record_outcome.assert_called_once()
    run_seed.assert_not_called()
    warn.assert_called_once()


def test_force_builds_and_applies_every_repo(make_cfg, tmp_path, mocker, fakes) -> None:
    build, apply = fakes
    a, b = _cfg(make_cfg, tmp_path, "aa"), _cfg(make_cfg, tmp_path, "bb")
    _load(mocker, {a.repo_root: a, b.repo_root: b})
    _observe("aa")
    _observe("bb")

    results = _run([a.repo_root, b.repo_root], force=True)

    assert build.call_count == 2
    assert apply.call_count == 2
    assert all(r.ok for r in results)


def test_seed_skips_repo_when_build_blocks_apply(make_cfg, tmp_path, mocker, fakes) -> None:
    build, _ = fakes
    build.side_effect = IncusError("build failed")
    a = _cfg(make_cfg, tmp_path, "aa")
    object.__setattr__(a.golden, "docker_seed", type("SeedConfig", (), {"enabled": True})())
    _load(mocker, {a.repo_root: a})
    preflight = mocker.patch("jailbee.docker_seed.preflight")
    run_seed = mocker.patch("jailbee.docker_seed.run_seed", create=True)

    (result,) = _run([a.repo_root], force=True)

    assert result.build == "failed"
    assert result.apply == "blocked"
    assert result.seed == "-"
    preflight.assert_not_called()
    run_seed.assert_not_called()


def test_seed_failure_does_not_stop_sweep(make_cfg, tmp_path, mocker, fakes) -> None:
    a = _cfg(make_cfg, tmp_path, "aa")
    b = _cfg(make_cfg, tmp_path, "bb")
    for cfg in (a, b):
        object.__setattr__(cfg.golden, "docker_seed", type("SeedConfig", (), {"enabled": True})())
    _load(mocker, {a.repo_root: a, b.repo_root: b})
    mocker.patch("jailbee.docker_seed.preflight")
    run_seed = mocker.patch(
        "jailbee.docker_seed.run_seed",
        create=True,
        side_effect=[
            type("Outcome", (), {"outcome": "failed", "detail": "load failed"})(),
            type("Outcome", (), {"outcome": "seeded", "detail": ""})(),
        ],
    )

    results = _run([a.repo_root, b.repo_root], force=True)

    assert [r.seed for r in results] == ["failed", "seeded"]
    assert [r.ok for r in results] == [True, True]
    assert run_seed.call_count == 2


@pytest.mark.parametrize("stage", ["preflight", "seed"])
def test_seed_exception_does_not_stop_sweep(make_cfg, tmp_path, mocker, fakes, stage) -> None:
    a = _cfg(make_cfg, tmp_path, "aa")
    b = _cfg(make_cfg, tmp_path, "bb")
    for cfg in (a, b):
        object.__setattr__(cfg.golden, "docker_seed", type("SeedConfig", (), {"enabled": True})())
    _load(mocker, {a.repo_root: a, b.repo_root: b})
    mocker.patch("jailbee.docker_seed.preflight", side_effect=[OSError("seed broke"), None] if stage == "preflight" else None)
    seeded = type("Outcome", (), {"outcome": "seeded", "detail": ""})()
    mocker.patch("jailbee.docker_seed.run_seed", side_effect=[OSError("seed broke"), seeded] if stage == "seed" else [seeded])
    record = mocker.patch("jailbee.docker_seed.record_outcome")
    warn = mocker.patch("jailbee.tui.warn")

    results = _run([a.repo_root, b.repo_root], force=True)

    assert [r.seed for r in results] == ["failed", "seeded"]
    assert all(r.ok for r in results)
    assert record.call_args.args[1].outcome == "failed"
    assert "seed broke" in record.call_args.args[1].detail
    assert "seed broke" in warn.call_args.args[0]


@pytest.mark.parametrize("stage", ["preflight", "seed"])
def test_seed_keyboard_interrupt_propagates(make_cfg, tmp_path, mocker, fakes, stage) -> None:
    a = _cfg(make_cfg, tmp_path, "aa")
    object.__setattr__(a.golden, "docker_seed", type("SeedConfig", (), {"enabled": True})())
    _load(mocker, {a.repo_root: a})
    mocker.patch("jailbee.docker_seed.preflight", side_effect=KeyboardInterrupt if stage == "preflight" else None)
    mocker.patch("jailbee.docker_seed.run_seed", side_effect=KeyboardInterrupt if stage == "seed" else None)
    with pytest.raises(KeyboardInterrupt):
        _run([a.repo_root], force=True)


def test_apply_never_restarts(make_cfg, tmp_path, mocker, fakes) -> None:
    _, apply = fakes
    a = _cfg(make_cfg, tmp_path, "aa")
    _load(mocker, {a.repo_root: a})

    _run([a.repo_root], force=True)

    kwargs = apply.call_args.kwargs
    assert kwargs["no_restart"] is True
    assert kwargs["assume_yes"] is True


def test_default_runs_only_what_the_notes_owe(make_cfg, tmp_path, mocker, fakes) -> None:
    build, apply = fakes
    a = _cfg(make_cfg, tmp_path, "aa")
    _load(mocker, {a.repo_root: a})
    _observe("aa", "1.0.0")
    _notes(mocker, "apply")

    results = _run([a.repo_root])

    assert build.call_count == 0
    assert apply.call_count == 1
    assert results[0].build == "skipped"
    assert results[0].apply == "applied"


def test_docker_seed_false_skips_seed(make_cfg, tmp_path, mocker, fakes) -> None:
    a = _cfg(make_cfg, tmp_path, "aa")
    object.__setattr__(a.golden, "docker_seed", type("SeedConfig", (), {"enabled": True})())
    _load(mocker, {a.repo_root: a})
    preflight = mocker.patch("jailbee.docker_seed.preflight")
    run_seed = mocker.patch("jailbee.docker_seed.run_seed", create=True)

    (result,) = _run([a.repo_root], force=True, docker_seed=False)

    assert result.seed == "-"
    preflight.assert_not_called()
    run_seed.assert_not_called()


def test_default_skips_a_repo_with_nothing_owed(make_cfg, tmp_path, mocker, fakes) -> None:
    build, apply = fakes
    a = _cfg(make_cfg, tmp_path, "aa")
    _load(mocker, {a.repo_root: a})
    _observe("aa")
    _notes(mocker, "apply", "base_build")

    results = _run([a.repo_root])

    assert build.call_count == 0
    assert apply.call_count == 0
    assert results[0].ok


def test_dismissed_advice_is_still_owed(make_cfg, tmp_path, mocker, fakes) -> None:
    """`jb dismiss` silences the hint, never the upgrade the user asked for."""
    from jailbee import notices
    from jailbee.upgrade import ACTION_KEYS

    _, apply = fakes
    a = _cfg(make_cfg, tmp_path, "aa")
    _load(mocker, {a.repo_root: a})
    _observe("aa", "1.0.0")
    _notes(mocker, "apply")
    with Session(get_engine()) as session:
        notices.save(
            session, ACTION_KEYS["apply"], "aa", fingerprint=VERSION, version=VERSION, now=NOW
        )

    _run([a.repo_root])

    assert apply.call_count == 1


def test_success_is_recorded_so_the_hint_goes_quiet(make_cfg, tmp_path, mocker, fakes) -> None:
    a = _cfg(make_cfg, tmp_path, "aa")
    _load(mocker, {a.repo_root: a})
    _observe("aa", "1.0.0")
    _notes(mocker, "apply", "base_build")

    _run([a.repo_root])

    with Session(get_engine()) as session:
        marks = load_or_backfill(session, "aa", VERSION, now=NOW)
    assert marks["base_build"].version == (2, 0, 0)
    assert marks["base_build"].observed
    assert marks["apply"].version == (2, 0, 0)
    assert marks["apply"].observed


def test_shared_alias_is_built_once_and_recorded_for_each_repo(
    make_cfg, tmp_path, mocker, fakes
) -> None:
    build, apply = fakes
    a = _cfg(make_cfg, tmp_path, "aa", alias="scratch-base")
    b = _cfg(make_cfg, tmp_path, "bb", alias="scratch-base")
    _load(mocker, {a.repo_root: a, b.repo_root: b})
    _observe("aa", "1.0.0")
    _observe("bb", "1.0.0")
    _notes(mocker, "base_build")

    _run([a.repo_root, b.repo_root])

    assert build.call_count == 1
    assert apply.call_count == 0
    with Session(get_engine()) as session:
        for prefix in ("aa", "bb"):
            mark = load_or_backfill(session, prefix, VERSION, now=NOW)["base_build"]
            assert mark.version == (2, 0, 0)
            assert mark.observed


def test_failed_build_skips_that_repos_apply_and_continues(
    make_cfg, tmp_path, mocker, fakes
) -> None:
    build, apply = fakes
    build.side_effect = [IncusError("apt exploded"), None]
    a, b = _cfg(make_cfg, tmp_path, "aa"), _cfg(make_cfg, tmp_path, "bb")
    _load(mocker, {a.repo_root: a, b.repo_root: b})

    results = _run([a.repo_root, b.repo_root], force=True)

    assert [r.build for r in results] == ["failed", "built"]
    assert results[0].apply == "blocked"
    assert "apt exploded" in (results[0].error or "")
    assert results[1].apply == "applied"
    assert apply.call_count == 1
    assert [r.ok for r in results] == [False, True]
    with Session(get_engine()) as session:
        # The failed build must not silence the advice.
        assert not load_or_backfill(session, "aa", VERSION, now=NOW)["base_build"].observed


def test_failed_apply_is_reported_and_does_not_stop_the_sweep(
    make_cfg, tmp_path, mocker, fakes
) -> None:
    _, apply = fakes
    apply.side_effect = [IncusError("acl broke"), _result()]
    a, b = _cfg(make_cfg, tmp_path, "aa"), _cfg(make_cfg, tmp_path, "bb")
    _load(mocker, {a.repo_root: a, b.repo_root: b})

    results = _run([a.repo_root, b.repo_root], force=True)

    assert [r.apply for r in results] == ["failed", "applied"]
    assert "acl broke" in (results[0].error or "")


def test_unloadable_config_is_reported_and_skipped(make_cfg, tmp_path, mocker, fakes) -> None:
    from jailbee.config import ConfigError

    build, _ = fakes
    good = _cfg(make_cfg, tmp_path, "bb")
    bad = tmp_path / "broken"

    def load(root: Path) -> Config:
        if root == bad:
            raise ConfigError("bad yaml")
        return good

    mocker.patch("jailbee.config.load_repo_config", side_effect=load)

    results = _run([bad, good.repo_root], force=True)

    assert results[0].error == "bad yaml"
    assert not results[0].ok
    assert results[1].ok
    assert build.call_count == 1


def test_dry_run_plans_but_runs_nothing(make_cfg, tmp_path, mocker, fakes, capsys) -> None:
    build, apply = fakes
    a = _cfg(make_cfg, tmp_path, "aa")
    _load(mocker, {a.repo_root: a})
    _observe("aa", "1.0.0")
    _notes(mocker, "apply")

    results = _run([a.repo_root], dry_run=True)

    assert build.call_count == 0
    assert apply.call_count == 0
    assert results[0].apply == "planned"
    assert results[0].build == "skipped"
    with Session(get_engine()) as session:
        assert load_or_backfill(session, "aa", VERSION, now=NOW)["apply"].version == (1, 0, 0)


def test_plan_carries_the_reasons(make_cfg, tmp_path, mocker) -> None:
    a = _cfg(make_cfg, tmp_path, "aa")
    _load(mocker, {a.repo_root: a})
    _observe("aa", "1.0.0")
    _notes(mocker, "apply")

    (plan,) = plan_repos([a.repo_root], force=False, version=VERSION, now=NOW)

    assert plan.apply and not plan.build
    assert plan.reasons == ["r"]
