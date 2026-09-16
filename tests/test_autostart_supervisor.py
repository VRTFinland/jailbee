"""The detached autostart supervisor: job-file codec, row lifecycle, flag.

Three layers, tested where each one lives:

* the codec and the job-row helpers in `background`, called directly;
* `autostart.run_detached` — the supervisor's algorithm — called directly,
  with a mocked `Incus` and a mocked `run_stages`;
* the `_autostart-worker` command and `_spawn_autostart_worker`, through
  `CliRunner` against a **real** job DB (`conftest` isolates XDG_STATE_HOME),
  because the row lifecycle is what those two exist to drive.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session

from jailbee import background as bg
from jailbee.config import Autostart

FLAG = "user.jailbee.autostart_in_progress"


# ---- the job-file codec


def test_job_file_round_trip_preserves_the_effective_autostart(tmp_path):
    """The branch's autostart must survive serialization — a worker that
    re-read config from disk would silently run the host checkout's stages."""
    block = Autostart.model_validate(
        {
            "on_start": [
                {
                    "stage": "deps",
                    "network": "loose",
                    "detach": True,
                    "chains": [{"name": "a", "steps": [{"name": "s", "run": "uv sync"}]}],
                }
            ]
        }
    )
    payload = bg.autostart_job_to_dict(
        container_name="c1",
        autostart=block,
        from_trigger="on_start",
        repo_dir="/home/dev/repo",
        mirror_endpoint=("10.0.0.2", 5000),
        override=None,
        log_path="/tmp/x.log",
        progress_path="/tmp/x.progress.json",
    )
    restored = bg.dict_to_autostart_job(json.loads(json.dumps(payload)))

    assert restored.container_name == "c1"
    assert restored.from_trigger == "on_start"
    assert restored.repo_dir == "/home/dev/repo"
    assert restored.mirror_endpoint == ("10.0.0.2", 5000)
    stage = restored.autostart.on_start[0]
    assert stage.stage == "deps"
    assert stage.network == "loose"
    assert stage.detach is True
    assert stage.all_chains()[0].steps[0].run == "uv sync"


def test_job_file_round_trip_preserves_a_flat_legacy_block(tmp_path):
    block = Autostart.model_validate(
        {"on_create": [{"name": "sync", "run": "uv sync", "network": "loose", "mounts": ["aws"]}]}
    )
    payload = bg.autostart_job_to_dict(
        container_name="c1",
        autostart=block,
        from_trigger="on_create",
        repo_dir="/r",
        mirror_endpoint=None,
        override=None,
        log_path="/tmp/x.log",
        progress_path="/tmp/x.progress.json",
    )
    restored = bg.dict_to_autostart_job(json.loads(json.dumps(payload)))
    step = restored.autostart.on_create[0]
    assert step.network == "loose"
    assert step.mounts == ["aws"]


def test_job_file_round_trip_preserves_the_wait_override():
    """`_boundary` branches on the override, so the worker's recompute needs
    the same one the foreground split on — see the `--no-wait` test below."""
    payload = bg.autostart_job_to_dict(
        container_name="c1",
        autostart=Autostart(),
        from_trigger="on_start",
        repo_dir="/r",
        mirror_endpoint=None,
        override="no_wait",
        log_path="/tmp/x.log",
        progress_path="/tmp/x.progress.json",
    )
    assert bg.dict_to_autostart_job(json.loads(json.dumps(payload))).override == "no_wait"


def test_job_label_renders_an_autostart_stage(mocker):
    from jailbee.db.models import JOB_AUTOSTART

    mocker.patch("jailbee.background.worker_alive", return_value=True)
    assert bg.job_label("deps", 42, kind=JOB_AUTOSTART) == "autostart:deps"


def test_autostart_kind_is_attachable_in_every_phase():
    from jailbee.db.models import JOB_AUTOSTART

    assert JOB_AUTOSTART in bg.ATTACHABLE_OP_KINDS
    assert bg.attachable(JOB_AUTOSTART, "deps") is True
    assert bg.attachable(JOB_AUTOSTART, "anything") is True


# ---- `autostart.run_detached`, the supervisor's algorithm


def _spec(tmp_path, autostart: Autostart, *, from_trigger="on_start", override=None):
    return bg.dict_to_autostart_job(
        bg.autostart_job_to_dict(
            container_name="c1",
            autostart=autostart,
            from_trigger=from_trigger,
            repo_dir="/r",
            mirror_endpoint=None,
            override=override,
            log_path=str(tmp_path / "x.log"),
            progress_path=str(tmp_path / "x.progress.json"),
        )
    )


def _stage(name, *, detach=False):
    return {"stage": name, "detach": detach, "steps": [{"name": f"s-{name}", "run": "true"}]}


def _cfg_with(make_cfg, tmp_path, block: Autostart):
    return make_cfg(tmp_path).model_copy(update={"autostart": block})


def test_run_detached_runs_only_the_stages_past_the_boundary(tmp_path, mocker, make_cfg):
    from jailbee import autostart as autostart_mod

    run_stages = mocker.patch("jailbee.autostart.run_stages")
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    block = Autostart.model_validate(
        {"on_start": [_stage("blocking"), _stage("deps", detach=True)]}
    )
    incus = mocker.MagicMock()

    autostart_mod.run_detached(_cfg_with(make_cfg, tmp_path, block), incus, _spec(tmp_path, block))

    assert [c.args[3][0].stage for c in run_stages.call_args_list] == ["deps"]
    incus.config_unset.assert_called_once_with("c1", FLAG)


