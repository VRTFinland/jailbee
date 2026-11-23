"""Sync comparisons through the listing and shared dashboard boundaries."""

from datetime import UTC, datetime
from io import StringIO

import pytest
from rich.console import Console
from rich.text import Text

from jailbee import table_format
from jailbee.dashboard.columns import visible_fields
from jailbee.git_status import GitStatus
from jailbee.host_target import SyncTarget
from jailbee.lifecycle import ContainerInfo, ls_field_specs

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def row(name, target, ahead, behind, diff):
    return ContainerInfo(
        name=name,
        state="Running",
        network=None,
        ip=None,
        memory_limit=None,
        git_status=GitStatus(
            wt="clean",
            ahead_diff="clean",
            ahead_count=ahead,
            behind_count=behind,
            conflict="ok",
            target_diff=diff,
            sync_target=target,
        ),
    )


def mixed_rows():
    return [
        row("host", SyncTarget("host", "main", "h", "main"), "1", "123", "+1 -0"),
        row("pr", SyncTarget("pr", "PR #7", "p", "pr"), "123", "2", "+12345 -9"),
        row("parent", SyncTarget("parent", "orch", "o", "", parent="orch"), "12", "0", "clean"),
        row("pr-fallback", SyncTarget("host", "main", "h", "main", fallback=True), "0", "12", "?"),
        row(
            "parent-fallback",
            SyncTarget("host", "main", "h", "main", fallback=True, parent="orch"),
            "?",
            "?",
            "?",
        ),
    ]


@pytest.mark.parametrize("frontend", ["tui", "qt", "web"])
@pytest.mark.parametrize("fold_saved", [False, True])
def test_saved_previous_column_set_migrates_once(frontend, fold_saved):
    from sqlmodel import SQLModel, create_engine

    from jailbee.dashboard.columns import seed_view_state
    from jailbee.db.view_prefs import ViewState, load_view_state, save_view_state

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    columns = ("name", "target_diff", "ahead_count", "behind_count", "host_ahead", "host_behind", "wt")
    save_view_state(engine, frontend, ViewState(columns=columns, columns_version=3))
    if fold_saved:
        save_view_state(engine, frontend, ViewState(columns=columns, folded=frozenset({"repo"})))
    notices = []
    state = seed_view_state(engine, frontend, on_migration=notices.append)
    assert state.columns == ("name", "sync", "host_ahead", "host_behind", "wt")
    assert state.folded == (frozenset({"repo"}) if fold_saved else frozenset())
    assert state.columns_version == 4
    assert load_view_state(engine, frontend) == state
    assert len(notices) == 1
    assert "target_diff" in notices[0] and "sync" in notices[0]
    save_view_state(engine, frontend, ViewState(columns=(*state.columns, "ahead_count")))
    assert seed_view_state(engine, frontend).columns == (*state.columns, "ahead_count")
    engine.dispose()


def test_first_launch_explicit_dashboard_fields_stay_legacy(mocker):
    from sqlmodel import SQLModel, create_engine

    from jailbee.dashboard.columns import seed_view_state
    from jailbee.global_config import GlobalConfig

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    fields = ["name", "target_diff", "ahead_count", "behind_count"]
    mocker.patch("jailbee.dashboard.model.load_global_config", return_value=(GlobalConfig(dashboard={"fields": fields}), []))
    assert seed_view_state(engine, "tui").columns == tuple(fields)
    assert seed_view_state(engine, "tui").columns == tuple(fields)
    engine.dispose()


def test_dashboard_sync_aligns_markers_counts_and_diff_across_visible_rows():
    rows = mixed_rows()
    fields = {f.name: f for f in visible_fields(NOW, rows)}
    assert "sync" in fields
    assert not {"target_diff", "ahead_count", "behind_count"} & fields.keys()
    assert fields["sync"].header == "SYNC"
    cells = [Text.from_markup(fields["sync"].cell(c)).plain for c in rows]
    assert cells == [
        "    ↑  1 ↓123 +1 -0",
        "pr  ↑123 ↓  2 +12345 -9",
        "⑂   ↑ 12 ↓  0 ✓",
        "pr? ↑  0 ↓ 12 ?",
        "⑂?  ↑  ? ↓  ? ?",
    ]
    for cell in cells:
        assert Text(cell[: cell.index("↑")]).cell_len == 4
        assert Text(cell[: cell.index("↓")]).cell_len == 9
    assert "[yellow]pr?[/yellow]" in fields["sync"].cell(rows[3])
    assert "[dim]⑂[/dim]" in fields["sync"].cell(rows[2])


def test_legacy_sync_fields_stay_selectable_without_repeating_the_marker():
    rows = mixed_rows()
    names = ("target_diff", "ahead_count", "behind_count", "conflict")
    fields = visible_fields(NOW, rows, enabled=names)
    assert [f.name for f in fields] == list(names)
    assert [Text.from_markup(f.cell(rows[1])).plain for f in fields] == [
        "+12345 -9",
        "123",
        "2",
        "ok",
    ]
    output = StringIO()
    table_format.emit(
        rows[1:2],
        ls_field_specs(now=NOW),
        fmt="table",
        fields=",".join(names),
        console=Console(file=output, width=100),
    )
    assert "pr" not in output.getvalue()


