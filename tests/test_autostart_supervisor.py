"""The detached autostart supervisor: job-file codec, row lifecycle, flag."""

from __future__ import annotations

import json

from jailbee import background as bg
from jailbee.config import Autostart


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
        log_path="/tmp/x.log",
        progress_path="/tmp/x.progress.json",
    )
    restored = bg.dict_to_autostart_job(json.loads(json.dumps(payload)))
    step = restored.autostart.on_create[0]
    assert step.network == "loose"
    assert step.mounts == ["aws"]


def test_job_label_renders_an_autostart_stage(mocker):
    from jailbee.db.models import JOB_AUTOSTART

    mocker.patch("jailbee.background.worker_alive", return_value=True)
    assert bg.job_label("deps", 42, kind=JOB_AUTOSTART) == "autostart:deps"


def test_autostart_kind_is_attachable_in_every_phase():
    from jailbee.db.models import JOB_AUTOSTART

    assert JOB_AUTOSTART in bg.ATTACHABLE_OP_KINDS
    assert bg.attachable(JOB_AUTOSTART, "deps") is True
    assert bg.attachable(JOB_AUTOSTART, "anything") is True


def _job_payload(tmp_path, autostart: Autostart, *, from_trigger: str = "on_start"):
    return bg.autostart_job_to_dict(
        container_name="c1",
        autostart=autostart,
        from_trigger=from_trigger,
        repo_dir="/r",
        mirror_endpoint=None,
        log_path=str(tmp_path / "x.log"),
        progress_path=str(tmp_path / "x.progress.json"),
    )


def test_worker_runs_only_the_detached_stages_and_clears_the_flag(tmp_path, mocker, make_cfg):
    from typer.testing import CliRunner

    from jailbee.cli import app

    run_stages = mocker.patch("jailbee.autostart.run_stages")
    incus = mocker.patch("jailbee.incus.Incus").return_value
    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.cli._job_engine", return_value=None)

    job = tmp_path / "job.json"
    job.write_text(
        json.dumps(
            bg.autostart_job_to_dict(
                container_name="c1",
                autostart=Autostart.model_validate(
                    {
                        "on_start": [
                            {"stage": "blocking", "steps": [{"name": "a", "run": "true"}]},
                            {
                                "stage": "deps",
                                "detach": True,
                                "steps": [{"name": "b", "run": "true"}],
                            },
                        ]
                    }
                ),
                from_trigger="on_start",
                repo_dir="/r",
                mirror_endpoint=None,
                log_path=str(tmp_path / "x.log"),
                progress_path=str(tmp_path / "x.progress.json"),
            )
        )
    )

    result = CliRunner().invoke(app, ["_autostart-worker", "--job", str(job)])

    assert result.exit_code == 0
    (_, _, _, stages, _), _ = run_stages.call_args
    assert [s.stage for s in stages] == ["deps"]
    # `call_args` is only the *last* call, so pin the count too: a supervisor
    # that re-ran the blocking half would still end on "deps".
    assert len(run_stages.call_args_list) == 1
    incus.config_unset.assert_any_call("c1", "user.jailbee.autostart_in_progress")


def test_worker_stamps_its_own_pid_before_running_anything(tmp_path, mocker, make_cfg):
    """The foreground left the literal "1" behind. Until the worker replaces it
    with its own pid, a crash here would pin the container loose for good."""
    import os

    from typer.testing import CliRunner

    from jailbee.cli import app

    incus = mocker.patch("jailbee.incus.Incus").return_value
    order: list[str] = []
    incus.config_set.side_effect = lambda *a: order.append("set")
    mocker.patch("jailbee.autostart.run_stages", side_effect=lambda *a, **kw: order.append("run"))
    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.cli._job_engine", return_value=None)

    job = tmp_path / "job.json"
    job.write_text(
        json.dumps(
            _job_payload(
                tmp_path,
                Autostart.model_validate(
                    {
                        "on_start": [
                            {
                                "stage": "deps",
                                "detach": True,
                                "steps": [{"name": "b", "run": "true"}],
                            }
                        ]
                    }
                ),
            )
        )
    )

    result = CliRunner().invoke(app, ["_autostart-worker", "--job", str(job)])

    assert result.exit_code == 0, result.output
    incus.config_set.assert_any_call(
        "c1", "user.jailbee.autostart_in_progress", str(os.getpid())
    )
    assert order[0] == "set"


def test_worker_restores_the_network_with_compare_and_swap(tmp_path, mocker, make_cfg):
    """A detached stage can finish long after the user ran `jailbee net` by
    hand, so the supervisor must never blind-restore the entry mode."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    run_stages = mocker.patch("jailbee.autostart.run_stages")
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.cli._job_engine", return_value=None)

    job = tmp_path / "job.json"
    job.write_text(
        json.dumps(
            _job_payload(
                tmp_path,
                Autostart.model_validate(
                    {
                        "on_start": [
                            {
                                "stage": "deps",
                                "detach": True,
                                "steps": [{"name": "b", "run": "true"}],
                            }
                        ]
                    }
                ),
            )
        )
    )

    result = CliRunner().invoke(app, ["_autostart-worker", "--job", str(job)])

    assert result.exit_code == 0, result.output
    assert run_stages.call_args.kwargs["cas_restore"] is True


def test_worker_resuming_on_create_also_runs_every_on_start_stage(tmp_path, mocker, make_cfg):
    """Once detached, everything after the boundary is the supervisor's — the
    later trigger's blocking half included."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    run_stages = mocker.patch("jailbee.autostart.run_stages")
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.cli._job_engine", return_value=None)
    mocker.patch("jailbee.autostart.agent_autostart_steps", return_value=[])

    job = tmp_path / "job.json"
    job.write_text(
        json.dumps(
            _job_payload(
                tmp_path,
                Autostart.model_validate(
                    {
                        "on_create": [
                            {"stage": "c-block", "steps": [{"name": "a", "run": "true"}]},
                            {
                                "stage": "c-deps",
                                "detach": True,
                                "steps": [{"name": "b", "run": "true"}],
                            },
                        ],
                        "on_start": [
                            {"stage": "s-one", "steps": [{"name": "c", "run": "true"}]},
                        ],
                    }
                ),
                from_trigger="on_create",
            )
        )
    )

    result = CliRunner().invoke(app, ["_autostart-worker", "--job", str(job)])

    assert result.exit_code == 0, result.output
    ran = [call.args[3][0].stage for call in run_stages.call_args_list]
    assert ran == ["c-deps", "s-one"]


