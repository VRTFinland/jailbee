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


def _record(events: list[tuple[str, str, str]]):
    """An `on_progress` callback that appends every (stage, step, state)."""

    def on_progress(stage: str, step: str, state: str) -> None:
        events.append((stage, step, state))

    return on_progress


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
    """The sibling must be *reaped*, not merely launched: both chains' first
    steps are in flight before the first poll, so asserting that `b1` was
    launched would survive a driver that raised the moment `a1` failed."""
    launched: list[str] = []
    events: list[tuple[str, str, str]] = []
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.tmux.launch_step", side_effect=_recording_launch(launched))
    poll = mocker.patch(
        "jailbee.tmux.poll_steps", side_effect=_scripted_poll([{"a1": 1}, {"b1": 0}])
    )

    cfg = make_cfg(tmp_path)
    stage = _stage(
        stage="deps",
        chains=[
            {"name": "a", "steps": [{"name": "a1", "run": "true"}, {"name": "a2", "run": "true"}]},
            {"name": "b", "steps": [{"name": "b1", "run": "true"}]},
        ],
    )
    with pytest.raises(autostart.AutostartStepError):
        autostart.run_stage(
            cfg, incus, "c1", stage, "/home/dev/repo", on_progress=_record(events)
        )

    assert "a2" not in launched  # failed chain does not advance
    assert ("deps", "a1", "fail") in events
    # The sibling ran to completion before the stage gave up on it.
    assert ("deps", "b1", "ok") in events
    assert poll.call_count == 2


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


# --- the two drivers must agree: continue_on_error × timeout -------------
#
# The same YAML must behave the same way whichever driver runs it, and the
# only thing that picks a driver is how many chains the stage happens to
# have. A step that *times out* under `continue_on_error: true` is the one
# crossing the two paths never covered: the serial path maps the timeout
# onto `AutostartStepError` and honours `continue_on_error`, while the
# parallel path enforces its deadlines itself. The pair below asserts one
# outcome for one config.


def _timeout_tolerant_chain(second_chain: bool):
    """`a1` times out under `continue_on_error`; `a2` must still run."""
    chains = [
        {
            "name": "a",
            "steps": [
                {"name": "a1", "run": "sleep 99", "continue_on_error": True},
                {"name": "a2", "run": "true"},
            ],
        }
    ]
    if second_chain:
        chains.append({"name": "b", "steps": [{"name": "b1", "run": "true"}]})
    return _stage(stage="deps", chains=chains)


def test_a_timeout_under_continue_on_error_advances_the_chain_serially(
    tmp_path, make_cfg, mocker, incus
):
    ran: list[str] = []

    def fake_run_step(incus_, container, *, name, **kw):
        ran.append(name)
        if name == "a1":
            raise TmuxStepError("boom", step_name="a1", reason="timeout", exit_code=None)

    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.tmux.run_step", side_effect=fake_run_step)

    cfg = make_cfg(tmp_path)
    autostart.run_stage(
        cfg, incus, "c1", _timeout_tolerant_chain(second_chain=False), "/home/dev/repo"
    )

    assert ran == ["a1", "a2"]


def test_a_timeout_under_continue_on_error_advances_the_chain_in_parallel(
    tmp_path, make_cfg, mocker, incus
):
    """Same config, one extra chain — and therefore the polling driver.

    Before the fix the timed-out step failed the whole stage *and* stalled
    its chain (`a2` never launched), so a config's behaviour depended on
    how many chains its stage happened to carry.
    """
    launched: list[str] = []

    def fake_launch(incus_, container, *, name, **kw):
        launched.append(name)
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
    mocker.patch("jailbee.tmux.poll_steps", side_effect=_scripted_poll([{}, {"b1": 0, "a2": 0}]))

    cfg = make_cfg(tmp_path)
    autostart.run_stage(
        cfg, incus, "c1", _timeout_tolerant_chain(second_chain=True), "/home/dev/repo"
    )

    assert launched == ["a1", "b1", "a2"]
    assert interrupt.call_args.args[2].name == "a1"  # the timed-out step was stopped