def test_run_detached_stamps_its_own_pid_before_running_anything(tmp_path, mocker, make_cfg):
    """The foreground left the literal "1" behind. Until this is replaced with
    a real pid, a crash here would pin the container loose for good."""
    from jailbee import autostart as autostart_mod

    order: list[str] = []
    incus = mocker.MagicMock()
    incus.config_set.side_effect = lambda *a: order.append("set")
    mocker.patch("jailbee.autostart.run_stages", side_effect=lambda *a, **kw: order.append("run"))
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    block = Autostart.model_validate({"on_start": [_stage("deps", detach=True)]})

    autostart_mod.run_detached(_cfg_with(make_cfg, tmp_path, block), incus, _spec(tmp_path, block))

    incus.config_set.assert_called_once_with("c1", FLAG, str(os.getpid()))
    assert order == ["set", "run"]


def test_run_detached_clears_the_flag_when_a_stage_raises(tmp_path, mocker, make_cfg):
    """A stage failure must not leave the container pinned loose."""
    import pytest

    from jailbee import autostart as autostart_mod

    incus = mocker.MagicMock()
    mocker.patch("jailbee.autostart.run_stages", side_effect=RuntimeError("boom"))
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    block = Autostart.model_validate({"on_start": [_stage("deps", detach=True)]})

    with pytest.raises(RuntimeError, match="boom"):
        autostart_mod.run_detached(
            _cfg_with(make_cfg, tmp_path, block), incus, _spec(tmp_path, block)
        )

    incus.config_unset.assert_called_once_with("c1", FLAG)


def test_run_detached_clears_the_flag_on_an_unknown_trigger(tmp_path, mocker, make_cfg):
    """A job file naming a trigger this build does not know must still leave
    through the `finally` — the pid stamp is already written by then."""
    import pytest

    from jailbee import autostart as autostart_mod

    incus = mocker.MagicMock()
    mocker.patch("jailbee.autostart.run_stages")
    block = Autostart.model_validate({"on_start": [_stage("deps", detach=True)]})
    spec = _spec(tmp_path, block)
    spec = type(spec)(**{**spec.__dict__, "from_trigger": "on_moonrise"})

    with pytest.raises(ValueError):
        autostart_mod.run_detached(_cfg_with(make_cfg, tmp_path, block), incus, spec)

    incus.config_unset.assert_called_once_with("c1", FLAG)


def test_run_detached_restores_the_network_with_compare_and_swap(tmp_path, mocker, make_cfg):
    """A detached stage can finish long after the user ran `jailbee net` by
    hand, so the supervisor must never blind-restore the entry mode."""
    from jailbee import autostart as autostart_mod

    run_stages = mocker.patch("jailbee.autostart.run_stages")
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    block = Autostart.model_validate({"on_start": [_stage("deps", detach=True)]})

    autostart_mod.run_detached(
        _cfg_with(make_cfg, tmp_path, block), mocker.MagicMock(), _spec(tmp_path, block)
    )

    assert run_stages.call_args.kwargs["cas_restore"] is True


def test_run_detached_resuming_on_create_also_runs_every_on_start_stage(tmp_path, mocker, make_cfg):
    """Once detached, everything after the boundary is the supervisor's — the
    later trigger's blocking half included."""
    from jailbee import autostart as autostart_mod

    run_stages = mocker.patch("jailbee.autostart.run_stages")
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    block = Autostart.model_validate(
        {
            "on_create": [_stage("c-block"), _stage("c-deps", detach=True)],
            "on_start": [_stage("s-one")],
        }
    )

    autostart_mod.run_detached(
        _cfg_with(make_cfg, tmp_path, block),
        mocker.MagicMock(),
        _spec(tmp_path, block, from_trigger="on_create"),
    )

    assert [c.args[3][0].stage for c in run_stages.call_args_list] == ["c-deps", "s-one"]


def test_run_detached_honours_a_no_wait_override(tmp_path, mocker, make_cfg):
    """No stage sets `detach: true`, so only the carried `--no-wait` tells the
    worker where the foreground stopped. Without it the planner defers nothing
    and every stage past the first is lost in silence."""
    from jailbee import autostart as autostart_mod

    run_stages = mocker.patch("jailbee.autostart.run_stages")
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    block = Autostart.model_validate({"on_start": [_stage("one"), _stage("two"), _stage("three")]})

    autostart_mod.run_detached(
        _cfg_with(make_cfg, tmp_path, block),
        mocker.MagicMock(),
        _spec(tmp_path, block, override="no_wait"),
    )

    assert [c.args[3][0].stage for c in run_stages.call_args_list] == ["two", "three"]


def test_run_detached_reports_each_stage_through_on_phase(tmp_path, mocker, make_cfg):
    from jailbee import autostart as autostart_mod

    mocker.patch("jailbee.autostart.run_stages")
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    block = Autostart.model_validate({"on_start": [_stage("a", detach=True), _stage("b")]})
    seen: list[str] = []

    autostart_mod.run_detached(
        _cfg_with(make_cfg, tmp_path, block),
        mocker.MagicMock(),
        _spec(tmp_path, block),
        on_phase=seen.append,
    )

    assert seen == ["a", "b"]


# ---- cancellation (`jailbee autostart cancel` → SIGTERM)


def test_run_detached_installs_a_cancel_handler_only_for_the_run(tmp_path, mocker, make_cfg):
    """SIGTERM's default action kills the process without unwinding, so the
    supervisor has to own the signal — and give it back afterwards, since the
    in-process continuation path has work of its own to finish."""
    import signal

    import pytest

    from jailbee import autostart as autostart_mod

    during: list[object] = []
    mocker.patch(
        "jailbee.autostart.run_stages",
        side_effect=lambda *a, **kw: during.append(signal.getsignal(signal.SIGTERM)),
    )
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    block = Autostart.model_validate({"on_start": [_stage("deps", detach=True)]})
    before = signal.getsignal(signal.SIGTERM)

    autostart_mod.run_detached(
        _cfg_with(make_cfg, tmp_path, block), mocker.MagicMock(), _spec(tmp_path, block)
    )

    installed = during[0]
    assert installed is not before
    # Driven directly rather than signalling the test process — same call the
    # interpreter would make.
    with pytest.raises(autostart_mod.AutostartCancelledError):
        installed(signal.SIGTERM, None)  # type: ignore[operator]  # it is the handler we installed
    assert signal.getsignal(signal.SIGTERM) is before


