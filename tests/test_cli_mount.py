"""`jailbee mount` / `unmount` ask for the optional mount left out."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.mounts import DEVICE_NAME_PREFIX
from tests.conftest import panel_text

runner = CliRunner()
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def _env(mocker, tmp_path, make_cfg, *, attached=()):
    cfg = make_cfg(
        tmp_path,
        optional_mounts={
            "aws": {"host": "~/.aws", "container": "/home/dev/.aws"},
            "gcp": {"host": "~/.config/gcloud", "container": "/home/dev/.config/gcloud"},
        },
    )
    from jailbee.global_config import GlobalConfig

    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._load_global", return_value=GlobalConfig())
    mocker.patch("jailbee.cli._now", return_value=NOW)
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        {"name": "app-x", "devices": {f"{DEVICE_NAME_PREFIX}{k}": {} for k in attached}}
    ]
    incus.config_device_get.return_value = None
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "app-x"))
    mocker.patch("jailbee.lifecycle.short_name", return_value="x")
    return incus


def _ttl_env(mocker, tmp_path, make_cfg, *, attached=(), gcfg=None, **cfg_overrides):
    from jailbee.global_config import GlobalConfig

    mounts = cfg_overrides.pop(
        "optional_mounts",
        {
            "aws": {"host": "~/.aws", "container": "/home/dev/.aws"},
            "docs": {
                "host": "~/docs",
                "container": "/home/dev/docs",
                "auto_unmount_after": "never",
            },
        },
    )
    cfg = make_cfg(tmp_path, optional_mounts=mounts, **cfg_overrides)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._load_global", return_value=gcfg or GlobalConfig())
    mocker.patch("jailbee.cli._now", return_value=NOW)
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        {"name": "app-x", "devices": {f"{DEVICE_NAME_PREFIX}{k}": {} for k in attached}}
    ]
    incus.config_device_get.side_effect = lambda c, dev, key: (
        "/src" if dev.removeprefix(DEVICE_NAME_PREFIX) in attached else None
    )
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "app-x"))
    mocker.patch("jailbee.lifecycle.short_name", return_value="x")
    return incus


def _label(incus):
    sets = {c.args[1]: c.args[2] for c in incus.config_set.call_args_list}
    return sets.get("user.jailbee.mount_until.aws")


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


def test_mount_noninteractive_uses_the_policy_default(mocker, tmp_path, make_cfg):
    incus = _ttl_env(mocker, tmp_path, make_cfg)
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    result = runner.invoke(app, ["mount", "aws", "x"])
    assert result.exit_code == 0, result.output
    incus.config_device_add.assert_called_once()
    assert _label(incus) == (NOW + timedelta(minutes=15)).isoformat()
    assert "auto-unmount in 15m" in panel_text(result.output)


def test_mount_for_flag_sets_that_ttl_without_asking(mocker, tmp_path, make_cfg):
    incus = _ttl_env(mocker, tmp_path, make_cfg)
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    prompt = mocker.patch("jailbee.cli._prompt_ttl")
    result = runner.invoke(app, ["mount", "aws", "x", "--for", "2h"])
    assert result.exit_code == 0, result.output
    prompt.assert_not_called()
    assert _label(incus) == (NOW + timedelta(hours=2)).isoformat()


def test_mount_no_revert_and_for_never_write_no_label(mocker, tmp_path, make_cfg):
    for flags in (["--no-revert"], ["--for", "never"]):
        incus = _ttl_env(mocker, tmp_path, make_cfg)
        result = runner.invoke(app, ["mount", "aws", "x", *flags])
        assert result.exit_code == 0, result.output
        incus.config_set.assert_not_called()
        incus.config_unset_checked.assert_any_call("app-x", "user.jailbee.mount_until.aws")


def test_mount_for_and_no_revert_together_exit_2(mocker, tmp_path, make_cfg):
    incus = _ttl_env(mocker, tmp_path, make_cfg)
    result = runner.invoke(app, ["mount", "aws", "x", "--for", "1h", "--no-revert"])
    assert result.exit_code == 2
    incus.config_device_add.assert_not_called()


def test_mount_bad_for_value_exits_2_before_mounting(mocker, tmp_path, make_cfg):
    incus = _ttl_env(mocker, tmp_path, make_cfg)
    result = runner.invoke(app, ["mount", "aws", "x", "--for", "30min"])
    assert result.exit_code == 2
    incus.config_device_add.assert_not_called()


def test_mount_malformed_policy_refused_before_any_device_add(mocker, tmp_path, make_cfg):
    incus = _ttl_env(mocker, tmp_path, make_cfg, mount_auto_revert={"after": "30min"})
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    result = runner.invoke(app, ["mount", "aws", "x"])
    assert result.exit_code == 2
    assert "mount_auto_revert.after" in panel_text(result.output)
    incus.config_device_add.assert_not_called()
    result = runner.invoke(app, ["mount", "aws", "x", "--for", "1h"])
    assert result.exit_code == 0, result.output
    incus.config_device_add.assert_called_once()
    assert _label(incus) == (NOW + timedelta(hours=1)).isoformat()


def test_mount_interactive_asks_with_the_default(mocker, tmp_path, make_cfg):
    from jailbee.cli import _Ttl

    incus = _ttl_env(mocker, tmp_path, make_cfg)
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    prompt = mocker.patch("jailbee.cli._prompt_ttl", return_value=_Ttl(timedelta(hours=1)))
    result = runner.invoke(app, ["mount", "aws", "x"])
    assert result.exit_code == 0, result.output
    question, default = prompt.call_args.args
    assert "aws" in question
    assert default == "15m"
    assert _label(incus) == (NOW + timedelta(hours=1)).isoformat()


def test_mount_interactive_cancel_mounts_nothing(mocker, tmp_path, make_cfg):
    incus = _ttl_env(mocker, tmp_path, make_cfg)
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.cli._prompt_ttl", return_value=None)
    result = runner.invoke(app, ["mount", "aws", "x"])
    assert result.exit_code != 0
    incus.config_device_add.assert_not_called()
    incus.config_set.assert_not_called()


def test_mount_never_kind_does_not_ask(mocker, tmp_path, make_cfg):
    incus = _ttl_env(mocker, tmp_path, make_cfg)
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    prompt = mocker.patch("jailbee.cli._prompt_ttl")
    result = runner.invoke(app, ["mount", "docs", "x"])
    assert result.exit_code == 0, result.output
    prompt.assert_not_called()
    incus.config_set.assert_not_called()


def test_mount_disabled_policy_does_not_ask(mocker, tmp_path, make_cfg):
    incus = _ttl_env(mocker, tmp_path, make_cfg, mount_auto_revert={"enabled": False})
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    prompt = mocker.patch("jailbee.cli._prompt_ttl")
    result = runner.invoke(app, ["mount", "aws", "x"])
    assert result.exit_code == 0, result.output
    prompt.assert_not_called()
    incus.config_set.assert_not_called()


def test_remount_retimes_instead_of_failing(mocker, tmp_path, make_cfg):
    incus = _ttl_env(mocker, tmp_path, make_cfg, attached=("aws",))
    result = runner.invoke(app, ["mount", "aws", "x", "--for", "4h"])
    assert result.exit_code == 0, result.output
    incus.config_device_add.assert_not_called()
    assert _label(incus) == (NOW + timedelta(hours=4)).isoformat()
    assert "TTL updated" in panel_text(result.output)


def test_mount_bare_minutes_sets_actual_deadline(mocker, tmp_path, make_cfg):
    incus = _ttl_env(mocker, tmp_path, make_cfg)
    result = runner.invoke(app, ["mount", "aws", "x", "--for", "20"])
    assert result.exit_code == 0, result.output
    incus.config_device_add.assert_called_once()
    assert _label(incus) == (NOW + timedelta(minutes=20)).isoformat()


def test_mount_bare_minutes_limits_have_no_side_effects(mocker, tmp_path, make_cfg):
    for value in ("0", "1441"):
        incus = _ttl_env(mocker, tmp_path, make_cfg)
        result = runner.invoke(app, ["mount", "aws", "x", "--for", value])
        assert result.exit_code == 2
        incus.config_device_add.assert_not_called()
        incus.config_set.assert_not_called()
        incus.config_unset_checked_checked.assert_not_called()


def test_malformed_per_kind_default_can_be_explicitly_overridden(mocker, tmp_path, make_cfg):
    for flags in (["--for", "1h"], ["--no-revert"]):
        incus = _ttl_env(
            mocker,
            tmp_path,
            make_cfg,
            optional_mounts={
                "aws": {"host": "/aws", "container": "/aws", "auto_unmount_after": "banana"}
            },
        )
        mocker.patch("jailbee.prompting.is_interactive", return_value=False)
        refused = runner.invoke(app, ["mount", "aws", "x"])
        assert refused.exit_code == 2
        assert "optional_mounts.aws.auto_unmount_after" in panel_text(refused.output)
        incus.config_device_add.assert_not_called()
        result = runner.invoke(app, ["mount", "aws", "x", *flags])
        assert result.exit_code == 0, result.output
        incus.config_device_add.assert_called_once()
        if flags == ["--no-revert"]:
            incus.config_unset_checked.assert_called_once()
            assert _label(incus) is None
        else:
            assert _label(incus) == (NOW + timedelta(hours=1)).isoformat()


def test_failed_no_revert_never_reports_success(mocker, tmp_path, make_cfg):
    from jailbee.incus import IncusError

    incus = _ttl_env(mocker, tmp_path, make_cfg, attached=("aws",))
    incus.config_unset_checked.side_effect = IncusError("daemon unavailable")
    result = runner.invoke(app, ["mount", "aws", "x", "--no-revert"])
    assert result.exit_code != 0
    assert "TTL updated" not in panel_text(result.output)
    incus.config_device_remove.assert_not_called()
