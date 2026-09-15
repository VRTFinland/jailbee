"""Stage-level execution: one switch per stage, CAS restore, parallel chains.

Two drivers live behind `run_stage`, and each test below names the one it
exercises. A stage with a single chain runs its steps serially through
`tmux.run_step` (the flat executor's path, pinned by
`tests/test_autostart.py`); a stage with two or more chains runs them
together through `tmux.launch_step` / `tmux.poll_steps`.
"""

from __future__ import annotations

import pytest

from jailbee import autostart
from jailbee.autostart import AutostartTrigger
from jailbee.tmux import StepHandle, TmuxStepError


def _stage(**kw):
    from jailbee.config import AutostartStage

    return AutostartStage.model_validate(kw)


@pytest.fixture
def incus(mocker):
    m = mocker.Mock()
    m.exec.return_value = ""
    return m


# --- helpers -------------------------------------------------------------


def _fake_launch(incus_, container, *, name, **kw):
    return StepHandle(
        name=name, window=name, sentinel=f"/tmp/{name}", background=False, deadline=1e9
    )


def _fake_poll_all_ok(incus_, container, handles):
    return {h.name: 0 for h in handles}


def _recording_launch(launched: list[str]):
    def fake_launch(incus_, container, *, name, **kw):
        launched.append(name)
        return StepHandle(
            name=name, window=name, sentinel=f"/tmp/{name}", background=False, deadline=1e9
        )

    return fake_launch


def _scripted_poll(polls: list[dict[str, int]]):
    def fake_poll(incus_, container, handles):
        return polls.pop(0) if polls else {}

    return fake_poll


# --- the parallel driver -------------------------------------------------


def test_stage_switches_network_once_for_all_its_chains(tmp_path, make_cfg, mocker, incus):
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    switch = mocker.patch("jailbee.lifecycle.switch_network")
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.tmux.launch_step", side_effect=_fake_launch)
    mocker.patch("jailbee.tmux.poll_steps", side_effect=_fake_poll_all_ok)

    cfg = make_cfg(tmp_path)
    stage = _stage(
        stage="deps",
        network="loose",
        chains=[
            {"name": "a", "steps": [{"name": "s1", "run": "true"}, {"name": "s2", "run": "true"}]},
            {"name": "b", "steps": [{"name": "s3", "run": "true"}]},
        ],
    )
    autostart.run_stage(cfg, incus, "c1", stage, "/home/dev/repo")

    modes = [c.args[3] for c in switch.call_args_list]
    assert modes == ["loose", "strict"]  # one switch in, one restore out


def test_parallel_chains_start_before_either_finishes(tmp_path, make_cfg, mocker, incus):
    launched: list[str] = []
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.tmux.launch_step", side_effect=_recording_launch(launched))
    mocker.patch(
        "jailbee.tmux.poll_steps",
        side_effect=_scripted_poll([{}, {"a1": 0, "b1": 0}, {"a2": 0}]),
    )

    cfg = make_cfg(tmp_path)
    stage = _stage(
        stage="deps",
        chains=[
            {"name": "a", "steps": [{"name": "a1", "run": "true"}, {"name": "a2", "run": "true"}]},
            {"name": "b", "steps": [{"name": "b1", "run": "true"}]},
        ],
    )
    autostart.run_stage(cfg, incus, "c1", stage, "/home/dev/repo")

    # Both chains' first steps are in flight before either advances.
    assert launched[:2] == ["a1", "b1"]
    assert launched[2] == "a2"


def test_failing_chain_does_not_launch_more_steps_but_lets_siblings_finish(
    tmp_path, make_cfg, mocker, incus
):
    launched: list[str] = []
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.tmux.launch_step", side_effect=_recording_launch(launched))
    mocker.patch("jailbee.tmux.poll_steps", side_effect=_scripted_poll([{"a1": 1}, {"b1": 0}]))

    cfg = make_cfg(tmp_path)
    stage = _stage(
        stage="deps",
        chains=[
            {"name": "a", "steps": [{"name": "a1", "run": "true"}, {"name": "a2", "run": "true"}]},
            {"name": "b", "steps": [{"name": "b1", "run": "true"}]},
        ],
    )
    with pytest.raises(autostart.AutostartStepError):
        autostart.run_stage(cfg, incus, "c1", stage, "/home/dev/repo")

    assert "a2" not in launched  # failed chain does not advance
    assert "b1" in launched  # sibling was already running and was awaited