def test_a_cancelled_run_unwinds_the_stage_it_was_on(tmp_path, mocker, make_cfg):
    """The whole point of catching the signal: the steps in flight are
    interrupted, the stage's mounts come off, its network mode is put back and
    the in-progress flag is cleared — none of which a killed process does."""
    import signal

    import pytest

    from jailbee import autostart as autostart_mod
    from jailbee.tmux import StepHandle

    incus = mocker.MagicMock()
    mocker.patch(
        "jailbee.lifecycle.current_network_mode",
        side_effect=["strict", "loose"],  # entry mode, then the CAS re-read
    )
    switch = mocker.patch("jailbee.lifecycle.switch_network")
    mocker.patch("jailbee.autostart.add_optional_mount")
    unmount = mocker.patch("jailbee.autostart.remove_optional_mount")
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch(
        "jailbee.tmux.launch_step",
        side_effect=lambda _i, _c, *, name, **kw: StepHandle(
            name=name, window=name, sentinel=f"/tmp/{name}", background=False, deadline=1e9
        ),
    )
    interrupt = mocker.patch("jailbee.tmux.interrupt_step")

    def cancel_while_the_steps_are_in_flight(*_a, **_kw):
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)  # type: ignore[operator]  # the supervisor's own handler

    mocker.patch("jailbee.tmux.poll_steps", side_effect=cancel_while_the_steps_are_in_flight)

    block = Autostart.model_validate(
        {
            "on_start": [
                {
                    "stage": "deps",
                    "detach": True,
                    "network": "loose",
                    "mounts": ["cache"],
                    # Two chains: the parallel driver is the one that holds
                    # step handles and can interrupt them.
                    "chains": [
                        {"name": "a", "steps": [{"name": "a1", "run": "true"}]},
                        {"name": "b", "steps": [{"name": "b1", "run": "true"}]},
                    ],
                }
            ]
        }
    )

    with pytest.raises(autostart_mod.AutostartCancelledError):
        autostart_mod.run_detached(
            _cfg_with(make_cfg, tmp_path, block), incus, _spec(tmp_path, block)
        )

    assert {c.args[2].name for c in interrupt.call_args_list} == {"a1", "b1"}
    assert unmount.call_args.args[2:] == ("c1", "cache")
    assert [c.args[3] for c in switch.call_args_list] == ["loose", "strict"]
    incus.config_unset.assert_called_once_with("c1", FLAG)


def test_a_cancelled_single_chain_stage_interrupts_its_step_first(tmp_path, mocker, make_cfg):
    """The serial driver holds no step handle, but its window name is derived
    from the step name — so the C-c still goes out *before* the stage unmounts
    and flips the network back under a step that is still running."""
    import signal

    import pytest

    from jailbee import autostart as autostart_mod

    events: list[tuple[str, str]] = []
    mocker.patch(
        "jailbee.lifecycle.current_network_mode",
        side_effect=["strict", "loose"],  # entry mode, then the CAS re-read
    )
    mocker.patch(
        "jailbee.lifecycle.switch_network",
        side_effect=lambda *a, **kw: events.append(("net", a[3])),
    )
    mocker.patch("jailbee.autostart.add_optional_mount")
    mocker.patch(
        "jailbee.autostart.remove_optional_mount",
        side_effect=lambda *a: events.append(("unmount", a[3])),
    )
    mocker.patch(
        "jailbee.tmux.interrupt_window",
        side_effect=lambda _i, _c, window: events.append(("interrupt", window)),
    )
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    mocker.patch("jailbee.tmux.ensure_session")

    def cancel_while_the_step_runs(*_a, **_kw):
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)  # type: ignore[operator]  # the supervisor's own handler

    mocker.patch("jailbee.tmux.run_step", side_effect=cancel_while_the_step_runs)

    block = Autostart.model_validate(
        {
            "on_start": [
                {
                    "stage": "deps",
                    "detach": True,
                    "network": "loose",
                    "mounts": ["cache"],
                    # One chain: the serial driver. The step name is
                    # deliberately tmux-unsafe, so the window it is sent to is
                    # the sanitized one and not the raw name.
                    "steps": [{"name": "npm ci", "run": "true"}],
                }
            ]
        }
    )

    with pytest.raises(autostart_mod.AutostartCancelledError):
        autostart_mod.run_detached(
            _cfg_with(make_cfg, tmp_path, block), mocker.MagicMock(), _spec(tmp_path, block)
        )

    assert events == [
        ("net", "loose"),
        ("interrupt", "npm_ci"),
        ("unmount", "cache"),
        ("net", "strict"),
    ]


