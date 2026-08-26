"""CLI tests for `gie destroy` (single-target, --all, interactive)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from jailbee.cli import app
from tests.conftest import make_cfg


def _payload(name: str, status: str = "Running") -> dict:
    return {
        "name": name,
        "status": status,
        "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
        "state": {"network": {"eth0": {"addresses": [{"address": "10.0.0.5", "family": "inet"}]}}},
        "config": {"limits.memory": "4GB"},
    }


def _setup(tmp_path: Path, mocker, container_names: list[str]):
    """Wire up a cfg with container_prefix='myrepo' and a mocked Incus.

    Returns (incus_mock_instance, destroy_mock) so tests can assert
    against destroy calls and tweak per-container behavior.
    """
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    object.__setattr__(cfg, "container_prefix", "myrepo")

    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus_cls = mocker.patch("jailbee.incus.Incus")
    incus = incus_cls.return_value
    incus.list_containers.return_value = [_payload(n) for n in container_names]
    incus.config_get.return_value = None
    destroy_mock = mocker.patch("jailbee.lifecycle.destroy_container")
    return incus, destroy_mock


def _clone_payload(name: str, status: str = "Running") -> dict:
    """Like `_payload`, but a clone-mode container with a repo dir — the
    shape the destroy guard will actually probe."""
    p = _payload(name, status)
    p["config"] = {
        **p["config"],
        "user.jailbee.mode": "clone",
        "user.jailbee.repo_dir": "/home/dev/repo",
        "user.jailbee.base_branch": "main",
    }
    return p


def _at_risk(**overrides):
    from jailbee.git_status import GitStatus

    base = {"wt": "+12 -3", "ahead_diff": "clean", "ahead_count": "0", "conflict": "ok"}
    return GitStatus(**{**base, **overrides})


def test_destroy_single_probes_and_warns_about_stranded_work(tmp_path, mocker):
    """The single-name path carries no git status — it probes the one container."""
    incus, _ = _setup(tmp_path, mocker, [])
    incus.list_containers.return_value = [_clone_payload("myrepo-feat-a")]
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="myrepo-feat-a")
    probe = mocker.patch("jailbee.git_status.probe_container_git", return_value=_at_risk())
    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)

    result = CliRunner().invoke(app, ["destroy", "feat-a"], input="y\nn\n")

    assert probe.call_count == 1
    assert "working tree +12 -3" in result.stdout
    # The first `y` answered the plain confirmation; the guard's prompt got `n`.
    assert result.exit_code != 0


def test_destroy_declining_the_risk_prompt_destroys_nothing(tmp_path, mocker):
    incus, destroy_mock = _setup(tmp_path, mocker, [])
    incus.list_containers.return_value = [_clone_payload("myrepo-feat-a")]
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="myrepo-feat-a")
    mocker.patch("jailbee.git_status.probe_container_git", return_value=_at_risk())
    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)

    CliRunner().invoke(app, ["destroy", "feat-a"], input="y\nn\n")

    destroy_mock.assert_not_called()


def test_destroy_accepting_both_prompts_destroys(tmp_path, mocker):
    incus, destroy_mock = _setup(tmp_path, mocker, [])
    incus.list_containers.return_value = [_clone_payload("myrepo-feat-a")]
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="myrepo-feat-a")
    mocker.patch("jailbee.git_status.probe_container_git", return_value=_at_risk())
    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)

    result = CliRunner().invoke(app, ["destroy", "feat-a"], input="y\ny\n")

    assert result.exit_code == 0, result.stdout
    destroy_mock.assert_called_once()


def test_destroy_clean_container_asks_only_once(tmp_path, mocker):
    incus, destroy_mock = _setup(tmp_path, mocker, [])
    incus.list_containers.return_value = [_clone_payload("myrepo-feat-a")]
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="myrepo-feat-a")
    mocker.patch(
        "jailbee.git_status.probe_container_git",
        return_value=_at_risk(wt="clean"),
    )
    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)

    result = CliRunner().invoke(app, ["destroy", "feat-a"], input="y\n")

    assert result.exit_code == 0, result.stdout
    assert "Destroying loses this" not in result.stdout
    destroy_mock.assert_called_once()


def test_destroy_force_skips_the_probe_and_both_prompts(tmp_path, mocker):
    incus, destroy_mock = _setup(tmp_path, mocker, [])
    incus.list_containers.return_value = [_clone_payload("myrepo-feat-a")]
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="myrepo-feat-a")
    probe = mocker.patch("jailbee.git_status.probe_container_git")

    result = CliRunner().invoke(app, ["destroy", "feat-a", "--force"])

    assert result.exit_code == 0, result.stdout
    probe.assert_not_called()
    destroy_mock.assert_called_once()


def test_destroy_notes_unknown_git_status(tmp_path, mocker):
    """A stopped container is never probed — say the status is unknown rather
    than let silence read as safety."""
    incus, _ = _setup(tmp_path, mocker, [])
    incus.list_containers.return_value = [_clone_payload("myrepo-feat-a", status="Stopped")]
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="myrepo-feat-a")
    probe = mocker.patch("jailbee.git_status.probe_container_git")

    result = CliRunner().invoke(app, ["destroy", "feat-a"], input="y\n")

    probe.assert_not_called()
    assert "git status unknown" in result.stdout.lower()


def test_destroy_mount_mode_is_not_reported_as_unknown(tmp_path, mocker):
    """A mount-mode container's working tree *is* the host's directory and
    survives the destroy, so "git status unknown — may discard uncommitted
    work" is the one case where the note would be provably false. It is not
    probed (`gie ls` renders those columns as `—`) and must stay silent."""
    incus, destroy_mock = _setup(tmp_path, mocker, [])
    payload = _clone_payload("myrepo-feat-a")
    payload["config"] = {**payload["config"], "user.jailbee.mode": "mount"}
    incus.list_containers.return_value = [payload]
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="myrepo-feat-a")
    probe = mocker.patch("jailbee.git_status.probe_container_git")

    result = CliRunner().invoke(app, ["destroy", "feat-a"], input="y\n")

    assert result.exit_code == 0, result.stdout
    probe.assert_not_called()
    assert "git status unknown" not in result.stdout.lower()
    destroy_mock.assert_called_once()


def test_destroy_single_missing_from_listing_notes_unknown_and_destroys(tmp_path, mocker):
    """If the resolved container isn't found in list_containers (e.g. it
    vanished between resolve and list), the guard still speaks up instead
    of silently skipping straight to destroy with no note at all."""
    incus, destroy_mock = _setup(tmp_path, mocker, [])
    incus.list_containers.return_value = []  # not present in the listing
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="myrepo-feat-a")
    probe = mocker.patch("jailbee.git_status.probe_container_git")

    result = CliRunner().invoke(app, ["destroy", "feat-a"], input="y\n")

    assert result.exit_code == 0, result.stdout
    probe.assert_not_called()
    assert "git status unknown" in result.stdout.lower()
    destroy_mock.assert_called_once()


def test_destroy_all_lists_at_risk_containers_without_probing(tmp_path, mocker):
    """--all already gets git status from list_containers(with_git_status=True)."""
    incus, destroy_mock = _setup(tmp_path, mocker, [])
    incus.list_containers.return_value = [
        _clone_payload("myrepo-feat-a"),
        _clone_payload("myrepo-feat-b"),
    ]
    probe = mocker.patch("jailbee.git_status.probe_container_git")
    mocker.patch(
        "jailbee.lifecycle.probe_many_parallel",
        return_value={"myrepo-feat-a": _at_risk()},
    )
    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)

    result = CliRunner().invoke(app, ["destroy", "--all"], input="y\nn\n")

    probe.assert_not_called()
    assert "feat-a" in result.stdout
    assert "Destroying loses this" in result.stdout
    destroy_mock.assert_not_called()


def test_destroy_all_force_skips_the_git_status_probe(tmp_path, mocker):
    """`--all --force` never reaches `_warn_before_destroy` (gated on `not
    force`), so probing every running container's git status just for that
    summary — one `incus exec` each — is pure waste. `list_containers` is
    called with `with_git_status=False` in this one case."""
    incus, destroy_mock = _setup(tmp_path, mocker, [])
    incus.list_containers.return_value = [_clone_payload("myrepo-feat-a")]
    probe = mocker.patch("jailbee.lifecycle.probe_many_parallel")

    result = CliRunner().invoke(app, ["destroy", "--all", "--force"])

    assert result.exit_code == 0, result.stdout
    probe.assert_not_called()
    destroy_mock.assert_called_once()


def test_destroy_all_without_force_still_probes_for_the_risk_summary(tmp_path, mocker):
    """Without `--force`, `_warn_before_destroy` needs the probed git status —
    the optimization above must not skip it here."""
    incus, destroy_mock = _setup(tmp_path, mocker, [])
    incus.list_containers.return_value = [_clone_payload("myrepo-feat-a")]
    probe = mocker.patch("jailbee.lifecycle.probe_many_parallel", return_value={})

    result = CliRunner().invoke(app, ["destroy", "--all"], input="y\ny\n")

    assert result.exit_code == 0, result.stdout
    probe.assert_called_once()
    destroy_mock.assert_called_once()


def test_destroy_single_target_prompts_and_destroys(tmp_path, mocker):
    _, destroy_mock = _setup(tmp_path, mocker, ["myrepo-feat-a"])
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="myrepo-feat-a")

    result = CliRunner().invoke(app, ["destroy", "feat-a"], input="y\n")

    assert result.exit_code == 0, result.stdout
    assert "Destroy container 'feat-a'?" in result.stdout
    destroy_mock.assert_called_once()
    assert destroy_mock.call_args.args[2] == "myrepo-feat-a"
    assert destroy_mock.call_args.kwargs == {"force": True}
    assert "Destroyed: feat-a" in result.stdout


def test_destroy_single_target_force_skips_prompt(tmp_path, mocker):
    _, destroy_mock = _setup(tmp_path, mocker, ["myrepo-feat-a"])
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="myrepo-feat-a")

    result = CliRunner().invoke(app, ["destroy", "feat-a", "--force"])

    assert result.exit_code == 0, result.stdout
    assert "Destroy container" not in result.stdout
    destroy_mock.assert_called_once()


def test_destroy_all_with_zero_containers_prints_message_and_exits_zero(tmp_path, mocker):
    _, destroy_mock = _setup(tmp_path, mocker, [])

    result = CliRunner().invoke(app, ["destroy", "--all"])

    assert result.exit_code == 0, result.stdout
    assert "No containers to destroy" in result.stdout
    destroy_mock.assert_not_called()


def test_destroy_all_prompts_then_destroys_each(tmp_path, mocker):
    _, destroy_mock = _setup(tmp_path, mocker, ["myrepo-feat-a", "myrepo-feat-b", "myrepo-feat-c"])

    result = CliRunner().invoke(app, ["destroy", "--all"], input="y\n")

    assert result.exit_code == 0, result.stdout
    assert "Destroy 3 container(s)" in result.stdout
    assert "feat-a" in result.stdout
    assert "feat-b" in result.stdout
    assert "feat-c" in result.stdout
    destroyed = [c.args[2] for c in destroy_mock.call_args_list]
    assert destroyed == ["myrepo-feat-a", "myrepo-feat-b", "myrepo-feat-c"]


def test_destroy_all_prompt_rejected_aborts_without_destroying(tmp_path, mocker):
    _, destroy_mock = _setup(tmp_path, mocker, ["myrepo-feat-a", "myrepo-feat-b"])

    result = CliRunner().invoke(app, ["destroy", "--all"], input="n\n")

    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "Aborted" in combined
    destroy_mock.assert_not_called()


def test_destroy_all_force_skips_prompt(tmp_path, mocker):
    _, destroy_mock = _setup(tmp_path, mocker, ["myrepo-feat-a", "myrepo-feat-b"])

    result = CliRunner().invoke(app, ["destroy", "--all", "--force"])

    assert result.exit_code == 0, result.stdout
    assert "Destroy 2 container(s)" not in result.stdout
    assert destroy_mock.call_count == 2


def test_destroy_all_with_failure_continues_and_exits_one(tmp_path, mocker):
    _, destroy_mock = _setup(tmp_path, mocker, ["myrepo-feat-a", "myrepo-feat-b", "myrepo-feat-c"])
    # Middle container raises; the helper must continue past it.
    destroy_mock.side_effect = [None, ValueError("simulated failure"), None]

    result = CliRunner().invoke(app, ["destroy", "--all", "--force"])

    assert result.exit_code == 1
    assert destroy_mock.call_count == 3
    combined = result.stdout + (result.stderr or "")
    assert "Destroyed: feat-a" in combined
    assert "feat-b: simulated failure" in combined
    assert "Destroyed: feat-c" in combined
    assert "Destroyed 2 of 3 container(s); 1 failed." in combined


def test_destroy_all_with_name_argument_is_mutex_error(tmp_path, mocker):
    _, destroy_mock = _setup(tmp_path, mocker, ["myrepo-feat-a"])

    result = CliRunner().invoke(app, ["destroy", "feat-a", "--all"])

    assert result.exit_code == 2
    assert "mutually exclusive" in result.stdout or "mutually exclusive" in result.stderr
    destroy_mock.assert_not_called()


def test_destroy_no_args_non_tty_errors(tmp_path, mocker):
    _, destroy_mock = _setup(tmp_path, mocker, ["myrepo-feat-a"])
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    picker = mocker.patch("jailbee.tui.pick_containers_multi")

    result = CliRunner().invoke(app, ["destroy"])

    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "no container name given" in combined
    picker.assert_not_called()
    destroy_mock.assert_not_called()


def test_destroy_no_args_tty_destroys_ticked_containers(tmp_path, mocker):
    _, destroy_mock = _setup(tmp_path, mocker, ["myrepo-feat-a", "myrepo-feat-b", "myrepo-feat-c"])
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch(
        "jailbee.tui.pick_containers_multi",
        return_value=["myrepo-feat-a", "myrepo-feat-c"],
    )

    result = CliRunner().invoke(app, ["destroy"])

    assert result.exit_code == 0, result.stdout
    destroyed = [c.args[2] for c in destroy_mock.call_args_list]
    assert destroyed == ["myrepo-feat-a", "myrepo-feat-c"]
    assert "Destroyed: feat-a" in result.stdout
    assert "Destroyed: feat-c" in result.stdout


def test_destroy_no_args_tty_warns_about_the_ticked_containers_risk(tmp_path, mocker):
    """The picker path runs the same guard as the single-name and `--all`
    paths, and only for the containers actually ticked."""
    incus, destroy_mock = _setup(tmp_path, mocker, [])
    incus.list_containers.return_value = [
        _clone_payload("myrepo-feat-a"),
        _clone_payload("myrepo-feat-b"),
    ]
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch(
        "jailbee.tui.pick_containers_multi",
        return_value=["myrepo-feat-a"],
    )
    mocker.patch(
        "jailbee.lifecycle.probe_many_parallel",
        return_value={"myrepo-feat-a": _at_risk(), "myrepo-feat-b": _at_risk(wt="+99 -0")},
    )
    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)

    result = CliRunner().invoke(app, ["destroy"], input="n\n")

    assert "working tree +12 -3" in result.stdout
    assert "+99 -0" not in result.stdout  # the unticked container is not assessed
    assert "Destroying loses this" in result.stdout
    destroy_mock.assert_not_called()


def test_destroy_no_args_tty_force_skips_the_risk_prompt(tmp_path, mocker):
    """`--force` means "don't ask me anything" on every path, the picker
    included: the risk summary sat outside the `if not force:` gate there."""
    incus, destroy_mock = _setup(tmp_path, mocker, [])
    incus.list_containers.return_value = [_clone_payload("myrepo-feat-a")]
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch(
        "jailbee.tui.pick_containers_multi",
        return_value=["myrepo-feat-a"],
    )
    mocker.patch(
        "jailbee.lifecycle.probe_many_parallel",
        return_value={"myrepo-feat-a": _at_risk()},
    )
    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)

    result = CliRunner().invoke(app, ["destroy", "--force"], input="")

    assert result.exit_code == 0, result.stdout
    assert "Destroying loses this" not in result.stdout
    assert "working tree" not in result.stdout
    destroy_mock.assert_called_once()


def test_destroy_no_args_tty_empty_selection_aborts(tmp_path, mocker):
    _, destroy_mock = _setup(tmp_path, mocker, ["myrepo-feat-a"])
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("jailbee.tui.pick_containers_multi", return_value=[])

    result = CliRunner().invoke(app, ["destroy"])

    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "Aborted" in combined
    destroy_mock.assert_not_called()


def test_destroy_no_args_tty_user_cancels_aborts(tmp_path, mocker):
    _, destroy_mock = _setup(tmp_path, mocker, ["myrepo-feat-a"])
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=True)
    mocker.patch("jailbee.tui.pick_containers_multi", return_value=None)

    result = CliRunner().invoke(app, ["destroy"])

    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "Aborted" in combined
    destroy_mock.assert_not_called()


def test_destroy_no_args_zero_containers_exits_clean(tmp_path, mocker):
    _, destroy_mock = _setup(tmp_path, mocker, [])
    # Even in non-TTY, zero containers takes the early exit before the TTY check.
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    picker = mocker.patch("jailbee.tui.pick_containers_multi")

    result = CliRunner().invoke(app, ["destroy"])

    assert result.exit_code == 0, result.stdout
    assert "No containers to destroy" in result.stdout
    picker.assert_not_called()
    destroy_mock.assert_not_called()


def test_destroy_named_prunes_orphaned_op_when_no_container(tmp_path, monkeypatch, mocker):
    """A failed background `gie new` (no container) leaves a job row; destroying
    its name clears the row instead of erroring."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _, destroy_mock = _setup(tmp_path, mocker, [])
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        side_effect=ValueError("no such container: 'feat-pre' (also tried 'myrepo-feat-pre')"),
    )

    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine

    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name="myrepo-feat-pre",
            container_prefix="myrepo",
            branch="feat/pre",
            pid=1,
            log_path="/l",
            now=datetime.now(UTC),
        )

    result = CliRunner().invoke(app, ["destroy", "feat-pre", "--force"])

    assert result.exit_code == 0, result.output
    assert "feat-pre" in result.output
    destroy_mock.assert_not_called()
    with Session(get_engine()) as s:
        assert background.list_jobs(s, "myrepo") == {}