def test_step_timeout_interrupts_only_that_step(tmp_path, make_cfg, mocker, incus):
    """Host-side deadlines exist only in the polling driver, so the stage
    under test carries two chains: `a1` is past its deadline the moment it
    launches, `b1` finishes normally and must not be interrupted."""

    def fake_launch(incus_, container, *, name, timeout, **kw):
        return StepHandle(
            name=name,
            window=name,
            sentinel=f"/tmp/{name}",
            background=False,
            deadline=0.0 if name == "a1" else 1e9,
        )

    interrupt = mocker.patch("jailbee.tmux.interrupt_step")
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.tmux.launch_step", side_effect=fake_launch)
    mocker.patch("jailbee.tmux.poll_steps", side_effect=_scripted_poll([{"b1": 0}]))

    cfg = make_cfg(tmp_path)
    stage = _stage(
        stage="deps",
        chains=[
            {"name": "a", "steps": [{"name": "a1", "run": "sleep 1"}]},
            {"name": "b", "steps": [{"name": "b1", "run": "true"}]},
        ],
    )
    with pytest.raises(autostart.AutostartStepError) as e:
        autostart.run_stage(cfg, incus, "c1", stage, "/home/dev/repo")

    assert "timed out" in str(e.value)
    assert interrupt.call_count == 1
    assert interrupt.call_args.args[2].name == "a1"


def test_continue_on_error_keeps_its_chain_going_in_parallel(tmp_path, make_cfg, mocker, incus):
    launched: list[str] = []
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.tmux.launch_step", side_effect=_recording_launch(launched))
    mocker.patch(
        "jailbee.tmux.poll_steps",
        side_effect=_scripted_poll([{"a1": 1, "b1": 0}, {"a2": 0}]),
    )

    cfg = make_cfg(tmp_path)
    stage = _stage(
        stage="deps",
        chains=[
            {
                "name": "a",
                "steps": [
                    {"name": "a1", "run": "false", "continue_on_error": True},
                    {"name": "a2", "run": "true"},
                ],
            },
            {"name": "b", "steps": [{"name": "b1", "run": "true"}]},
        ],
    )
    autostart.run_stage(cfg, incus, "c1", stage, "/home/dev/repo")
    assert launched == ["a1", "b1", "a2"]


# --- the serial driver ---------------------------------------------------


def test_stage_mounts_attach_once_and_detach_in_finally(tmp_path, make_cfg, mocker, incus):
    """A stage-scoped subject, so the stage holds one chain and the step
    runs through `tmux.run_step` — the path every flat config takes."""
    add = mocker.patch("jailbee.autostart.add_optional_mount")
    remove = mocker.patch("jailbee.autostart.remove_optional_mount")
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch(
        "jailbee.tmux.run_step",
        side_effect=TmuxStepError("boom", step_name="s1", reason="exit", exit_code=1),
    )

    cfg = make_cfg(tmp_path, optional_mounts={"aws": {"host": str(tmp_path), "container": "/aws"}})
    stage = _stage(stage="deps", mounts=["aws"], steps=[{"name": "s1", "run": "true"}])

    with pytest.raises(autostart.AutostartStepError):
        autostart.run_stage(cfg, incus, "c1", stage, "/home/dev/repo")

    assert add.call_count == 1
    assert remove.call_count == 1


