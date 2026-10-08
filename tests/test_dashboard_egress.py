"""Tests for the dashboard's scoped egress panel."""

from __future__ import annotations

from jailbee import egress_scope
from jailbee.dashboard.egress import (
    EgressState,
    egress_argv,
    egress_label,
    removable_entry,
)
from jailbee.egress_scope import EntryRow


def test_empty_panel_and_argv(make_cfg, tmp_path, mocker):
    from jailbee.dashboard.egress_data import load_egress_rows

    mocker.patch("jailbee.dashboard.egress_data.load_repo_config", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.dashboard.egress_data.get_engine")
    classify = mocker.patch("jailbee.dashboard.egress_data.classify_sources", return_value=[])
    assert load_egress_rows(tmp_path, mocker.Mock(), None) == ()
    assert classify.call_args.kwargs == {"container": None}


def test_loader_classifies_scoped_rows_and_preserves_redundant_sources(make_cfg, tmp_path, mocker):
    from jailbee.dashboard.egress_data import load_egress_rows

    cfg = make_cfg(tmp_path, egress_allow=["config.example", "shared.example", "shared.example"])
    legacy = mocker.Mock(entry="shared.example")
    session = mocker.MagicMock()
    session.exec.return_value.all.return_value = [legacy]
    local = mocker.patch("jailbee.egress_scope.local_entries", return_value=["shared.example"])
    incus = mocker.MagicMock()
    incus.config_get.return_value = '["container.example", "shared.example"]'
    mocker.patch("jailbee.dashboard.egress_data.load_repo_config", return_value=cfg)
    mocker.patch("jailbee.dashboard.egress_data.get_engine")
    mocker.patch(
        "jailbee.dashboard.egress_data.Session"
    ).return_value.__enter__.return_value = session

    rows = load_egress_rows(tmp_path, incus, "repo-feat")
    indexed = {(row.entry, row.source): row for row in rows}
    assert indexed[("config.example", egress_scope.CONFIG_SOURCE)]
    assert indexed[("shared.example", egress_scope.LOCAL_SOURCE)].redundant
    assert indexed[("shared.example", egress_scope.LEGACY_SOURCE)].redundant
    assert indexed[("shared.example", egress_scope.CONTAINER_SOURCE)].redundant
    assert ("container.example", egress_scope.CONTAINER_SOURCE) in indexed
    repo_rows = load_egress_rows(tmp_path, incus, None)
    assert all(row.source != egress_scope.CONTAINER_SOURCE for row in repo_rows)
    local.assert_called()
    incus.config_get.assert_called_once()


def test_argv_names_the_scope_explicitly():
    entry = "example.org:443"
    repo = EgressState("repo", None, ())
    container = EgressState("repo", "repo-feat", ())
    assert egress_argv(repo, "rm", entry) == ["net", "egress", "rm", entry, "--repo"]
    assert egress_argv(repo, "add", entry) == ["net", "egress", "add", entry, "--repo"]
    assert egress_argv(container, "add", entry) == ["net", "egress", "add", entry, "repo-feat"]


def test_removable_entry_follows_the_scope():
    repo = EgressState("alpha", None, ())
    ctr = EgressState("alpha", "alpha-x", ())
    assert removable_entry(repo, EntryRow("a.io", "local")) == "a.io"
    assert removable_entry(repo, EntryRow("a.io", "db (legacy)")) == "a.io"
    assert removable_entry(repo, EntryRow("a.io", "config")) is None
    assert removable_entry(repo, EntryRow("a.io", "container")) is None
    assert removable_entry(ctr, EntryRow("a.io", "container")) == "a.io"
    assert removable_entry(ctr, EntryRow("a.io", "local")) is None
    assert removable_entry(ctr, EntryRow("a.io", "config")) is None


def test_egress_label_notes_source_proxy_and_redundancy():
    state = EgressState(
        "alpha",
        None,
        (EntryRow("*.x.io", "local", redundant=True), EntryRow("*.x.io", "db (legacy)")),
    )
    text = egress_label(state, state.rows[0]).plain
    assert text.startswith("*.x.io")
    assert "[local; removes both repo copies]" in text and "[proxy]" in text
    assert "(redundant)" in text


def test_the_duplicate_note_is_a_repo_scope_note_only():
    rows = (EntryRow("shared.example", "local"), EntryRow("shared.example", "db (legacy)"))
    container = EgressState("repo", "repo-feat", rows)
    assert "removes both" not in egress_label(container, rows[0]).plain
    lone = EgressState("repo", None, rows[:1])
    assert egress_label(lone, rows[0]).plain == "shared.example  [local]"


def test_proxy_tag_only_on_wildcard_rows():
    state = EgressState(
        "repo", None, (EntryRow("github.com", "config"), EntryRow("*.example.com", "local"))
    )
    assert "[proxy]" not in egress_label(state, state.rows[0]).plain
    assert egress_label(state, state.rows[1]).plain == "*.example.com  [local]  [proxy]"
