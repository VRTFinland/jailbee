"""The Qt outbox dialog's Sent list: rows from the real state DB, preview, Open."""

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QUrl

from jailbee.dashboard import outbox as dob
from jailbee.dashboard.model import RepoTarget
from jailbee.outbox import sent
from jailbee.outbox.sent import SentAction
from tests.outbox_support import IDENTITY

T0 = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
CREATED = "uuid-alpha-x"  # the instance uuid names the incarnation
URL = "https://github.com/o/r/issues/5#issuecomment-1"


def _rec(manifest, *, minutes=0, instance="alpha-x", created=CREATED, url=URL, state="applied"):
    sent.record(
        instance=instance,
        container_prefix="alpha",
        uuid=created,
        publisher="user",
        action=SentAction(
            outbox="issue",
            manifest=manifest,
            index=0,
            kind="comment",
            repo="o/r",
            target=5,
            url=url,
            title=None,
            content=f"body of {manifest}\nsecond line",
            digest="d",
        ),
        state=state,
        now=T0 + timedelta(minutes=minutes),
    )


def _source(whole_repo):
    if whole_repo:
        live = {"alpha-x": CREATED}
        return dob.read_sent("alpha", live=live)
    return dob.read_sent("alpha", instance="alpha-x", uuid=CREATED)


@pytest.fixture
def dialog(qtbot, mocker, make_cfg, tmp_path):
    from jailbee.qtui import outbox

    cfg = make_cfg(tmp_path)
    mocker.patch.object(outbox, "Incus")
    mocker.patch.object(outbox.config_api, "load_repo_config", return_value=cfg)
    mocker.patch.object(outbox.commands, "resolve_target", return_value=(cfg, IDENTITY.full_name))
    mocker.patch.object(
        outbox.service,
        "load_container",
        return_value=outbox.ContainerView(None, IDENTITY.full_name, False, "n/a", (), ()),
    )
    dlg = outbox.OutboxDialog(RepoTarget(cfg.repo_root, None), "alpha-x", sent_source=_source)
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: not dlg.busy)
    yield dlg
    dlg.close()
    qtbot.waitUntil(lambda: not dlg.busy)


def _texts(dlg):
    return [dlg.sent_tree.topLevelItem(i).text(4) for i in range(dlg.sent_tree.topLevelItemCount())]


def test_sent_list_newest_first_and_current_incarnation_only(dialog):
    _rec("old", minutes=0)
    _rec("new", minutes=5)
    _rec("other-box", minutes=9, instance="alpha-y", created="uuid-alpha-y")
    _rec("earlier", minutes=8, created=CREATED + "-minus-3")
    dialog.sent_button.setChecked(True)
    assert _texts(dialog) == ["body of new", "body of old"]


def test_preview_shows_content_and_url(dialog):
    _rec("one")
    dialog.sent_button.setChecked(True)
    dialog.sent_tree.setCurrentItem(dialog.sent_tree.topLevelItem(0))
    text = dialog.details.toPlainText()
    assert "body of one" in text and "second line" in text and URL in text


def test_open_calls_desktop_services_with_row_url(dialog, mocker):
    _rec("one")
    opened = mocker.patch("jailbee.qtui.outbox.QDesktopServices.openUrl")
    dialog.sent_button.setChecked(True)
    dialog.sent_tree.setCurrentItem(dialog.sent_tree.topLevelItem(0))
    assert dialog.open_button.isEnabled()
    dialog.open_button.click()
    opened.assert_called_once()
    assert opened.call_args.args[0] == QUrl(URL)


def test_open_disabled_without_url(dialog, mocker):
    _rec("nourl", url=None)
    opened = mocker.patch("jailbee.qtui.outbox.QDesktopServices.openUrl")
    dialog.sent_button.setChecked(True)
    dialog.sent_tree.setCurrentItem(dialog.sent_tree.topLevelItem(0))
    assert not dialog.open_button.isEnabled()
    dialog.open_sent_url()
    opened.assert_not_called()


@pytest.mark.parametrize(
    "url", ["file:///tmp/private", "http://github.com/o/r", "javascript:alert(1)"]
)
def test_open_refuses_non_https_url(dialog, mocker, url):
    _rec("unsafe", url=url)
    opened = mocker.patch("jailbee.qtui.outbox.QDesktopServices.openUrl")
    dialog.sent_button.setChecked(True)
    dialog.sent_tree.setCurrentItem(dialog.sent_tree.topLevelItem(0))
    dialog.open_sent_url()
    opened.assert_not_called()
    assert not dialog.open_button.isEnabled()


def test_uncertain_row_is_marked(dialog):
    _rec("maybe", state="uncertain", url=None)
    dialog.sent_button.setChecked(True)
    node = dialog.sent_tree.topLevelItem(0)
    assert "[uncertain]" in node.text(4) and node.text(0).startswith("? ")


