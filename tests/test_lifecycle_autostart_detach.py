"""`new_container` hands the detached stages to its caller's callback.

The planner/executor split itself lives in test_autostart_plan.py and
test_autostart_stages.py; this file covers the *wiring*: what
`run_autostart` reports back, what `new_container` does with it, and that
the `--wait` / `--no-wait` override survives the `--background` job file.
"""

from __future__ import annotations

import pytest

from jailbee.autostart_plan import AutostartPlan, AutostartTrigger


def _cfg_with(make_cfg, tmp_path, block: dict):
    return make_cfg(tmp_path, autostart=block)


# ---- run_autostart reports the deferred half


def test_run_autostart_returns_the_detached_stages(tmp_path, make_cfg, mocker):
    """`run_autostart` runs the blocking half and reports the rest; the
    caller decides who runs it. Nothing detaches without `detach: true`."""
    from jailbee import autostart

    ran: list[str] = []
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch(
        "jailbee.autostart.run_stages",
        side_effect=lambda c, i, n, stages, r, **kw: ran.extend(s.stage for s in stages),
    )
    incus = mocker.MagicMock()
    cfg = _cfg_with(
        make_cfg,
        tmp_path,
        {
            "on_start": [
                {"stage": "schema", "steps": [{"name": "m", "run": "true"}]},
                {"stage": "deps", "detach": True, "steps": [{"name": "d", "run": "true"}]},
            ]
        },
    )

    plan = autostart.run_autostart(cfg, incus, "c1", AutostartTrigger.ON_START, repo_dir="/r")

    assert ran == ["schema"]
    assert [s.stage for s in plan.detached] == ["deps"]


def test_run_autostart_keeps_the_flag_set_when_stages_remain(tmp_path, make_cfg, mocker):
    """The supervisor re-stamps the flag with its own pid; clearing it here
    would open a window in which loose_revert could flip the container."""
    from jailbee import autostart

    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.autostart.run_stages")
    incus = mocker.MagicMock()
    cfg = _cfg_with(
        make_cfg,
        tmp_path,
        {"on_start": [{"stage": "deps", "detach": True, "steps": [{"name": "d", "run": "true"}]}]},
    )

    autostart.run_autostart(cfg, incus, "c1", AutostartTrigger.ON_START, repo_dir="/r")

    assert not any(
        c.args[1] == "user.jailbee.autostart_in_progress"
        for c in incus.config_unset.call_args_list
    )


def test_run_autostart_clears_the_flag_when_nothing_detaches(tmp_path, make_cfg, mocker):
    from jailbee import autostart

    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.autostart.run_stages")
    incus = mocker.MagicMock()
    cfg = _cfg_with(make_cfg, tmp_path, {"on_start": [{"name": "a", "run": "true"}]})

    autostart.run_autostart(cfg, incus, "c1", AutostartTrigger.ON_START, repo_dir="/r")

    incus.config_unset.assert_any_call("c1", "user.jailbee.autostart_in_progress")


def test_a_failing_blocking_stage_clears_the_flag_even_with_stages_pending(
    tmp_path, make_cfg, mocker
):
    """Stages pending is not the same as stages handed off.

    When a blocking stage raises, the exception propagates past every
    caller's `on_detach` (`lifecycle.new_container`,
    `cli._post_start_actions`) and no supervisor is ever spawned — so
    nobody re-stamps the flag with a pid, and nobody clears it. The
    literal "1" left behind is read by `loose_revert._autostart_holds` as
    "held" unconditionally, exempting the container from TTL auto-revert
    for good.
    """
    from jailbee import autostart

    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.autostart.run_stages", side_effect=RuntimeError("boom"))
    incus = mocker.MagicMock()
    cfg = _cfg_with(
        make_cfg,
        tmp_path,
        {
            "on_start": [
                {"stage": "schema", "steps": [{"name": "m", "run": "true"}]},
                {"stage": "deps", "detach": True, "steps": [{"name": "d", "run": "true"}]},
            ]
        },
    )

    with pytest.raises(RuntimeError):
        autostart.run_autostart(cfg, incus, "c1", AutostartTrigger.ON_START, repo_dir="/r")

    incus.config_unset.assert_any_call("c1", "user.jailbee.autostart_in_progress")


def test_override_wait_never_detaches(tmp_path, make_cfg, mocker):
    from jailbee import autostart

    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.autostart.run_stages")
    incus = mocker.MagicMock()
    cfg = _cfg_with(
        make_cfg,
        tmp_path,
        {"on_start": [{"stage": "deps", "detach": True, "steps": [{"name": "d", "run": "true"}]}]},
    )

    plan = autostart.run_autostart(
        cfg, incus, "c1", AutostartTrigger.ON_START, repo_dir="/r", override="wait"
    )
    assert plan.detached == []


# ---- new_container hands the deferred stages to its caller


def _opts(**overrides):
    from jailbee.lifecycle import NewContainerOptions

    base = dict(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="golden",
        clone=True,
        autostart=True,
    )
    base.update(overrides)
    return NewContainerOptions(**base)  # type: ignore[arg-type]