def test_the_cancel_handler_disarms_itself_on_the_first_signal(tmp_path, mocker, make_cfg):
    """One-shot: the unwind a cancellation starts runs inside `finally`
    blocks, so a second SIGTERM must not be able to raise through it and skip
    the unmount, the network restore or the flag clear."""
    import signal

    import pytest

    from jailbee import autostart as autostart_mod

    after: list[object] = []
    before = signal.getsignal(signal.SIGTERM)

    def cancel_and_look(*_a, **_kw):
        handler = signal.getsignal(signal.SIGTERM)
        try:
            handler(signal.SIGTERM, None)  # type: ignore[operator]  # the installed handler
        finally:
            after.append(signal.getsignal(signal.SIGTERM))

    mocker.patch("jailbee.autostart.run_stages", side_effect=cancel_and_look)
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    block = Autostart.model_validate({"on_start": [_stage("deps", detach=True)]})

    with pytest.raises(autostart_mod.AutostartCancelledError):
        autostart_mod.run_detached(
            _cfg_with(make_cfg, tmp_path, block), mocker.MagicMock(), _spec(tmp_path, block)
        )

    assert after == [before]


def test_a_cancellation_during_the_unmount_stops_the_run(tmp_path, mocker, make_cfg):
    """The stage's cleanup unmounts warn-and-continue, and
    `AutostartCancelledError` is an ordinary `Exception` — so a SIGTERM arriving
    while a mount comes off was warned about and dropped: the `finally`
    completed and `run_stages` went on to the *next stage* of a run the user
    had just cancelled. The cleanup must still finish (the handler is
    one-shot, so nothing raises it again), but the run must not continue.
    """
    import signal

    import pytest

    from jailbee import autostart as autostart_mod

    ran: list[str] = []
    unmounted: list[str] = []
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    mocker.patch("jailbee.tmux.ensure_session")
    mocker.patch("jailbee.autostart.add_optional_mount")
    mocker.patch(
        "jailbee.tmux.run_step", side_effect=lambda _i, _c, *, name, **kw: ran.append(name)
    )

    def cancel_while_unmounting(_cfg, _incus, _container, mount):
        unmounted.append(mount)
        if len(unmounted) > 1:
            return  # the handler is one-shot; a second call is not a cancel
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)  # type: ignore[operator]  # the supervisor's own handler

    mocker.patch("jailbee.autostart.remove_optional_mount", side_effect=cancel_while_unmounting)

    block = Autostart.model_validate(
        {
            "on_start": [
                {
                    "stage": "deps",
                    "detach": True,
                    "mounts": ["cache", "npm"],
                    "steps": [{"name": "s1", "run": "true"}],
                },
                {"stage": "build", "steps": [{"name": "s2", "run": "true"}]},
            ]
        }
    )

    with pytest.raises(autostart_mod.AutostartCancelledError):
        autostart_mod.run_detached(
            _cfg_with(make_cfg, tmp_path, block), mocker.MagicMock(), _spec(tmp_path, block)
        )

    assert ran == ["s1"], "the cancelled run must not start the next stage"
    # …and the cleanup the cancellation interrupted still ran to the end.
    assert unmounted == ["npm", "cache"]


def test_a_cancellation_is_not_swallowed_by_continue_on_error(tmp_path, mocker, make_cfg):
    """The serial driver turns a failing step into a warning when the step
    says `continue_on_error`. A cancellation is not a failing step, and must
    not be absorbed into the next one."""
    import signal

    import pytest

    from jailbee import autostart as autostart_mod

    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    mocker.patch("jailbee.tmux.ensure_session")
    ran: list[str] = []

    def cancel_the_first_step(_i, _c, *, name, **kw):
        ran.append(name)
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)  # type: ignore[operator]  # the supervisor's own handler

    mocker.patch("jailbee.tmux.run_step", side_effect=cancel_the_first_step)

    block = Autostart.model_validate(
        {
            "on_start": [
                {
                    "stage": "deps",
                    "detach": True,
                    "steps": [
                        {"name": "s1", "run": "true", "continue_on_error": True},
                        {"name": "s2", "run": "true"},
                    ],
                }
            ]
        }
    )

    with pytest.raises(autostart_mod.AutostartCancelledError):
        autostart_mod.run_detached(
            _cfg_with(make_cfg, tmp_path, block), mocker.MagicMock(), _spec(tmp_path, block)
        )

    assert ran == ["s1"]


# ---- the `_autostart-worker` command and the job row


def _jobs():
    from jailbee.db import get_engine

    with Session(get_engine()) as s:
        return bg.list_all_jobs(s)


def _insert_job(name: str, prefix: str, pid: int, phase: str, kind: str) -> None:
    from jailbee.db import get_engine

    with Session(get_engine()) as s:
        bg.start_job(
            s,
            container_name=name,
            container_prefix=prefix,
            branch=None,
            pid=pid,
            log_path="/l",
            now=datetime.now(UTC),
            op_kind=kind,
        )
        bg.set_phase(s, name, phase, now=datetime.now(UTC))


def _worker_cfg(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path)
    object.__setattr__(cfg, "container_prefix", "myrepo")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    return cfg


def _write_job(tmp_path, block: Autostart, *, name="myrepo-c1"):
    job = tmp_path / "job.json"
    job.write_text(
        json.dumps(
            bg.autostart_job_to_dict(
                container_name=name,
                autostart=block,
                from_trigger="on_start",
                repo_dir="/r",
                mirror_endpoint=None,
                override=None,
                log_path=str(tmp_path / "x.log"),
                progress_path=str(tmp_path / "x.progress.json"),
            )
        )
    )
    return job


