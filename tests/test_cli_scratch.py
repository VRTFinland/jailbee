"""CLI tests for commands that used to traceback in a scratch directory.

Every test here drives a directory with no `.jailbee/config.yaml` through the
*real* loader, so `cfg.is_synthetic()` is genuinely true rather than mocked.

The defect these cover (final-review finding C1): `cli._resolve_config_path`
raises `ConfigNotFoundError` unconditionally, and six call sites still used it
after the loader was converted. `destroy` and `net egress export` computed it
eagerly and so failed on *every* invocation in such a directory; the three
background spawns failed after the container-name and pre-flight work was
already done. `net egress export`/`rm` are covered in test_cli_egress.py.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from jailbee.cli import app

runner = CliRunner()


def _scratch_cwd(tmp_path, monkeypatch, mocker, *, global_yaml: str = "{}\n") -> Path:
    """A config-less directory, chdir'd into, with git kept out of the loader.

    Both `detect_default_branch` and `detect_upstream_remote` are stubbed:
    `_build_config_from_dict` calls both, and the tests below that patch
    `subprocess.Popen` would otherwise break `subprocess.run` underneath the
    real git calls (`subprocess.run` builds a `Popen` internally).
    """
    xdg = tmp_path / ".config"
    (xdg / "jailbee").mkdir(parents=True)
    (xdg / "jailbee" / "global.yaml").write_text(global_yaml)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    repo = tmp_path / "tutkimus"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)

    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    mocker.patch("jailbee.config.loader.detect_upstream_remote", return_value="origin")
    return repo


def _payload(name: str, prefix: str = "tutkimus") -> dict:
    return {
        "name": name,
        "status": "Running",
        "profiles": ["default", f"{prefix}-base", f"{prefix}-binds", f"{prefix}-net-strict"],
        "state": None,
        "config": {},
    }


# ---------------------------------------------------------------------------
# `jb destroy` — the only cleanup path a scratch container has
# ---------------------------------------------------------------------------


def test_destroy_in_a_scratch_directory_destroys_the_container(tmp_path, monkeypatch, mocker):
    """`destroy` resolved its config path eagerly, before the background
    branch and before `Incus()`, so *every* `jb destroy` in a scratch
    directory tracebacked. Asserts the container was actually destroyed, not
    merely that the command exited 0 — the latter would pass against a
    `destroy` that found nothing to do."""
    _scratch_cwd(tmp_path, monkeypatch, mocker)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.list_containers.return_value = [_payload("tutkimus-feat-a")]
    incus.config_get.return_value = None
    destroyed = mocker.patch("jailbee.lifecycle.destroy_container")

    result = runner.invoke(app, ["destroy", "--all", "--force"])

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert destroyed.call_count == 1
    assert destroyed.call_args.args[2] == "tutkimus-feat-a"


def test_destroy_background_in_a_scratch_directory_omits_the_config_flag(
    tmp_path, monkeypatch, mocker
):
    """The worker cannot be given a `--config` path that does not exist. It
    re-synthesizes the same config from the cwd it inherits, which is why the
    spawn also has to run in `cfg.repo_root`."""
    repo = _scratch_cwd(tmp_path, monkeypatch, mocker)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.list_containers.return_value = [_payload("tutkimus-feat-a")]
    incus.config_get.return_value = None
    popen = mocker.patch("jailbee.cli.subprocess.Popen")
    popen.return_value = mocker.MagicMock(pid=4242)

    result = runner.invoke(app, ["destroy", "--all", "--force", "--background"])

    assert result.exit_code == 0, result.output
    popen.assert_called_once()
    argv = popen.call_args.args[0]
    assert argv[1:4] == ["-m", "jailbee", "_destroy-worker"]
    assert "--config" not in argv
    assert popen.call_args.kwargs["cwd"] == str(repo)


def test_destroy_background_for_a_configured_repo_still_passes_the_config_flag(
    tmp_path, mocker, make_cfg
):
    """The guard rail on the fix: a repo with a real config file must still
    hand the worker its path. Without this, dropping `--config` altogether
    would leave every test above green."""
    repo = tmp_path / "myrepo"
    (repo / ".jailbee").mkdir(parents=True)
    cfg_path = repo / ".jailbee" / "config.yaml"
    cfg_path.write_text("{}\n")
    cfg = make_cfg(repo)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_config_path_or_none", return_value=cfg_path)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.list_containers.return_value = [_payload("myrepo-feat-a", prefix="myrepo")]
    incus.config_get.return_value = None
    popen = mocker.patch("jailbee.cli.subprocess.Popen")
    popen.return_value = mocker.MagicMock(pid=4242)

    result = runner.invoke(app, ["destroy", "--all", "--force", "--background"])

    assert result.exit_code == 0, result.output
    argv = popen.call_args.args[0]
    assert "--config" in argv
    assert argv[argv.index("--config") + 1] == str(cfg_path)


# ---------------------------------------------------------------------------
# `jb start --background` / `jb restart --background`
# ---------------------------------------------------------------------------


def _boot_env(tmp_path, monkeypatch, mocker):
    repo = _scratch_cwd(tmp_path, monkeypatch, mocker)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "tutkimus-feat-a"))
    popen = mocker.patch("jailbee.cli.subprocess.Popen")
    popen.return_value = mocker.MagicMock(pid=7777)
    return repo, popen


def test_start_background_in_a_scratch_directory_omits_the_config_flag(
    tmp_path, monkeypatch, mocker
):
    repo, popen = _boot_env(tmp_path, monkeypatch, mocker)

    result = runner.invoke(app, ["start", "feat-a", "--background"])

    assert result.exit_code == 0, result.output
    argv = popen.call_args.args[0]
    assert argv[1:4] == ["-m", "jailbee", "_boot-worker"]
    assert "--config" not in argv
    assert "--restart" not in argv
    assert popen.call_args.kwargs["cwd"] == str(repo)


def test_restart_background_in_a_scratch_directory_omits_the_config_flag(
    tmp_path, monkeypatch, mocker
):
    repo, popen = _boot_env(tmp_path, monkeypatch, mocker)

    result = runner.invoke(app, ["restart", "feat-a", "--background"])

    assert result.exit_code == 0, result.output
    argv = popen.call_args.args[0]
    assert argv[1:4] == ["-m", "jailbee", "_boot-worker"]
    assert "--config" not in argv
    # `--restart` still lands after the (now conditional) `--config` fragment.
    assert "--restart" in argv
    assert popen.call_args.kwargs["cwd"] == str(repo)


def test_start_background_from_the_config_setting_in_a_scratch_directory(
    tmp_path, monkeypatch, mocker
):
    """`boot.background: true` in `scratch.config` reaches the same spawn
    without any flag — the config-settable route the finding calls out, where
    a user never types `--background` and still hits the crash."""
    repo = _scratch_cwd(
        tmp_path,
        monkeypatch,
        mocker,
        global_yaml="scratch:\n  config:\n    boot:\n      background: true\n",
    )
    incus = mocker.patch("jailbee.incus.Incus").return_value
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "tutkimus-feat-a"))
    popen = mocker.patch("jailbee.cli.subprocess.Popen")
    popen.return_value = mocker.MagicMock(pid=7777)

    result = runner.invoke(app, ["start", "feat-a"])

    assert result.exit_code == 0, result.output
    argv = popen.call_args.args[0]
    assert argv[1:4] == ["-m", "jailbee", "_boot-worker"]
    assert "--config" not in argv
    assert popen.call_args.kwargs["cwd"] == str(repo)


# ---------------------------------------------------------------------------
# `jb new --background`
# ---------------------------------------------------------------------------


def test_new_background_in_a_scratch_directory_omits_the_config_flag(tmp_path, monkeypatch, mocker):
    """`new.background: true` is config-settable, so a user with it set hits
    this on every scratch `jb new`. Driven through the flag here; the
    `_resolve_config_path` call is the same either way."""
    from jailbee.global_config import DockerRegistryMirror, GlobalConfig

    repo = _scratch_cwd(tmp_path, monkeypatch, mocker)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.exists.return_value = False
    incus.image_exists.return_value = True
    incus.profile_exists.return_value = True

    mirror_data_dir = tmp_path / "registry"
    (mirror_data_dir / "ca").mkdir(parents=True)
    (mirror_data_dir / "ca" / "ca.crt").write_text("fake-ca")
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=GlobalConfig(
            docker_registry_mirror=DockerRegistryMirror(data_dir=mirror_data_dir)
        ),
    )
    mocker.patch(
        "jailbee.docker_daemon.compute_mirror_endpoint",
        return_value=("10.234.216.1", 3128),
    )
    # Orthogonal to the argv under test: the host-side asking a detached
    # worker cannot do.
    mocker.patch("jailbee.cli._preflight_background_new", side_effect=lambda _cfg, opts: opts)
    mocker.patch("jailbee.egress_pool.register_repo")
    refresh = mocker.MagicMock()
    refresh.status = "ok"
    mocker.patch("jailbee.egress_pool.refresh_pool", return_value=refresh)

    popen = mocker.patch("jailbee.cli.subprocess.Popen")
    popen.return_value = mocker.MagicMock(pid=1234)

    result = runner.invoke(app, ["new", "--mount", "work", "--background"])

    assert result.exit_code == 0, result.output
    popen.assert_called_once()
    argv = popen.call_args.args[0]
    assert argv[1:4] == ["-m", "jailbee", "_new-worker"]
    assert "--config" not in argv
    assert popen.call_args.kwargs["cwd"] == str(repo)


# ---------------------------------------------------------------------------
# I1: the registry-row hijack, refused before anything expensive happens
# ---------------------------------------------------------------------------


def test_new_in_a_scratch_dir_colliding_with_a_configured_repo_refuses(
    tmp_path, monkeypatch, mocker, make_cfg
):
    """`~/Downloads/myapp` derives the same `container_prefix` as a
    configured `~/src/myapp`, and registering it would repoint that repo's
    row at the scratch directory. `new_cmd` refuses up front — before the
    (multi-minute) scratch base-image build, which is why the check is a
    pre-flight and not only `register_repo`'s own guard."""
    from sqlmodel import Session

    from jailbee.db import get_engine
    from jailbee.egress_pool import register_repo

    xdg = tmp_path / ".config"
    (xdg / "jailbee").mkdir(parents=True)
    (xdg / "jailbee" / "global.yaml").write_text("{}\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    mocker.patch("jailbee.config.loader.detect_upstream_remote", return_value="origin")

    configured = tmp_path / "src" / "myapp"
    configured.mkdir(parents=True)
    with Session(get_engine()) as session:
        register_repo(session, make_cfg(configured))

    scratch = tmp_path / "Downloads" / "myapp"
    (scratch / ".git").mkdir(parents=True)
    monkeypatch.chdir(scratch)

    incus = mocker.patch("jailbee.incus.Incus").return_value
    incus.exists.return_value = False
    # False, so a missing refusal would reach the image-build pre-flight and
    # this test would still be able to tell the two apart.
    incus.image_exists.return_value = False
    build = mocker.patch("jailbee.golden.build_golden_image")
    new_container = mocker.patch("jailbee.lifecycle.new_container")

    result = runner.invoke(app, ["new", "work", "--no-clone", "--no-autostart"])
    collapsed = " ".join(result.output.split())

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert str(configured.resolve()) in collapsed
    assert "jailbee config init" in collapsed
    assert build.call_count == 0
    assert new_container.call_count == 0

    # And the configured repo's row survived untouched.
    from jailbee.db.models import RegisteredRepo

    with Session(get_engine()) as session:
        row = session.get(RegisteredRepo, "myapp")
    assert row is not None
    assert row.repo_root == str(configured.resolve())
    assert row.synthetic_config is False