def test_whole_repo_shows_other_containers_and_marks_stale(dialog):
    _rec("mine", minutes=1)
    _rec("elsewhere", minutes=2, instance="alpha-gone", created="uuid-alpha-gone")
    _rec("earlier", minutes=3, created=CREATED + "-minus-3")
    dialog.sent_button.setChecked(True)
    assert _texts(dialog) == ["body of mine"]
    dialog.repo_check.setChecked(True)
    assert dialog.sent_tree.headerItem().text(2) == "Container"
    by_summary = {
        dialog.sent_tree.topLevelItem(i).text(5): dialog.sent_tree.topLevelItem(i)
        for i in range(dialog.sent_tree.topLevelItemCount())
    }
    assert set(by_summary) == {"body of mine", "body of elsewhere", "body of earlier"}
    assert not by_summary["body of mine"].text(0).startswith("†")
    assert by_summary["body of elsewhere"].text(0).startswith("†")
    assert by_summary["body of earlier"].text(0).startswith("†")


def test_refresh_rereads_the_log(dialog, qtbot):
    dialog.sent_button.setChecked(True)
    assert _texts(dialog) == []
    _rec("late")
    dialog.refresh()
    qtbot.waitUntil(lambda: not dialog.busy)
    assert _texts(dialog) == ["body of late"]


def test_app_sent_source_uses_snapshot_incarnation(tmp_path):
    from dataclasses import replace

    from jailbee.qtui.app import AppController
    from tests.dashboard_fixtures import cfg_group, ci

    _rec("now", minutes=2)
    _rec("earlier", minutes=1, created=CREATED + "-minus-3")
    info = replace(ci("alpha-x", "alpha"), uuid=CREATED)
    app = AppController.__new__(AppController)
    app._latest = [cfg_group(tmp_path, (info,))]
    read = app._sent_source("alpha-x")
    assert [i.row.content.split("\n")[0] for i in read(False)] == ["body of now"]
    whole = read(True)
    assert [i.gone is None for i in whole] == [True, False]


def test_first_view_is_sent_when_only_sent_records_exist(qtbot, mocker, make_cfg, tmp_path):
    from jailbee.qtui import outbox

    _rec("only")
    cfg = make_cfg(tmp_path)
    mocker.patch.object(outbox, "Incus")
    mocker.patch.object(outbox.config_api, "load_repo_config", return_value=cfg)
    mocker.patch.object(outbox.commands, "resolve_target", return_value=(cfg, IDENTITY.full_name))
    mocker.patch.object(
        outbox.service,
        "load_container",
        return_value=outbox.ContainerView(IDENTITY, IDENTITY.full_name, True, None, (), ()),
    )
    dlg = outbox.OutboxDialog(RepoTarget(cfg.repo_root, None), "alpha-x", sent_source=_source)
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: not dlg.busy)
    assert dlg.sent_mode
    assert _texts(dlg) == ["body of only"]
    assert "No proposals" not in dlg.status.text()
    dlg.sent_button.setChecked(False)  # a later user choice is respected
    dlg.refresh()
    qtbot.waitUntil(lambda: not dlg.busy)
    assert not dlg.sent_mode
    dlg.close()
    qtbot.waitUntil(lambda: not dlg.busy)


def test_sent_button_hidden_without_source(qtbot, mocker, make_cfg, tmp_path):
    from jailbee.qtui import outbox

    cfg = make_cfg(tmp_path)
    mocker.patch.object(outbox, "Incus")
    mocker.patch.object(outbox.config_api, "load_repo_config", return_value=cfg)
    mocker.patch.object(outbox.commands, "resolve_target", return_value=(cfg, IDENTITY.full_name))
    mocker.patch.object(
        outbox.service,
        "load_container",
        return_value=outbox.ContainerView(IDENTITY, IDENTITY.full_name, True, None, (), ()),
    )
    dlg = outbox.OutboxDialog(RepoTarget(cfg.repo_root, None), "alpha-x")
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: not dlg.busy)
    assert dlg.sent_button.isHidden() and not dlg.sent_mode
    dlg.close()
    qtbot.waitUntil(lambda: not dlg.busy)


def test_sent_read_error_stays_visible(qtbot, mocker, make_cfg, tmp_path):
    from jailbee.qtui import outbox

    def broken(whole):
        raise OSError("state db gone")

    cfg = make_cfg(tmp_path)
    mocker.patch.object(outbox, "Incus")
    mocker.patch.object(outbox.config_api, "load_repo_config", return_value=cfg)
    mocker.patch.object(outbox.commands, "resolve_target", return_value=(cfg, IDENTITY.full_name))
    mocker.patch.object(
        outbox.service,
        "load_container",
        return_value=outbox.ContainerView(IDENTITY, IDENTITY.full_name, True, None, (), ()),
    )
    dlg = outbox.OutboxDialog(RepoTarget(cfg.repo_root, None), "alpha-x", sent_source=broken)
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: not dlg.busy)
    dlg._load_sent()
    assert "could not read the Sent log: state db gone" in dlg.status.text()
    assert "Nothing sent yet" not in dlg.status.text()
    dlg.close()
    qtbot.waitUntil(lambda: not dlg.busy)
