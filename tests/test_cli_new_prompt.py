"""`jailbee new --prompt` / `jailbee fork --prompt`: the initial prompt flag."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from jailbee.cli import app
from tests.conftest import make_cfg
from tests.test_cli_new_detach import _setup_new

runner = CliRunner()

AGENTS_CLAUDE = {"claude": {"enabled": True, "autostart": True}}
AGENTS_AIDER = {"aider": {"enabled": True, "autostart": True}}


def _env(tmp_path, mocker, agents=None):
    cfg = _setup_new(tmp_path, mocker)
    cfg = make_cfg(
        tmp_path / "myrepo",
        after_new="none",
        agents=agents or AGENTS_CLAUDE,
    )
    object.__setattr__(cfg, "container_prefix", "myrepo")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    return cfg


def _new(mocker):
    return mocker.patch("jailbee.lifecycle.new_container", return_value="myrepo-feat-a")


_BASE = ["new", "feat-a", "--mount", "--no-attach", "--no-background"]


def test_new_prompt_file_reaches_options(tmp_path, mocker):
    _env(tmp_path, mocker)
    f = tmp_path / "p.md"
    f.write_text("fix bug 12\n")
    nc = _new(mocker)
    result = runner.invoke(app, [*_BASE, "--prompt", str(f)])
    assert result.exit_code == 0, result.output
    assert nc.call_args.args[2].initial_prompt == "fix bug 12\n"


def test_new_without_prompt_has_none(tmp_path, mocker):
    _env(tmp_path, mocker)
    nc = _new(mocker)
    result = runner.invoke(app, _BASE)
    assert result.exit_code == 0, result.output
    assert nc.call_args.args[2].initial_prompt is None


def test_new_prompt_from_stdin(tmp_path, mocker):
    _env(tmp_path, mocker)
    nc = _new(mocker)
    result = runner.invoke(app, [*_BASE, "--prompt", "-"], input="from stdin")
    assert result.exit_code == 0, result.output
    assert nc.call_args.args[2].initial_prompt == "from stdin"


def test_new_prompt_refused_when_primary_lacks_prompt_args(tmp_path, mocker):
    _env(tmp_path, mocker, agents=AGENTS_AIDER)
    f = tmp_path / "p.md"
    f.write_text("hello")
    nc = _new(mocker)
    register = mocker.patch("jailbee.egress_pool.register_repo")
    ensure = mocker.patch("jailbee.create_flow.ensure_profiles")
    result = runner.invoke(app, [*_BASE, "--prompt", str(f)])
    assert result.exit_code == 2
    assert "aider" in result.output
    nc.assert_not_called()
    register.assert_not_called()
    ensure.assert_not_called()


def test_new_prompt_with_no_autostart_exits_2(tmp_path, mocker):
    _env(tmp_path, mocker)
    f = tmp_path / "p.md"
    f.write_text("hello")
    nc = _new(mocker)
    result = runner.invoke(app, [*_BASE, "--no-autostart", "--prompt", str(f)])
    assert result.exit_code == 2
    assert "--prompt" in result.output and "--no-autostart" in result.output
    nc.assert_not_called()


def test_new_prompt_empty_file_exits_2(tmp_path, mocker):
    _env(tmp_path, mocker)
    f = tmp_path / "p.md"
    f.write_text("  \n")
    nc = _new(mocker)
    result = runner.invoke(app, [*_BASE, "--prompt", str(f)])
    assert result.exit_code == 2
    assert "empty" in result.output
    nc.assert_not_called()


def test_new_prompt_missing_file_exits_2(tmp_path, mocker):
    _env(tmp_path, mocker)
    nc = _new(mocker)
    result = runner.invoke(app, [*_BASE, "--prompt", str(tmp_path / "nope.md")])
    assert result.exit_code == 2
    assert "nope.md" in result.output
    nc.assert_not_called()


def test_new_prompt_background_puts_prompt_in_job_file(tmp_path, mocker, monkeypatch):
    from jailbee import background

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _env(tmp_path, mocker)
    f = tmp_path / "p.md"
    f.write_text("do the thing\n")
    import jailbee.incus

    jailbee.incus.Incus.return_value.exists.return_value = False
    popen = mocker.patch("jailbee.create_flow.subprocess.Popen")
    popen.return_value.pid = 99
    mocker.patch("jailbee.background.track_job")
    nc = _new(mocker)
    result = runner.invoke(app, ["new", "feat-a", "--mount", "--background", "--prompt", str(f)])
    assert result.exit_code == 0, result.output
    nc.assert_not_called()
    argv = popen.call_args.args[0]
    job_file = argv[argv.index("--job") + 1]
    with open(job_file) as fh:
        opts, _name, _log = background.job_to_opts(json.load(fh))
    assert opts.initial_prompt == "do the thing\n"


def test_fork_forwards_prompt(tmp_path, mocker):
    from pathlib import Path

    from jailbee.cli import fork_cmd  # noqa: F401

    cfg = _env(tmp_path, mocker)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(mocker.MagicMock(), "myrepo-src"))
    new = mocker.patch("jailbee.cli.new_cmd")
    f = tmp_path / "p.md"
    f.write_text("hi")
    result = runner.invoke(app, ["fork", "src", "dst", "--prompt", str(f)])
    assert result.exit_code == 0, result.output
    assert new.call_args.kwargs["prompt"] == Path(str(f))
    assert cfg is not None