def test_a_timeout_without_continue_on_error_still_fails_the_stage_in_parallel(
    tmp_path, make_cfg, mocker, incus
):
    """The other half of the same branch: `continue_on_error` is what makes
    a timeout survivable, not the timeout path itself."""

    def fake_launch(incus_, container, *, name, **kw):
        return StepHandle(
            name=name,
            window=name,
            sentinel=f"/tmp/{name}",
            background=False,
            deadline=0.0 if name == "a1" else 1e9,
        )

    mocker.patch("jailbee.tmux.interrupt_step")
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.tmux.launch_step", side_effect=fake_launch)
    mocker.patch("jailbee.tmux.poll_steps", side_effect=_scripted_poll([{"b1": 0}]))

    cfg = make_cfg(tmp_path)
    stage = _stage(
        stage="deps",
        chains=[
            {"name": "a", "steps": [{"name": "a1", "run": "sleep 99"}]},
            {"name": "b", "steps": [{"name": "b1", "run": "true"}]},
        ],
    )
    with pytest.raises(autostart.AutostartStepError) as e:
        autostart.run_stage(cfg, incus, "c1", stage, "/home/dev/repo")

    assert "timed out" in str(e.value)


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


def test_the_agent_install_step_asks_to_manage_its_own_network(tmp_path, make_cfg, mocker):
    """`agents._ensure_one` is the one caller that opts in, and the opt-in is
    a defaulted keyword: dropping it leaves the profile alone and a `loose`
    install silently runs under `strict`. Pin the kwarg at the call site."""
    from jailbee.agents import ensure_agents

    cfg = make_cfg(tmp_path, agents={"grok": {"enabled": True}}, shared_dir=tmp_path / "shared")
    incus = mocker.MagicMock()
    incus.exec.side_effect = Exception("not found")  # install_check fails: not installed
    apply_step = mocker.patch("jailbee.autostart._apply_step")

    ensure_agents(cfg, incus, "c1", "/home/dev/repo")

    apply_step.assert_called_once()
    assert apply_step.call_args.kwargs["manage_network"] is True


# --- the on_progress contract (Task 7's progress file is built on it) ----


def test_on_progress_reports_start_then_ok_serially(tmp_path, make_cfg, mocker, incus):
    events: list[tuple[str, str, str]] = []
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.tmux.run_step")

    cfg = make_cfg(tmp_path)
    stage = _stage(stage="deps", steps=[{"name": "s1", "run": "true"}])
    autostart.run_stage(cfg, incus, "c1", stage, "/r", on_progress=_record(events))

    assert events == [("deps", "s1", "start"), ("deps", "s1", "ok")]


def test_on_progress_reports_fail_before_reraising_serially(tmp_path, make_cfg, mocker, incus):
    events: list[tuple[str, str, str]] = []
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch(
        "jailbee.tmux.run_step",
        side_effect=TmuxStepError("boom", step_name="s1", reason="exit", exit_code=1),
    )

    cfg = make_cfg(tmp_path)
    stage = _stage(
        stage="deps",
        steps=[{"name": "s1", "run": "false"}, {"name": "s2", "run": "true"}],
    )
    with pytest.raises(autostart.AutostartStepError):
        autostart.run_stage(cfg, incus, "c1", stage, "/r", on_progress=_record(events))

    assert events == [("deps", "s1", "start"), ("deps", "s1", "fail")]