def _cfg_for_new(make_cfg, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return make_cfg(
        repo,
        shared_dir=tmp_path / "shared",
        new={"clone_from": "local", "autofetch": False},
    )


def _stage(name: str):
    from jailbee.config import AutostartStage

    return AutostartStage(stage=name, steps=[{"name": "s", "run": "true"}])  # type: ignore[arg-type]


def _patch_new_container_deps(mocker):
    """Silence everything `new_container` does before the autostart block."""
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=True)
    mocker.patch("jailbee.autostart.inject_github_token")


def test_new_container_hands_the_on_start_stages_to_on_detach(tmp_path, make_cfg, mocker):
    """The whole point of the wiring: `new_container` computes the deferred
    stages but does not run them — the CLI's callback does."""
    from jailbee.lifecycle import new_container

    _patch_new_container_deps(mocker)
    run_autostart = mocker.patch(
        "jailbee.autostart.run_autostart",
        side_effect=[
            AutostartPlan(blocking=[], detached=[]),
            AutostartPlan(blocking=[], detached=[_stage("deps")]),
        ],
    )
    incus = mocker.MagicMock()
    incus.exists.return_value = False
    seen: list[tuple[str, str]] = []

    new_container(
        _cfg_for_new(make_cfg, tmp_path),
        incus,
        _opts(),
        on_detach=lambda block, trigger, repo_dir: seen.append((trigger, repo_dir)),
    )

    assert run_autostart.call_count == 2
    assert [t for t, _ in seen] == ["on_start"]


def test_new_container_hands_off_from_on_create_without_running_on_start(
    tmp_path, make_cfg, mocker
):
    """A stage deferred in `on_create` moves the boundary: everything after
    it — including the whole `on_start` trigger — belongs to the supervisor,
    so the foreground must not run it as well."""
    from jailbee.lifecycle import new_container

    _patch_new_container_deps(mocker)
    run_autostart = mocker.patch(
        "jailbee.autostart.run_autostart",
        return_value=AutostartPlan(blocking=[], detached=[_stage("deps")]),
    )
    incus = mocker.MagicMock()
    incus.exists.return_value = False
    seen: list[str] = []

    new_container(
        _cfg_for_new(make_cfg, tmp_path),
        incus,
        _opts(),
        on_detach=lambda block, trigger, repo_dir: seen.append(trigger),
    )

    assert run_autostart.call_count == 1
    assert seen == ["on_create"]


def test_new_container_does_not_hand_off_when_nothing_detaches(tmp_path, make_cfg, mocker):
    from jailbee.lifecycle import new_container

    _patch_new_container_deps(mocker)
    mocker.patch(
        "jailbee.autostart.run_autostart",
        return_value=AutostartPlan(blocking=[], detached=[]),
    )
    incus = mocker.MagicMock()
    incus.exists.return_value = False
    calls: list[str] = []

    new_container(
        _cfg_for_new(make_cfg, tmp_path),
        incus,
        _opts(),
        on_detach=lambda block, trigger, repo_dir: calls.append(trigger),
    )

    assert calls == []


def test_new_container_forwards_the_autostart_override(tmp_path, make_cfg, mocker):
    """`--wait` / `--no-wait` reaches the planner through the options object;
    without it the flag would be accepted and then ignored."""
    from jailbee.lifecycle import new_container

    _patch_new_container_deps(mocker)
    run_autostart = mocker.patch(
        "jailbee.autostart.run_autostart",
        return_value=AutostartPlan(blocking=[], detached=[]),
    )
    incus = mocker.MagicMock()
    incus.exists.return_value = False

    new_container(_cfg_for_new(make_cfg, tmp_path), incus, _opts(autostart_override="no_wait"))

    # Both triggers, not just the last: `on_create` planning without the
    # override would run stages the operator asked to defer.
    assert run_autostart.call_count == 2
    assert [c.kwargs["override"] for c in run_autostart.call_args_list] == ["no_wait"] * 2


# ---- the override survives the background job file


def test_autostart_override_round_trips_through_the_job_file():
    """Both halves of the codec, in one assertion: a field added to only
    `op_to_job` is dropped in silence on the `--background` path."""
    from jailbee import background

    job = background.op_to_job(
        _opts(autostart_override="no_wait"), container_name="c1", log_path="/l"
    )

    assert job["opts"]["autostart_override"] == "no_wait"
    opts, _name, _log = background.job_to_opts(job)
    assert opts.autostart_override == "no_wait"


def test_autostart_override_defaults_to_none_for_an_older_job_file():
    """An in-flight background `jailbee new` must survive the upgrade that
    introduced the key."""
    from jailbee import background

    job = background.op_to_job(_opts(), container_name="c1", log_path="/l")
    del job["opts"]["autostart_override"]

    opts, _name, _log = background.job_to_opts(job)
    assert opts.autostart_override is None