def test_continue_on_error_keeps_its_chain_going_serially(tmp_path, make_cfg, mocker, incus):
    calls = {"n": 0}

    def fake_run_step(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TmuxStepError("boom", step_name="a1", reason="exit", exit_code=1)

    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.tmux.ensure_session")
    run_step = mocker.patch("jailbee.tmux.run_step", side_effect=fake_run_step)

    cfg = make_cfg(tmp_path)
    stage = _stage(
        stage="deps",
        steps=[
            {"name": "a1", "run": "false", "continue_on_error": True},
            {"name": "a2", "run": "true"},
        ],
    )
    autostart.run_stage(cfg, incus, "c1", stage, "/home/dev/repo")
    assert run_step.call_count == 2


# --- network restore: compare-and-swap vs. the blind restore -------------


def _serial_net_stage(mocker, make_cfg, tmp_path, *, entry_modes, stage_network):
    mocker.patch("jailbee.lifecycle.current_network_mode", side_effect=entry_modes)
    switch = mocker.patch("jailbee.lifecycle.switch_network")
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.tmux.run_step")
    cfg = make_cfg(tmp_path)
    stage = _stage(stage="deps", network=stage_network, steps=[{"name": "s1", "run": "true"}])
    return cfg, stage, switch


def test_cas_restore_is_skipped_when_the_user_changed_the_mode(
    tmp_path, make_cfg, mocker, incus
):
    """Compare-and-swap: a detached stage must not overwrite a mode the user
    chose with `jailbee net` while it was running."""
    cfg, stage, switch = _serial_net_stage(
        mocker,
        make_cfg,
        tmp_path,
        # entry: loose. At exit the user has flipped it back to loose.
        entry_modes=["loose", "loose"],
        stage_network="strict",
    )
    autostart.run_stage(cfg, incus, "c1", stage, "/r", cas_restore=True)

    assert [c.args[3] for c in switch.call_args_list] == ["strict"]


def test_cas_restore_happens_when_the_mode_is_still_the_stages(tmp_path, make_cfg, mocker, incus):
    """The companion case: nobody touched the mode, so the entry mode is
    put back exactly as the blind restore would have done."""
    cfg, stage, switch = _serial_net_stage(
        mocker,
        make_cfg,
        tmp_path,
        entry_modes=["loose", "strict"],
        stage_network="strict",
    )
    autostart.run_stage(cfg, incus, "c1", stage, "/r", cas_restore=True)

    assert [c.args[3] for c in switch.call_args_list] == ["strict", "loose"]


def test_blind_restore_ignores_a_changed_mode_when_cas_is_off(tmp_path, make_cfg, mocker, incus):
    """`cas_restore=False` is the foreground path and today's behaviour: the
    entry mode goes back unconditionally, even where a compare-and-swap
    would have observed a different mode and stood down."""
    cfg, stage, switch = _serial_net_stage(
        mocker,
        make_cfg,
        tmp_path,
        entry_modes=["strict", "manual"],
        stage_network="loose",
    )
    autostart.run_stage(cfg, incus, "c1", stage, "/r")

    assert [c.args[3] for c in switch.call_args_list] == ["loose", "strict"]


# --- run_autostart's return value ----------------------------------------


def test_run_autostart_returns_the_plan_it_ran(tmp_path, make_cfg, mocker, incus):
    """Task 7 hands `plan.detached` to the supervisor, so the plan has to
    come back out rather than being consumed internally."""
    mocker.patch("jailbee.autostart.run_stages")

    cfg = make_cfg(
        tmp_path,
        autostart={"on_create": [{"name": "a", "run": "true"}, {"name": "b", "run": "true"}]},
    )
    plan = autostart.run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, "/r")

    assert [s.stage for s in plan.blocking] == ["a"]  # both steps, one stage
    assert plan.detached == []


# --- who owns the network profile ----------------------------------------


def test_a_steps_own_network_is_inert_inside_a_stage(tmp_path, make_cfg, mocker, incus):
    """A legacy flat step keeps its `network` field after normalization, but
    the stage that was built from it is what switches. If `_apply_step`
    swapped as well, the profile would round-trip twice per step."""
    from jailbee.autostart_plan import normalize_stages
    from jailbee.config import AutostartStep

    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    switch = mocker.patch("jailbee.lifecycle.switch_network")
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.tmux.run_step")

    cfg = make_cfg(tmp_path)
    (stage,) = normalize_stages([AutostartStep(name="s1", run="true", network="loose")])
    assert stage.all_chains()[0].steps[0].network == "loose"  # the field survives

    autostart.run_stage(cfg, incus, "c1", stage, "/r")

    assert [c.args[3] for c in switch.call_args_list] == ["loose", "strict"]


def test_a_standalone_step_still_manages_its_own_network(tmp_path, make_cfg, mocker, incus):
    """`agents._ensure_one` runs its install command before any stage
    exists, so that one caller asks `_apply_step` to swap the profile
    itself — a `loose` install must still reach the registry."""
    from jailbee.config import AutostartStep

    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    switch = mocker.patch("jailbee.lifecycle.switch_network")
    mocker.patch("jailbee.tmux.run_step")

    cfg = make_cfg(tmp_path)
    step = AutostartStep(name="install-grok", run="true", network="loose")
    autostart._apply_step(cfg, incus, "c1", step, "/r", manage_network=True)

    assert [c.args[3] for c in switch.call_args_list] == ["loose", "strict"]
