"""`jailbee mount` / `unmount` ask for the optional mount left out."""

from __future__ import annotations

from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.mounts import DEVICE_NAME_PREFIX
from tests.conftest import panel_text

runner = CliRunner()


def _env(mocker, tmp_path, make_cfg, *, attached=()):
    cfg = make_cfg(
        tmp_path,
        optional_mounts={
            "aws": {"host": "~/.aws", "container": "/home/dev/.aws"},
            "gcp": {"host": "~/.config/gcloud", "container": "/home/dev/.config/gcloud"},
        },
    )
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        {"name": "app-x", "devices": {f"{DEVICE_NAME_PREFIX}{k}": {} for k in attached}}
    ]
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "app-x"))
    mocker.patch("jailbee.lifecycle.short_name", return_value="x")
    return incus


def test_mount_without_kind_offers_unattached_kinds(mocker, tmp_path, make_cfg):
    _env(mocker, tmp_path, make_cfg, attached=("aws",))
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    add = mocker.patch("jailbee.mounts.add_optional_mount")
    result = runner.invoke(app, ["mount"])
    assert result.exit_code == 0, result.output
    assert add.call_args.args[3] == "gcp"  # the only unattached kind, auto-taken
    assert "Using optional mount gcp" in panel_text(result.output)


def test_unmount_without_kind_takes_the_only_attached_kind(mocker, tmp_path, make_cfg):
    _env(mocker, tmp_path, make_cfg, attached=("aws",))
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    remove = mocker.patch("jailbee.mounts.remove_optional_mount")
    result = runner.invoke(app, ["unmount"])
    assert result.exit_code == 0, result.output
    assert remove.call_args.args[3] == "aws"
    assert "Using optional mount aws" in panel_text(result.output)


def test_unmount_without_kind_and_nothing_mounted_exits_2(mocker, tmp_path, make_cfg):
    _env(mocker, tmp_path, make_cfg)
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    remove = mocker.patch("jailbee.mounts.remove_optional_mount")
    result = runner.invoke(app, ["unmount"])
    assert result.exit_code == 2
    assert "nothing mounted" in panel_text(result.output)
    remove.assert_not_called()


def test_mount_several_kinds_off_a_tty_names_them(mocker, tmp_path, make_cfg):
    _env(mocker, tmp_path, make_cfg)
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    add = mocker.patch("jailbee.mounts.add_optional_mount")
    result = runner.invoke(app, ["mount"])
    assert result.exit_code == 2
    assert "Candidates: aws, gcp" in panel_text(result.output)
    add.assert_not_called()