def test_worker_records_each_stage_on_the_job_row_then_deletes_it(tmp_path, mocker, make_cfg):
    """The row's phase is the stage the supervisor is on — that is the whole
    input to `job_label`'s `autostart:<stage>` — and a clean run clears it."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.db.models import JOB_AUTOSTART

    _worker_cfg(tmp_path, mocker, make_cfg)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    seen: list[str] = []
    mocker.patch(
        "jailbee.autostart.run_stages",
        side_effect=lambda *a, **kw: seen.append(_jobs()["myrepo-c1"].phase),
    )
    _insert_job("myrepo-c1", "myrepo", os.getpid(), bg.PHASE_STARTING, JOB_AUTOSTART)

    block = Autostart.model_validate({"on_start": [_stage("a", detach=True), _stage("b")]})
    job = _write_job(tmp_path, block)
    result = CliRunner().invoke(app, ["_autostart-worker", "--job", str(job)])

    assert result.exit_code == 0, result.output
    assert seen == ["a", "b"]
    assert _jobs() == {}


def test_worker_marks_the_row_failed_and_keeps_it_when_a_stage_raises(tmp_path, mocker, make_cfg):
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.db.models import JOB_AUTOSTART

    _worker_cfg(tmp_path, mocker, make_cfg)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    mocker.patch("jailbee.autostart.run_stages", side_effect=RuntimeError("boom"))
    _insert_job("myrepo-c1", "myrepo", os.getpid(), bg.PHASE_STARTING, JOB_AUTOSTART)

    block = Autostart.model_validate({"on_start": [_stage("a", detach=True)]})
    job = _write_job(tmp_path, block)
    result = CliRunner().invoke(app, ["_autostart-worker", "--job", str(job)])

    assert result.exit_code == 1
    row = _jobs()["myrepo-c1"]
    assert row.phase == bg.PHASE_FAILED
    assert row.error_msg == "boom"


def test_worker_marks_a_cancelled_run_failed_with_the_reason(tmp_path, mocker, make_cfg):
    """A cancelled run must not leave a row that still reads as in flight —
    which is why `AutostartCancelledError` is an ordinary `Exception` and lands in
    the worker's `except Exception` like any other failure."""
    import signal

    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.db.models import JOB_AUTOSTART

    _worker_cfg(tmp_path, mocker, make_cfg)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])

    def cancel(*_a, **_kw):
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)  # type: ignore[operator]  # the supervisor's own handler

    mocker.patch("jailbee.autostart.run_stages", side_effect=cancel)
    _insert_job("myrepo-c1", "myrepo", os.getpid(), bg.PHASE_STARTING, JOB_AUTOSTART)

    block = Autostart.model_validate({"on_start": [_stage("a", detach=True)]})
    job = _write_job(tmp_path, block)
    result = CliRunner().invoke(app, ["_autostart-worker", "--job", str(job)])

    assert result.exit_code == 1
    row = _jobs()["myrepo-c1"]
    assert row.phase == bg.PHASE_FAILED
    assert row.error_msg is not None
    assert "cancelled" in row.error_msg
    # ...and no stack trace in the worker log: a cancellation is a deliberate
    # stop, and a traceback there reads as a bug in jailbee.
    assert "Traceback" not in (result.stdout + (result.stderr or ""))


