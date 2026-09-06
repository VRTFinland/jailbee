"""Tests for `jailbee apps`."""

from __future__ import annotations

from typer.testing import CliRunner

from jailbee.cli import app

runner = CliRunner()


def test_apps_ls_lists_configured_apps(tmp_path, mocker, monkeypatch):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/opt/f/f", "description": "Figma"}})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    result = runner.invoke(app, ["apps", "ls"])
    assert result.exit_code == 0
    assert "figma" in result.output
    assert "config" in result.output


def test_apps_ls_omits_status_without_a_container(tmp_path, mocker):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/opt/f/f"}})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    probe = mocker.patch("jailbee.apps.probe")
    result = runner.invoke(app, ["apps", "ls"])
    assert "STATUS" not in result.output
    assert not probe.called


def test_apps_ls_probes_when_given_a_container(tmp_path, mocker):
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/opt/f/f"}})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_attachable", return_value=(Incus(), "c1"))
    mocker.patch("jailbee.apps.probe", return_value="missing")
    result = runner.invoke(app, ["apps", "ls", "c1"])
    assert "STATUS" in result.output
    assert "missing" in result.output


def test_apps_ls_empty_registry_says_so(tmp_path, mocker):
    from tests.conftest import make_cfg

    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    result = runner.invoke(app, ["apps", "ls"])
    assert result.exit_code == 0
    assert "No GUI apps" in result.output


def test_apps_run_launches_the_named_app(tmp_path, mocker):
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/opt/f/f"}})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_attachable", return_value=(Incus(), "c1"))
    launch = mocker.patch("jailbee.apps.launch")
    result = runner.invoke(app, ["apps", "run", "figma", "c1", "--", "--flag"])
    assert result.exit_code == 0
    assert launch.call_args.args[3].name == "figma"
    assert launch.call_args.args[4] == ["--flag"]


def test_apps_run_unknown_name_exits_2_and_lists_options(tmp_path, mocker):
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/opt/f/f"}})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_attachable", return_value=(Incus(), "c1"))
    result = runner.invoke(app, ["apps", "run", "nope", "c1"])
    assert result.exit_code == 2
    assert "figma" in result.output


def test_apps_run_omitting_container_resolves_default(tmp_path, mocker):
    """App-first, container optional: `jailbee apps run figma` alone must resolve.

    Distinguishes app-first from container-first-optional: under the brief's
    original (wrong) order, this exact invocation would bind "figma" to the
    container slot and the app slot would be None, which errors instead of
    launching.
    """
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/opt/f/f"}})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    resolve_attachable = mocker.patch(
        "jailbee.cli._resolve_attachable", return_value=(Incus(), "c1")
    )
    launch = mocker.patch("jailbee.apps.launch")
    result = runner.invoke(app, ["apps", "run", "figma"])
    assert result.exit_code == 0
    # The container positional was omitted, so `_resolve_attachable` must have
    # been called with name=None, not "figma".
    assert resolve_attachable.call_args.args[1] is None
    assert launch.call_args.args[3].name == "figma"
    assert launch.call_args.args[4] == []
