from typer.testing import CliRunner

from jailbee.cli import app
from tests.conftest import panel_text

runner = CliRunner()


def _env(mocker, tmp_path, make_cfg, snaps):
    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    incus = mocker.MagicMock()
    resolve = mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "app-x"))
    mocker.patch("jailbee.lifecycle.short_name", return_value="x")
    mocker.patch("jailbee.snapshots.list_snapshots", return_value=snaps)
    return resolve


def test_delete_without_arguments_asks_container_destructively_then_tag(mocker, tmp_path, make_cfg):
    resolve = _env(
        mocker, tmp_path, make_cfg, [{"name": "s1", "created_at": "2026-10-01T10:00:00Z"}]
    )
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    select = mocker.patch("jailbee.prompting._select", return_value="s1")
    delete = mocker.patch("jailbee.snapshots.delete_snapshot")
    result = runner.invoke(app, ["snapshot", "delete"])
    assert result.exit_code == 0, result.output
    assert resolve.call_args.args[1] is None
    assert resolve.call_args.kwargs == {"always_prompt": True}
    assert select.call_count == 1  # one snapshot, still asked: destructive
    delete.assert_called_once_with(mocker.ANY, "app-x", "s1")


def test_restore_without_tag_asks_even_for_one_snapshot(mocker, tmp_path, make_cfg):
    resolve = _env(mocker, tmp_path, make_cfg, [{"name": "s1", "created_at": "a"}])
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    select = mocker.patch("jailbee.prompting._select", return_value="s1")
    restore = mocker.patch("jailbee.snapshots.restore_snapshot")
    result = runner.invoke(app, ["snapshot", "restore", "x"])
    assert result.exit_code == 0, result.output
    assert resolve.call_args.kwargs == {"always_prompt": True}
    assert select.call_count == 1
    restore.assert_called_once_with(mocker.ANY, mocker.ANY, "app-x", "s1")


def test_delete_with_one_snapshot_off_a_tty_does_not_auto_take_it(mocker, tmp_path, make_cfg):
    _env(mocker, tmp_path, make_cfg, [{"name": "s1", "created_at": "a"}])
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    delete = mocker.patch("jailbee.snapshots.delete_snapshot")
    result = runner.invoke(app, ["snapshot", "delete", "x"])
    assert result.exit_code == 2
    assert "Candidates: s1" in panel_text(result.output)
    delete.assert_not_called()


def test_delete_cancel_at_the_tag_picker_deletes_nothing(mocker, tmp_path, make_cfg):
    _env(mocker, tmp_path, make_cfg, [{"name": "s1", "created_at": "x"}])
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.prompting._select", return_value=None)
    delete = mocker.patch("jailbee.snapshots.delete_snapshot")
    result = runner.invoke(app, ["snapshot", "delete", "x"])
    assert result.exit_code == 1
    assert "cancelled" in panel_text(result.output)
    delete.assert_not_called()


def test_restore_with_no_snapshots_exits_2(mocker, tmp_path, make_cfg):
    _env(mocker, tmp_path, make_cfg, [])
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    restore = mocker.patch("jailbee.snapshots.restore_snapshot")
    result = runner.invoke(app, ["snapshot", "restore", "x"])
    assert result.exit_code == 2
    assert "no snapshots" in panel_text(result.output)
    restore.assert_not_called()


def test_restore_named_container_without_tag_off_a_tty_names_tags(mocker, tmp_path, make_cfg):
    _env(
        mocker,
        tmp_path,
        make_cfg,
        [{"name": "s1", "created_at": "a"}, {"name": "s2", "created_at": "b"}],
    )
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    result = runner.invoke(app, ["snapshot", "restore", "x"])
    assert result.exit_code == 2
    assert "Candidates: s1, s2" in panel_text(result.output)


def test_delete_picks_the_container_destructively_then_the_tag(mocker, tmp_path, make_cfg):
    """Unmocked resolver: one container is still picked (not auto-taken), then the tag."""
    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.incus.Incus", return_value=mocker.MagicMock())
    info = mocker.MagicMock()
    info.name = "app-x"
    info.display_name = "x"
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[info])
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    pick_container = mocker.patch("jailbee.tui.pick_container", return_value="app-x")
    select = mocker.patch("jailbee.prompting._select", return_value="s1")
    mocker.patch("jailbee.snapshots.list_snapshots", return_value=[{"name": "s1"}])
    delete = mocker.patch("jailbee.snapshots.delete_snapshot")
    result = runner.invoke(app, ["snapshot", "delete"])
    assert result.exit_code == 0, result.output
    pick_container.assert_called_once()
    assert select.call_count == 1  # the tag; the container went through pick_container
    delete.assert_called_once_with(mocker.ANY, "app-x", "s1")