def test_destroy_named_unknown_with_no_op_errors(tmp_path, monkeypatch, mocker):
    """An unknown name with no job row still errors cleanly (exit 1)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _, destroy_mock = _setup(tmp_path, mocker, [])
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        side_effect=ValueError("no such container: 'nope' (also tried 'myrepo-nope')"),
    )

    result = CliRunner().invoke(app, ["destroy", "nope", "--force"])

    assert result.exit_code == 1
    assert "no such container" in result.output
    destroy_mock.assert_not_called()


def test_destroy_worker_success_deletes_op(tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.db.models import JOB_DESTROY

    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    object.__setattr__(cfg, "container_prefix", "myrepo")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus", return_value=mocker.MagicMock())
    dc = mocker.patch("jailbee.lifecycle.destroy_container")

    name = "myrepo-feat-a"
    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name=name,
            container_prefix="myrepo",
            branch=None,
            pid=1,
            log_path="/l",
            now=datetime.now(UTC),
            op_kind=JOB_DESTROY,
        )

    result = CliRunner().invoke(app, ["_destroy-worker", "--name", name, "--force"])
    assert result.exit_code == 0, result.stdout
    dc.assert_called_once()
    assert dc.call_args.args[2] == name
    assert dc.call_args.kwargs["force"] is True
    # destroy_container is mocked (doesn't clear the row), so the worker's own
    # trailing delete_job is what removes it.
    with Session(get_engine()) as s:
        assert background.list_jobs(s, "myrepo") == {}


def test_destroy_worker_failure_marks_op_failed(tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.db.models import JOB_DESTROY

    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    object.__setattr__(cfg, "container_prefix", "myrepo")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus", return_value=mocker.MagicMock())
    mocker.patch(
        "jailbee.lifecycle.destroy_container",
        side_effect=RuntimeError("delete exploded"),
    )

    name = "myrepo-feat-a"
    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name=name,
            container_prefix="myrepo",
            branch=None,
            pid=1,
            log_path="/l",
            now=datetime.now(UTC),
            op_kind=JOB_DESTROY,
        )

    result = CliRunner().invoke(app, ["_destroy-worker", "--name", name, "--force"])
    assert result.exit_code == 1
    with Session(get_engine()) as s:
        ops = background.list_jobs(s, "myrepo")
    assert ops[name].phase == background.PHASE_FAILED
    assert "delete exploded" in (ops[name].error_msg or "")


def test_destroy_worker_survives_a_reset_job_table(tmp_path, monkeypatch, mocker):
    """The create worker's guard, on the destroy side: bookkeeping only."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.db.models import JOB_DESTROY

    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    object.__setattr__(cfg, "container_prefix", "myrepo")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus", return_value=mocker.MagicMock())

    name = "myrepo-feat-a"

    def _destroy(_cfg, _incus, _name, *, force=False, on_phase=None):
        assert on_phase is not None
        on_phase("stopping")
        on_phase("deleting")

    dc = mocker.patch("jailbee.lifecycle.destroy_container", side_effect=_destroy)

    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name=name,
            container_prefix="myrepo",
            branch=None,
            pid=1,
            log_path="/l",
            now=datetime.now(UTC),
            op_kind=JOB_DESTROY,
        )
    # A concurrently running older jailbee resetting the schema mid-destroy.
    with get_engine().begin() as conn:
        conn.exec_driver_sql("DROP TABLE background_op")

    result = CliRunner().invoke(app, ["_destroy-worker", "--name", name, "--force"])

    assert result.exit_code == 0, result.output
    dc.assert_called_once()
    combined = result.output + (result.stderr or "")
    assert "could not record phase 'stopping'" in combined
    assert "could not clear the finished job row" in combined