def test_on_progress_reports_fail_and_the_chain_continues_serially(
    tmp_path, make_cfg, mocker, incus
):
    events: list[tuple[str, str, str]] = []
    calls = {"n": 0}

    def fake_run_step(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TmuxStepError("boom", step_name="s1", reason="exit", exit_code=1)

    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.tmux.run_step", side_effect=fake_run_step)

    cfg = make_cfg(tmp_path)
    stage = _stage(
        stage="deps",
        steps=[
            {"name": "s1", "run": "false", "continue_on_error": True},
            {"name": "s2", "run": "true"},
        ],
    )
    autostart.run_stage(cfg, incus, "c1", stage, "/r", on_progress=_record(events))

    assert events == [
        ("deps", "s1", "start"),
        ("deps", "s1", "fail"),
        ("deps", "s2", "start"),
        ("deps", "s2", "ok"),
    ]


def test_on_progress_reports_terminal_states_in_parallel(tmp_path, make_cfg, mocker, incus):
    """Both terminal states come out of `_finish_step`, so a swapped or
    dropped report shows up only here."""
    events: list[tuple[str, str, str]] = []
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.tmux.launch_step", side_effect=_fake_launch)
    mocker.patch(
        "jailbee.tmux.poll_steps", side_effect=_scripted_poll([{"a1": 0, "b1": 1}])
    )

    cfg = make_cfg(tmp_path)
    stage = _stage(
        stage="deps",
        chains=[
            {"name": "a", "steps": [{"name": "a1", "run": "true"}]},
            {"name": "b", "steps": [{"name": "b1", "run": "false", "continue_on_error": True}]},
        ],
    )
    autostart.run_stage(cfg, incus, "c1", stage, "/r", on_progress=_record(events))

    assert ("deps", "a1", "start") in events
    assert ("deps", "b1", "start") in events
    assert ("deps", "a1", "ok") in events
    assert ("deps", "b1", "fail") in events


# --- the per-step start announcement -------------------------------------


def test_each_step_is_announced_before_it_runs_serially(tmp_path, make_cfg, mocker, incus, capsys):
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.tmux.run_step")

    cfg = make_cfg(tmp_path)
    stage = _stage(
        stage="deps",
        steps=[{"name": "s1", "run": "true"}, {"name": "s2", "run": "true"}],
    )
    autostart.run_stage(cfg, incus, "c1", stage, "/r")

    out = capsys.readouterr().out
    assert "→ step: s1" in out
    assert "→ step: s2" in out
    # The announcement precedes the step's own completion line.
    assert out.index("→ step: s1") < out.index("↳ s1:")


def test_each_step_is_announced_before_it_runs_in_parallel(
    tmp_path, make_cfg, mocker, incus, capsys
):
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.tmux.launch_step", side_effect=_fake_launch)
    mocker.patch("jailbee.tmux.poll_steps", side_effect=_fake_poll_all_ok)

    cfg = make_cfg(tmp_path)
    stage = _stage(
        stage="deps",
        chains=[
            {"name": "a", "steps": [{"name": "a1", "run": "true"}]},
            {"name": "b", "steps": [{"name": "b1", "run": "true"}]},
        ],
    )
    autostart.run_stage(cfg, incus, "c1", stage, "/r")

    out = capsys.readouterr().out
    assert "→ step: a1" in out
    assert "→ step: b1" in out


# --- the driver's own failure modes --------------------------------------


def test_an_unexpected_error_interrupts_the_steps_left_in_flight(
    tmp_path, make_cfg, mocker, incus
):
    """`run_stage`'s `finally` unmounts and flips the profile next. Anything
    still running in tmux at that moment would have the ground moved under
    it, so the driver interrupts what it launched before propagating."""

    def fake_launch(incus_, container, *, name, **kw):
        if name == "b1":
            raise RuntimeError("incus fell over")
        return StepHandle(
            name=name, window=name, sentinel=f"/tmp/{name}", background=False, deadline=1e9
        )

    interrupt = mocker.patch("jailbee.tmux.interrupt_step")
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.tmux.launch_step", side_effect=fake_launch)
    mocker.patch("jailbee.tmux.poll_steps", return_value={})

    cfg = make_cfg(tmp_path)
    stage = _stage(
        stage="deps",
        chains=[
            {"name": "a", "steps": [{"name": "a1", "run": "true"}]},
            {"name": "b", "steps": [{"name": "b1", "run": "true"}]},
        ],
    )
    with pytest.raises(RuntimeError, match="incus fell over"):
        autostart.run_stage(cfg, incus, "c1", stage, "/r")

    assert [c.args[2].name for c in interrupt.call_args_list] == ["a1"]


def test_a_poll_result_for_an_untracked_step_is_ignored(tmp_path, make_cfg, mocker, incus):
    """The loader enforces unique step names per trigger, but the detached
    supervisor builds stages in code that never went through it. A stray
    sentinel must not crash the driver and abandon the live steps."""
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.tmux.launch_step", side_effect=_fake_launch)
    mocker.patch(
        "jailbee.tmux.poll_steps",
        side_effect=_scripted_poll([{"ghost": 0, "a1": 0}, {"b1": 0}]),
    )

    cfg = make_cfg(tmp_path)
    stage = _stage(
        stage="deps",
        chains=[
            {"name": "a", "steps": [{"name": "a1", "run": "true"}]},
            {"name": "b", "steps": [{"name": "b1", "run": "true"}]},
        ],
    )
    autostart.run_stage(cfg, incus, "c1", stage, "/r")


# --- run_autostart's planner hand-off ------------------------------------


def test_run_autostart_forwards_the_cli_overrides_to_the_planner(
    tmp_path, make_cfg, mocker, incus
):
    """`--wait` / `--no-wait` and the on_create->on_start detach hand-off are
    the planner's inputs; dropping either keyword here is invisible until a
    container silently blocks (or silently doesn't)."""
    from jailbee.autostart_plan import AutostartPlan

    planner = mocker.patch(
        "jailbee.autostart.plan_autostart",
        return_value=AutostartPlan(blocking=[], detached=[]),
    )

    cfg = make_cfg(tmp_path, autostart={"on_create": [{"name": "a", "run": "true"}]})
    autostart.run_autostart(
        cfg,
        incus,
        "c1",
        AutostartTrigger.ON_CREATE,
        "/r",
        override="no_wait",
        already_detached=True,
    )

    assert planner.call_args.kwargs["override"] == "no_wait"
    assert planner.call_args.kwargs["already_detached"] is True