def test_worker_runs_the_job_files_autostart_not_the_configs(tmp_path, mocker, make_cfg):
    """The create path's effective block is the *target branch's*. A worker
    that used the loaded config would run the host checkout's stages."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = _worker_cfg(tmp_path, mocker, make_cfg)
    object.__setattr__(
        cfg,
        "autostart",
        Autostart.model_validate({"on_start": [_stage("host-checkout", detach=True)]}),
    )
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    run_stages = mocker.patch("jailbee.autostart.run_stages")

    block = Autostart.model_validate({"on_start": [_stage("branch-block", detach=True)]})
    job = _write_job(tmp_path, block)
    result = CliRunner().invoke(app, ["_autostart-worker", "--job", str(job)])

    assert result.exit_code == 0, result.output
    assert [c.args[3][0].stage for c in run_stages.call_args_list] == ["branch-block"]


def test_worker_writes_progress_entries(tmp_path, mocker, make_cfg):
    from typer.testing import CliRunner

    from jailbee import autostart_progress
    from jailbee.cli import app

    _worker_cfg(tmp_path, mocker, make_cfg)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])

    def fake_run_stages(cfg, incus, name, stages, repo_dir, **kw):
        kw["on_progress"]("deps", "b", "start")
        kw["on_progress"]("deps", "b", "ok")

    mocker.patch("jailbee.autostart.run_stages", side_effect=fake_run_stages)

    block = Autostart.model_validate({"on_start": [_stage("deps", detach=True)]})
    job = _write_job(tmp_path, block)
    result = CliRunner().invoke(app, ["_autostart-worker", "--job", str(job)])

    assert result.exit_code == 0, result.output
    entries = autostart_progress.read(tmp_path / "x.progress.json")
    assert [(e.stage, e.step, e.state) for e in entries] == [
        ("deps", "b", "start"),
        ("deps", "b", "ok"),
    ]
    assert all(e.at for e in entries)


# ---- the progress file


def test_progress_read_is_empty_when_the_file_is_absent(tmp_path):
    from jailbee import autostart_progress

    assert autostart_progress.read(tmp_path / "nope.json") == []


def test_progress_append_survives_an_unwritable_path(tmp_path):
    """Progress is bookkeeping: it must never abort the run it describes."""
    from jailbee import autostart_progress

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    autostart_progress.append(
        blocker / "sub" / "p.json",
        autostart_progress.ProgressEntry(stage="s", step="t", state="start", at="now"),
    )


def test_progress_read_skips_a_truncated_line(tmp_path):
    """A supervisor killed mid-write leaves a partial line; the rest still reads."""
    from jailbee import autostart_progress

    path = tmp_path / "p.json"
    path.write_text(
        json.dumps({"stage": "a", "step": "b", "state": "start", "at": "t0"})
        + "\n"
        + '{"stage": "a", "step":'
    )
    entries = autostart_progress.read(path)
    assert [e.state for e in entries] == ["start"]


# ---- the spawner


def _spawn(cli, cfg, mocker, incus, *, pid=4242, override=None):
    proc = mocker.MagicMock()
    proc.pid = pid
    popen = mocker.patch("jailbee.cli.subprocess.Popen", return_value=proc)
    cli._spawn_autostart_worker(
        cfg,
        None,
        "myrepo-c1",
        incus=incus,
        autostart=Autostart.model_validate({"on_start": [_stage("deps", detach=True)]}),
        from_trigger="on_start",
        repo_dir="/r",
        mirror_endpoint=("10.0.0.2", 5000),
        override=override,
    )
    return popen


def test_spawn_records_the_job_row_and_stamps_the_worker_pid(tmp_path, mocker, make_cfg):
    """Without the stamp, a worker that dies before its own leaves the literal
    "1" behind and `loose_revert` skips the container forever."""
    from jailbee import cli
    from jailbee.db.models import JOB_AUTOSTART

    cfg = make_cfg(tmp_path)
    object.__setattr__(cfg, "container_prefix", "myrepo")
    incus = mocker.MagicMock()

    _spawn(cli, cfg, mocker, incus)

    row = _jobs()["myrepo-c1"]
    assert row.pid == 4242
    assert row.op_kind == JOB_AUTOSTART
    assert row.phase == bg.PHASE_STARTING
    incus.config_set.assert_called_once_with("myrepo-c1", FLAG, "4242")


def test_spawn_writes_a_job_file_the_worker_can_read(tmp_path, mocker, make_cfg):
    from jailbee import cli

    cfg = make_cfg(tmp_path)
    object.__setattr__(cfg, "container_prefix", "myrepo")

    popen = _spawn(cli, cfg, mocker, mocker.MagicMock(), override="no_wait")

    argv = popen.call_args.args[0]
    assert argv[1:4] == ["-m", "jailbee", "_autostart-worker"]
    job_file = Path(argv[argv.index("--job") + 1])
    spec = bg.dict_to_autostart_job(json.loads(job_file.read_text()))
    assert spec.container_name == "myrepo-c1"
    assert spec.from_trigger == "on_start"
    assert spec.mirror_endpoint == ("10.0.0.2", 5000)
    assert spec.override == "no_wait"
    assert spec.autostart.on_start[0].stage == "deps"


def test_spawn_refuses_while_another_job_is_live(tmp_path, mocker, make_cfg):
    """`start_job` is keyed on the container name and replaces: spawning under
    a live create/boot worker would hand that worker's row to the supervisor,
    and the parent would then delete a row it no longer owns."""
    from jailbee import cli
    from jailbee.db.models import JOB_CREATE

    cfg = make_cfg(tmp_path)
    object.__setattr__(cfg, "container_prefix", "myrepo")
    _insert_job("myrepo-c1", "myrepo", os.getpid(), bg.PHASE_AUTOSTART, JOB_CREATE)
    incus = mocker.MagicMock()

    popen = _spawn(cli, cfg, mocker, incus)

    popen.assert_not_called()
    incus.config_set.assert_not_called()
    row = _jobs()["myrepo-c1"]
    assert row.op_kind == JOB_CREATE
    assert row.pid == os.getpid()


def test_spawn_clears_the_flag_when_it_refuses(tmp_path, mocker, make_cfg):
    """Refusing means nobody owns the deferred stages, so the literal "1" that
    `run_autostart` left behind is now a lie — and `loose_revert` reads "1" as
    held unconditionally, so leaving it pins the container loose for good."""
    from jailbee import cli
    from jailbee.db.models import JOB_CREATE

    cfg = make_cfg(tmp_path)
    object.__setattr__(cfg, "container_prefix", "myrepo")
    _insert_job("myrepo-c1", "myrepo", os.getpid(), bg.PHASE_AUTOSTART, JOB_CREATE)
    incus = mocker.MagicMock()

    popen = _spawn(cli, cfg, mocker, incus)

    popen.assert_not_called()
    incus.config_unset.assert_called_once_with("myrepo-c1", FLAG)


def test_spawn_proceeds_over_a_dead_job_row(tmp_path, mocker, make_cfg):
    """A row whose worker is gone is not a live job — it must not block the
    deferred stages of the run that just handed over."""
    from jailbee import cli
    from jailbee.db.models import JOB_AUTOSTART, JOB_BOOT

    cfg = make_cfg(tmp_path)
    object.__setattr__(cfg, "container_prefix", "myrepo")
    # `worker_alive` rather than an improbably high pid: what makes the row
    # dead is that its process is gone, and the guard must consult that and
    # not the pid's value.
    _insert_job("myrepo-c1", "myrepo", os.getpid(), bg.PHASE_AUTOSTART, JOB_BOOT)
    mocker.patch("jailbee.background.worker_alive", return_value=False)

    popen = _spawn(cli, cfg, mocker, mocker.MagicMock())

    popen.assert_called_once()
    assert _jobs()["myrepo-c1"].op_kind == JOB_AUTOSTART


# ---- the background workers, which continue past the boundary in-process


def _bg_worker_env(tmp_path, mocker, make_cfg, block: Autostart):
    """A detached worker's world: autostart real, everything around it stubbed.

    `run_stages` is the only executor mock, so the *planner* decides what
    runs — which is the whole point of these tests: they pin which stages a
    worker runs, not that it called something.
    """
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo).model_copy(update={"autostart": block})
    object.__setattr__(cfg, "container_prefix", "myrepo")
    incus = mocker.MagicMock()
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    mocker.patch("jailbee.cli._mirror_endpoint_or_none", return_value=None)
    mocker.patch("jailbee.cli._post_create_gui_launches")
    mocker.patch("jailbee.cli._finalize_new")
    mocker.patch("jailbee.lifecycle.boot_container")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/r")
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="loose")
    mocker.patch("jailbee.autostart.inject_github_token")
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])
    ran: list[str] = []
    mocker.patch(
        "jailbee.autostart.run_stages",
        side_effect=lambda c, i, n, stages, r, **kw: ran.extend(s.stage for s in stages),
    )
    return cfg, incus, ran


def _flag_writes(incus) -> list[str]:
    return [c.args[2] for c in incus.config_set.call_args_list if c.args[1] == FLAG]


def test_boot_worker_does_not_spawn_a_second_supervisor(tmp_path, mocker, make_cfg):
    """The boot worker is already the detached process; handing off to
    another one would create a second job row for the same container and
    race the row this worker still owns."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    block = Autostart.model_validate({"on_start": [_stage("schema"), _stage("deps", detach=True)]})
    _cfg, _incus, ran = _bg_worker_env(tmp_path, mocker, make_cfg, block)
    spawn = mocker.patch("jailbee.cli._spawn_autostart_worker")

    result = CliRunner().invoke(app, ["_boot-worker", "--name", "myrepo-feat-a"])

    assert result.exit_code == 0, result.output
    assert spawn.call_count == 0
    assert ran == ["schema", "deps"]