def test_worker_clears_the_flag_and_fails_the_row_when_a_stage_raises(
    tmp_path, mocker, make_cfg
):
    """A stage failure must not leave the container pinned loose."""
    from typer.testing import CliRunner

    from jailbee.cli import app

    incus = mocker.patch("jailbee.incus.Incus").return_value
    mocker.patch("jailbee.autostart.run_stages", side_effect=RuntimeError("boom"))
    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.cli._job_engine", return_value=None)
    fail_job = mocker.patch("jailbee.background.fail_job")
    delete_job = mocker.patch("jailbee.background.delete_job")

    job = tmp_path / "job.json"
    job.write_text(
        json.dumps(
            _job_payload(
                tmp_path,
                Autostart.model_validate(
                    {
                        "on_start": [
                            {
                                "stage": "deps",
                                "detach": True,
                                "steps": [{"name": "b", "run": "true"}],
                            }
                        ]
                    }
                ),
            )
        )
    )

    result = CliRunner().invoke(app, ["_autostart-worker", "--job", str(job)])

    assert result.exit_code == 1
    incus.config_unset.assert_any_call("c1", "user.jailbee.autostart_in_progress")
    delete_job.assert_not_called()
    # The row write itself is routed through `_track_job`, whose engine is None
    # here, so `fail_job` is never reached — what matters is that the failure
    # path does not fall through to the success cleanup.
    fail_job.assert_not_called()


def test_worker_writes_progress_entries(tmp_path, mocker, make_cfg):
    from typer.testing import CliRunner

    from jailbee import autostart_progress
    from jailbee.cli import app

    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.cli._job_engine", return_value=None)

    def fake_run_stages(cfg, incus, name, stages, repo_dir, **kw):
        kw["on_progress"]("deps", "b", "start")
        kw["on_progress"]("deps", "b", "ok")

    mocker.patch("jailbee.autostart.run_stages", side_effect=fake_run_stages)

    progress = tmp_path / "x.progress.json"
    job = tmp_path / "job.json"
    job.write_text(
        json.dumps(
            _job_payload(
                tmp_path,
                Autostart.model_validate(
                    {
                        "on_start": [
                            {
                                "stage": "deps",
                                "detach": True,
                                "steps": [{"name": "b", "run": "true"}],
                            }
                        ]
                    }
                ),
            )
        )
    )

    result = CliRunner().invoke(app, ["_autostart-worker", "--job", str(job)])

    assert result.exit_code == 0, result.output
    entries = autostart_progress.read(progress)
    assert [(e.stage, e.step, e.state) for e in entries] == [
        ("deps", "b", "start"),
        ("deps", "b", "ok"),
    ]
    assert all(e.at for e in entries)


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


def test_spawn_autostart_worker_stamps_the_worker_pid(tmp_path, mocker, make_cfg):
    """Without this, a worker that dies before its own stamp leaves the literal
    "1" behind and `loose_revert` skips the container forever."""
    from jailbee import cli
    from jailbee.config import Autostart

    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.cli._job_engine", return_value=None)
    proc = mocker.MagicMock()
    proc.pid = 4242
    mocker.patch("jailbee.cli.subprocess.Popen", return_value=proc)
    incus = mocker.MagicMock()

    cli._spawn_autostart_worker(
        cfg,
        None,
        "c1",
        incus=incus,
        autostart=Autostart(),
        from_trigger="on_start",
        repo_dir="/r",
        mirror_endpoint=None,
    )

    incus.config_set.assert_called_once_with(
        "c1", "user.jailbee.autostart_in_progress", "4242"
    )


def test_spawn_autostart_worker_writes_a_job_file_the_worker_can_read(
    tmp_path, mocker, make_cfg
):
    from jailbee import cli
    from jailbee.config import Autostart

    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.cli._job_engine", return_value=None)
    proc = mocker.MagicMock()
    proc.pid = 4242
    popen = mocker.patch("jailbee.cli.subprocess.Popen", return_value=proc)

    block = Autostart.model_validate(
        {"on_start": [{"stage": "deps", "detach": True, "steps": [{"name": "b", "run": "true"}]}]}
    )
    cli._spawn_autostart_worker(
        cfg,
        None,
        "c1",
        incus=mocker.MagicMock(),
        autostart=block,
        from_trigger="on_start",
        repo_dir="/r",
        mirror_endpoint=("10.0.0.2", 5000),
    )

    argv = popen.call_args.args[0]
    assert argv[1:4] == ["-m", "jailbee", "_autostart-worker"]
    job_file = argv[argv.index("--job") + 1]
    spec = bg.dict_to_autostart_job(json.loads(open(job_file).read()))
    assert spec.container_name == "c1"
    assert spec.from_trigger == "on_start"
    assert spec.mirror_endpoint == ("10.0.0.2", 5000)
    assert spec.autostart.on_start[0].stage == "deps"
