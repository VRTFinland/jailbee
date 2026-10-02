"""Tests for the dashboard's scoped egress panel."""

from __future__ import annotations

from rich.console import Console

from jailbee import egress_scope
from jailbee.dashboard_egress import (
    EgressState,
    egress_argv,
    move_egress,
    removable_entry,
    render_egress,
    replace_egress_rows,
)
from jailbee.egress_scope import EntryRow


def test_empty_panel_and_argv(make_cfg, tmp_path, mocker):
    from jailbee.dashboard_egress_data import load_egress_rows

    console = Console()
    with console.capture() as captured:
        console.print(render_egress(EgressState("repo", None, ()), can_add=True, can_rm=False))
    assert "No egress entries" in captured.get()
    mocker.patch("jailbee.dashboard_egress_data.load_repo_config", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.dashboard_egress_data.get_engine")
    classify = mocker.patch("jailbee.dashboard_egress_data.classify_sources", return_value=[])
    assert load_egress_rows(tmp_path, mocker.Mock(), None) == ()
    assert classify.call_args.kwargs == {"container": None}


def test_cursor_clamps_scrolls_and_replacement_preserves_selection():
    rows = tuple(EntryRow(f"host{i}.example", "config") for i in range(12))
    state = EgressState("repo", None, rows)
    assert move_egress(state, 100).index == 11
    assert move_egress(state, -100).index == 0
    state = move_egress(state, 5)
    changed = replace_egress_rows(state, (rows[5], rows[0]))
    assert changed.index == 0
    assert replace_egress_rows(state, rows[:2]).index == 1
    console = Console()
    with console.capture() as captured:
        console.print(render_egress(move_egress(state, 6), can_add=True, can_rm=False))
    assert "↓" in captured.get()


def test_loader_classifies_scoped_rows_and_preserves_redundant_sources(make_cfg, tmp_path, mocker):
    from jailbee.dashboard_egress_data import load_egress_rows

    cfg = make_cfg(tmp_path, egress_allow=["config.example", "shared.example", "shared.example"])
    legacy = mocker.Mock(entry="shared.example")
    session = mocker.MagicMock()
    session.exec.return_value.all.return_value = [legacy]
    local = mocker.patch("jailbee.egress_scope.local_entries", return_value=["shared.example"])
    incus = mocker.MagicMock()
    incus.config_get.return_value = '["container.example", "shared.example"]'
    mocker.patch("jailbee.dashboard_egress_data.load_repo_config", return_value=cfg)
    mocker.patch("jailbee.dashboard_egress_data.get_engine")
    mocker.patch(
        "jailbee.dashboard_egress_data.Session"
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


def test_remove_rules_and_explicit_argv():
    entry = "example.org:443"
    assert removable_entry(EgressState("repo", None, (EntryRow(entry, "config"),))) is None
    for source in ("local", "db (legacy)"):
        state = EgressState("repo", None, (EntryRow(entry, source, True),))
        assert removable_entry(state) == entry
        assert egress_argv(state, "rm", entry) == ["net", "egress", "rm", entry, "--repo"]
    assert removable_entry(EgressState("repo", "repo-feat", (EntryRow(entry, "local"),))) is None
    container = EgressState("repo", "repo-feat", (EntryRow(entry, "container", True),))
    assert removable_entry(container) == entry
    assert egress_argv(container, "add", entry) == ["net", "egress", "add", entry, "repo-feat"]
    assert egress_argv(EgressState("repo", None, ()), "add", entry) == [
        "net",
        "egress",
        "add",
        entry,
        "--repo",
    ]


def test_duplicate_repo_copies_explain_both_are_removed():
    state = EgressState(
        "repo",
        None,
        (EntryRow("shared.example", "local"), EntryRow("shared.example", "db (legacy)")),
    )
    console = Console()
    with console.capture() as captured:
        console.print(render_egress(state, can_add=True, can_rm=True))
    assert "removes both repo copies" in captured.get()


def test_proxy_tag_only_on_wildcard_rows():
    state = EgressState(
        "repo", None, (EntryRow("github.com", "config"), EntryRow("*.example.com", "local"))
    )
    console = Console(width=100)
    with console.capture() as captured:
        console.print(render_egress(state, can_add=True, can_rm=True))
    lines = captured.get().splitlines()
    plain = next(line for line in lines if "github.com" in line)
    wild = next(line for line in lines if "*.example.com" in line)
    assert "[proxy]" not in plain
    assert "[local]  [proxy]" in wild