def test_boot_worker_runs_every_stage_a_no_wait_deferred(tmp_path, mocker, make_cfg):
    """`--wait` / `--no-wait` stay meaningful with `--background`: they move
    the boundary the *worker* observes. Lost anywhere along
    spawn → argv → worker, no stage past the first ever runs, and the flag
    keeps the literal "1" that `loose_revert` honours forever."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    block = Autostart.model_validate(
        {"on_start": [_stage("alpha"), _stage("beta"), _stage("gamma")]}
    )
    _cfg, incus, ran = _bg_worker_env(tmp_path, mocker, make_cfg, block)
    spawn = mocker.patch("jailbee.cli._spawn_autostart_worker")

    result = CliRunner().invoke(app, ["_boot-worker", "--name", "myrepo-feat-a", "--no-wait"])

    assert result.exit_code == 0, result.output
    assert spawn.call_count == 0
    assert ran == ["alpha", "beta", "gamma"]
    # The worker's own pid replaces the foreground's literal "1", and the
    # flag is cleared once its last stage is done.
    assert _flag_writes(incus) == ["1", str(os.getpid())]
    incus.config_unset.assert_called_with("myrepo-feat-a", FLAG)


def test_boot_worker_keeps_its_own_job_row_and_phases_it_per_stage(tmp_path, mocker, make_cfg):
    """One row, one worker: the phase advances to each detached stage on the
    row this worker already owns — same pid, no second row — and the row is
    re-kinded to `autostart` when the continuation begins, because that is
    what it now tracks. The kind is not cosmetic: `attachable`, `job_label`
    and `wait_for_background_ready`'s failure exemption all key on it."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.db.models import JOB_AUTOSTART, JOB_BOOT

    block = Autostart.model_validate(
        {"on_start": [_stage("schema"), _stage("deps", detach=True), _stage("agents-ish")]}
    )
    _bg_worker_env(tmp_path, mocker, make_cfg, block)
    seen: list[tuple[str, str, str, int]] = []

    def record(cfg, incus, name, stages, repo_dir, **kw):
        rows = _jobs()
        assert set(rows) == {"myrepo-feat-a"}, rows
        row = rows["myrepo-feat-a"]
        seen.append((stages[0].stage, row.op_kind, row.phase, row.pid))

    mocker.patch("jailbee.autostart.run_stages", side_effect=record)
    _insert_job("myrepo-feat-a", "myrepo", os.getpid(), bg.PHASE_STARTING, JOB_BOOT)

    result = CliRunner().invoke(app, ["_boot-worker", "--name", "myrepo-feat-a"])

    assert result.exit_code == 0, result.output
    assert seen == [
        ("schema", JOB_BOOT, bg.PHASE_AUTOSTART, os.getpid()),
        ("deps", JOB_AUTOSTART, "deps", os.getpid()),
        ("agents-ish", JOB_AUTOSTART, "agents-ish", os.getpid()),
    ]
    assert _jobs() == {}


def _no_sleep(_seconds: float) -> None:
    """`wait_for_background_ready`'s sleep, as a test assertion: reaching it
    means the gate would have blocked the attach."""
    raise AssertionError("the attach would have blocked on this row")


def test_the_container_is_attachable_during_the_continuation(tmp_path, mocker, make_cfg):
    """`jb shell` / `tmux` / `ide` go through `wait_for_background_ready`,
    which lets an attach in on `attachable(kind, phase)` — unconditional only
    for an autostart row. A boot row carrying a stage name for a phase is not
    in `ATTACHABLE_CREATE_PHASES`, so every attach would block until the last
    deferred stage finished and `start --background --no-wait` would move
    nothing the user can observe."""
    from typer.testing import CliRunner

    from jailbee import lifecycle
    from jailbee.cli import app
    from jailbee.db.models import JOB_BOOT

    block = Autostart.model_validate({"on_start": [_stage("schema"), _stage("deps", detach=True)]})
    cfg, _incus, _ran = _bg_worker_env(tmp_path, mocker, make_cfg, block)
    attached: list[str] = []

    def record(cfg_, incus_, name, stages, repo_dir, **kw):
        if stages[0].stage != "deps":
            return
        # Mid-continuation: the deferred stage is running right now.
        lifecycle.wait_for_background_ready(cfg, "myrepo-feat-a", sleep=_no_sleep)
        attached.append(stages[0].stage)

    mocker.patch("jailbee.autostart.run_stages", side_effect=record)
    _insert_job("myrepo-feat-a", "myrepo", os.getpid(), bg.PHASE_STARTING, JOB_BOOT)

    result = CliRunner().invoke(app, ["_boot-worker", "--name", "myrepo-feat-a"])

    assert result.exit_code == 0, result.output
    assert attached == ["deps"]


