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


def test_run_detached_resuming_on_create_also_runs_every_on_start_stage(
    tmp_path, mocker, make_cfg
):
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
    block = Autostart.model_validate(
        {"on_start": [_stage("one"), _stage("two"), _stage("three")]}
    )

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
    block = Autostart.model_validate(
        {"on_start": [_stage("a", detach=True), _stage("b")]}
    )
    seen: list[str] = []

    autostart_mod.run_detached(
        _cfg_with(make_cfg, tmp_path, block),
        mocker.MagicMock(),
        _spec(tmp_path, block),
        on_phase=seen.append,
    )

    assert seen == ["a", "b"]


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