def test_destroy_background_spawns_worker_and_returns(tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _incus, destroy_mock = _setup(tmp_path, mocker, ["myrepo-feat-a"])
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="myrepo-feat-a")
    popen_mock = mocker.patch("jailbee.cli.subprocess.Popen")
    proc = mocker.MagicMock()
    proc.pid = 5555
    popen_mock.return_value = proc

    result = CliRunner().invoke(app, ["destroy", "feat-a", "--force", "--background"])

    assert result.exit_code == 0, result.stdout
    destroy_mock.assert_not_called()
    popen_mock.assert_called_once()
    argv = popen_mock.call_args.args[0]
    assert argv[1:4] == ["-m", "jailbee", "_destroy-worker"]
    assert "--name" in argv and "myrepo-feat-a" in argv

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.db.models import JOB_DESTROY

    with Session(get_engine()) as s:
        ops = background.list_jobs(s, "myrepo")
    assert len(ops) == 1
    row = ops["myrepo-feat-a"]
    assert row.pid == 5555
    assert row.op_kind == JOB_DESTROY
    assert row.phase == background.PHASE_STARTING


def test_destroy_background_all_spawns_one_worker_each(tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _incus, destroy_mock = _setup(tmp_path, mocker, ["myrepo-a", "myrepo-b"])
    # `destroy --all` (no name) lists with with_git_status=True, which now
    # calls lifecycle.get_head_sha -> a real `git rev-parse HEAD` subprocess.
    # Stub it directly rather than widen the Popen mock below: `Popen` is
    # patched on the shared stdlib `subprocess` module object (not a
    # cli.py-local copy), so it replaces `subprocess.Popen` process-wide —
    # and `subprocess.run` (which get_head_sha uses) is implemented on top
    # of `Popen`. Left un-stubbed, get_head_sha's `communicate()` call
    # returns a bare MagicMock, which fails to unpack as a 2-tuple.
    mocker.patch("jailbee.lifecycle.get_head_sha", return_value=None)
    popen_mock = mocker.patch("jailbee.cli.subprocess.Popen")
    popen_mock.return_value = mocker.MagicMock(pid=4242)

    result = CliRunner().invoke(app, ["destroy", "--all", "--force", "--background"])

    assert result.exit_code == 0, result.stdout
    destroy_mock.assert_not_called()
    assert popen_mock.call_count == 2

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine

    with Session(get_engine()) as s:
        ops = background.list_jobs(s, "myrepo")
    assert set(ops) == {"myrepo-a", "myrepo-b"}


def test_destroy_background_and_no_background_mutually_exclusive(tmp_path, mocker):
    _setup(tmp_path, mocker, ["myrepo-feat-a"])
    result = CliRunner().invoke(
        app, ["destroy", "feat-a", "--background", "--no-background", "--force"]
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output.lower()


def test_destroy_background_orphan_op_prune_stays_synchronous(tmp_path, monkeypatch, mocker):
    """A name that resolves to no live container (only a stale job row) is
    pruned synchronously even with --background — nothing to detach."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _incus, _destroy_mock = _setup(tmp_path, mocker, [])
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        side_effect=ValueError("no such container"),
    )
    popen_mock = mocker.patch("jailbee.cli.subprocess.Popen")

    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine

    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name="myrepo-ghost",
            container_prefix="myrepo",
            branch=None,
            pid=1,
            log_path="/l",
            now=datetime.now(UTC),
        )

    result = CliRunner().invoke(app, ["destroy", "ghost", "--background", "--force"])
    assert result.exit_code == 0, result.stdout
    popen_mock.assert_not_called()
    with Session(get_engine()) as s:
        assert background.list_jobs(s, "myrepo") == {}