def test_ls_sync_uses_the_visible_rows_for_count_alignment(tmp_path, mocker, make_cfg):
    from typer.testing import CliRunner

    from jailbee.cli import app

    rows = mixed_rows()
    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.lifecycle.list_containers", return_value=rows)
    mocker.patch("jailbee.incus.Incus")
    result = CliRunner().invoke(app, ["ls", "--fields", "name,sync"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "pr  ↑123 ↓  2 +12345 -9" in result.output
    assert "    ↑  1 ↓123 +1 -0" in result.output


def test_sync_is_table_only_and_git_status_remains_the_json_boundary(capsys):
    import json

    rows = mixed_rows()
    specs = ls_field_specs(now=NOW, containers=rows)
    table_format.emit(rows, specs, fmt="json", fields=None, console=Console())
    payload = json.loads(capsys.readouterr().out)
    assert "sync" not in payload[0]
    assert payload[1]["git_status"]["ahead_count"] == "123"
    assert payload[1]["git_status"]["target_diff"] == "+12345 -9"
    import typer

    with pytest.raises(typer.BadParameter, match="sync"):
        table_format.emit(rows, specs, fmt="json", fields="sync,git_status", console=Console())


def test_ls_fit_keeps_sync_until_after_base_and_merge(tmp_path, mocker, make_cfg):
    from typer.testing import CliRunner

    from jailbee.cli import app

    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.lifecycle.list_containers", return_value=mixed_rows())
    mocker.patch("jailbee.incus.Incus")
    output = StringIO()
    mocker.patch("jailbee.tui.console", Console(file=output, width=30, force_terminal=True, color_system=None))
    result = CliRunner().invoke(app, ["ls"])
    assert result.exit_code == 0, result.output
    notice = " ".join(output.getvalue().split()).split("hidden to fit:")[1]
    assert notice.index("BASE") < notice.index("SYNC")
    assert notice.index("MERGE") < notice.index("SYNC")


def test_sync_sorts_by_diff_size_and_legacy_count_sorts_remain_available():
    from jailbee.dashboard.model import RepoGroup
    from jailbee.dashboard.sorting import SortSpec, click_sort, sort_groups

    rows = mixed_rows()[:3]
    group = RepoGroup("repo", "/repo", None, rows)
    chosen = click_sort(SortSpec(), "sync", now=NOW)
    assert chosen == SortSpec("sync", desc=True)
    sorted_rows = sort_groups([group], chosen, ("sync",), now=NOW)[0].containers
    assert [c.name for c in sorted_rows] == ["pr", "host", "parent"]
    sorted_rows = sort_groups([group], SortSpec("behind_count", True), ("behind_count",), now=NOW)[
        0
    ].containers
    assert [c.name for c in sorted_rows] == ["host", "pr", "parent"]


def test_sync_default_listing_keeps_submodule_diffs(tmp_path, mocker, make_cfg):
    from dataclasses import replace

    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.git_status import SubmoduleChange

    cfg = make_cfg(tmp_path)
    (tmp_path / ".gitmodules").write_text('[submodule "lib"]\npath = lib\n')
    c = mixed_rows()[0]
    c.git_status = replace(
        c.git_status, submodules=(SubmoduleChange("lib", 42, 7, 1234, 0, 0, "modified"),)
    )
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[c])
    mocker.patch("jailbee.incus.Incus")
    result = CliRunner().invoke(app, ["ls", "--fields", "name,sync"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "↑   1 ↓123 +1 -0" in result.output
    assert "↑1234 ↓  0 +42 -7" in result.output


def test_sync_preserves_missing_target_and_unprobed_status():
    from dataclasses import replace

    from jailbee.git_status import BEHIND_UNKNOWN

    c = mixed_rows()[0]
    c.git_status = replace(
        c.git_status, ahead_count="?", behind_count=BEHIND_UNKNOWN, target_diff="?"
    )
    stopped = replace(c, name="stopped", git_status=None)
    fields = {f.name: f for f in visible_fields(NOW, [c, stopped], enabled=("sync",))}
    assert Text.from_markup(fields["sync"].cell(c)).plain == "    ↑? ↓• ?"
    assert Text.from_markup(fields["sync"].cell(stopped)).plain == "—"


def test_sync_count_widths_follow_only_unfolded_repo_rows():
    from jailbee.dashboard.model import RepoGroup
    from jailbee.dashboard.tui.fleet import entry_line, table_model

    rows = mixed_rows()
    groups = [RepoGroup("wide", "/wide", None, rows[:2]), RepoGroup("small", "/small", None, rows[2:3])]

    def rendered(folded):
        model = table_model(
            groups, now=NOW, enabled=("name", "sync"), folded=folded,
            column_widths=None, shown_columns=None, column_offset=0,
            hidden_by_preferences=False, width=100,
        )
        entry = next(entry for entry in model.entries if entry.container is rows[2])
        return entry_line(entry, model.geometry, folded, selected=False, width=100).plain

    assert "⑂   ↑ 12 ↓  0 ✓" in rendered(frozenset())
    assert "⑂   ↑12 ↓0 ✓" in rendered(frozenset({"wide"}))