def test_a_failed_deferred_stage_leaves_a_row_that_never_gates_an_attach(
    tmp_path, mocker, make_cfg
):
    """`wait_for_background_ready` exempts a failed *autostart* row from the
    gate — refusing a shell over a failed deferred stage is the behaviour the
    job kind exists to prevent. Left kinded as a boot, the same failure turns
    every attach into a refusal."""
    from typer.testing import CliRunner

    from jailbee import lifecycle
    from jailbee.cli import app
    from jailbee.db.models import JOB_AUTOSTART, JOB_BOOT

    block = Autostart.model_validate({"on_start": [_stage("schema"), _stage("deps", detach=True)]})
    cfg, _incus, _ran = _bg_worker_env(tmp_path, mocker, make_cfg, block)

    def blow_up_past_the_boundary(cfg_, incus_, name, stages, repo_dir, **kw):
        if stages[0].stage == "deps":
            raise RuntimeError("deps blew up")

    mocker.patch("jailbee.autostart.run_stages", side_effect=blow_up_past_the_boundary)
    _insert_job("myrepo-feat-a", "myrepo", os.getpid(), bg.PHASE_STARTING, JOB_BOOT)

    result = CliRunner().invoke(app, ["_boot-worker", "--name", "myrepo-feat-a"])

    assert result.exit_code == 1
    row = _jobs()["myrepo-feat-a"]
    assert row.phase == bg.PHASE_FAILED
    assert row.op_kind == JOB_AUTOSTART
    assert row.error_msg == "deps blew up"
    # The whole point of the kind: the failed row is not a gate.
    lifecycle.wait_for_background_ready(cfg, "myrepo-feat-a", sleep=_no_sleep)


def test_new_worker_continues_past_the_boundary_in_process(tmp_path, mocker, make_cfg):
    """`jailbee new --background --no-wait` defers every stage after the
    first — to a supervisor a worker must not spawn. The worker runs them
    itself, resuming at the trigger that detached and taking every later
    trigger in full."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.lifecycle import NewContainerOptions

    block = Autostart.model_validate(
        {
            "on_create": [_stage("clone"), _stage("schema")],
            # Not `agents`: that name is the planner's reserved slot, which
            # is *dropped* when no agent has autostart on.
            "on_start": [_stage("launch")],
        }
    )
    cfg, incus, ran = _bg_worker_env(tmp_path, mocker, make_cfg, block)
    spawn = mocker.patch("jailbee.cli._spawn_autostart_worker")

    def fake_new(cfg_, incus_, opts, *, on_phase=None, confirm_fn=None, on_detach=None):
        # What `new_container` does once `--no-wait` has made `on_create`
        # defer: its blocking half already ran, the rest is the caller's.
        assert on_detach is not None
        on_detach(cfg_.autostart, "on_create", "/r")
        return "myrepo-feat-a"

    mocker.patch("jailbee.lifecycle.new_container", side_effect=fake_new)

    opts = NewContainerOptions(
        container_branch="",
        name="myrepo-feat-a",
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base=cfg.golden.alias,
        clone=False,
        mount=True,
        autostart_override="no_wait",
    )
    job = tmp_path / "job.json"
    job.write_text(
        json.dumps(
            bg.op_to_job(opts, container_name="myrepo-feat-a", log_path=str(tmp_path / "w.log"))
        )
    )

    result = CliRunner().invoke(app, ["_new-worker", "--job", str(job)])

    assert result.exit_code == 0, result.output
    assert spawn.call_count == 0
    assert ran == ["schema", "launch"]
    assert _flag_writes(incus) == [str(os.getpid())]
    incus.config_unset.assert_called_with("myrepo-feat-a", FLAG)


def test_new_worker_writes_progress_beside_its_own_log(tmp_path, mocker, make_cfg):
    """`jailbee autostart status` finds a run's progress next to its log —
    a worker's continuation must land there too, not nowhere."""
    from typer.testing import CliRunner

    from jailbee import autostart_progress
    from jailbee.cli import app
    from jailbee.lifecycle import NewContainerOptions

    block = Autostart.model_validate({"on_start": [_stage("deps", detach=True)]})
    cfg, _incus, _ran = _bg_worker_env(tmp_path, mocker, make_cfg, block)

    def record(cfg_, incus_, name, stages, repo_dir, **kw):
        kw["on_progress"]("deps", "s-deps", "start")
        kw["on_progress"]("deps", "s-deps", "ok")

    mocker.patch("jailbee.autostart.run_stages", side_effect=record)

    def fake_new(cfg_, incus_, opts, *, on_phase=None, confirm_fn=None, on_detach=None):
        assert on_detach is not None
        on_detach(cfg_.autostart, "on_start", "/r")
        return "myrepo-feat-a"

    mocker.patch("jailbee.lifecycle.new_container", side_effect=fake_new)

    opts = NewContainerOptions(
        container_branch="",
        name="myrepo-feat-a",
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base=cfg.golden.alias,
        clone=False,
        mount=True,
    )
    log_path = tmp_path / "myrepo-feat-a-20260101-000000.log"
    job = tmp_path / "job.json"
    job.write_text(
        json.dumps(bg.op_to_job(opts, container_name="myrepo-feat-a", log_path=str(log_path)))
    )

    result = CliRunner().invoke(app, ["_new-worker", "--job", str(job)])

    assert result.exit_code == 0, result.output
    entries = autostart_progress.read(log_path.with_suffix(".progress.json"))
    assert [(e.stage, e.state) for e in entries] == [("deps", "start"), ("deps", "ok")]
