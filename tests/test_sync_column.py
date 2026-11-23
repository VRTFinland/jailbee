"""Sync comparisons through the listing and shared dashboard boundaries."""

from datetime import UTC, datetime
from io import StringIO

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
        row("parent-fallback", SyncTarget("host", "main", "h", "main", fallback=True, parent="orch"), "?", "?", "?"),
    ]


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
        assert Text(cell[:cell.index("↑")]).cell_len == 4
        assert Text(cell[:cell.index("↓")]).cell_len == 9
    assert "[yellow]pr?[/yellow]" in fields["sync"].cell(rows[3])
    assert "[dim]⑂[/dim]" in fields["sync"].cell(rows[2])


def test_legacy_sync_fields_stay_selectable_without_repeating_the_marker():
    rows = mixed_rows()
    names = ("target_diff", "ahead_count", "behind_count", "conflict")
    fields = visible_fields(NOW, rows, enabled=names)
    assert [f.name for f in fields] == list(names)
    assert [Text.from_markup(f.cell(rows[1])).plain for f in fields] == [
        "+12345 -9", "123", "2", "ok",
    ]
    output = StringIO()
    table_format.emit(
        rows[1:2], ls_field_specs(now=NOW), fmt="table", fields=",".join(names),
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


def test_sync_json_is_opt_in_and_reuses_the_existing_git_status_payload(capsys):
    import json

    rows = mixed_rows()
    specs = ls_field_specs(now=NOW, containers=rows)
    table_format.emit(rows, specs, fmt="json", fields=None, console=Console())
    payload = json.loads(capsys.readouterr().out)
    assert "sync" not in payload[0]
    assert payload[1]["git_status"]["ahead_count"] == "123"
    assert payload[1]["git_status"]["target_diff"] == "+12345 -9"
    table_format.emit(rows, specs, fmt="json", fields="sync,git_status", console=Console())
    explicit = json.loads(capsys.readouterr().out)
    assert explicit[1]["sync"] == explicit[1]["git_status"]


def test_sync_sorts_by_diff_size_and_legacy_count_sorts_remain_available():
    from jailbee.dashboard.model import RepoGroup
    from jailbee.dashboard.sorting import click_sort, sort_groups, SortSpec

    rows = mixed_rows()[:3]
    group = RepoGroup("repo", "/repo", None, rows)
    chosen = click_sort(SortSpec(), "sync", now=NOW)
    assert chosen == SortSpec("sync", desc=True)
    sorted_rows = sort_groups([group], chosen, ("sync",), now=NOW)[0].containers
    assert [c.name for c in sorted_rows] == ["pr", "host", "parent"]
    sorted_rows = sort_groups([group], SortSpec("behind_count", True), ("behind_count",), now=NOW)[0].containers
    assert [c.name for c in sorted_rows] == ["host", "pr", "parent"]


def test_sync_default_listing_keeps_submodule_diffs(tmp_path, mocker, make_cfg):
    from dataclasses import replace

    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.git_status import SubmoduleChange

    cfg = make_cfg(tmp_path)
    (tmp_path / ".gitmodules").write_text('[submodule "lib"]\npath = lib\n')
    c = mixed_rows()[0]
    c.git_status = replace(c.git_status, submodules=(SubmoduleChange("lib", 42, 7, 1234, 0, 0, "modified"),))
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
    c.git_status = replace(c.git_status, ahead_count="?", behind_count=BEHIND_UNKNOWN, target_diff="?")
    stopped = replace(c, name="stopped", git_status=None)
    fields = {f.name: f for f in visible_fields(NOW, [c, stopped], enabled=("sync",))}
    assert Text.from_markup(fields["sync"].cell(c)).plain == "    ↑? ↓• ?"
    assert Text.from_markup(fields["sync"].cell(stopped)).plain == "—"


def test_sync_count_widths_follow_only_the_visible_rows():
    rows = mixed_rows()
    [field] = visible_fields(NOW, [rows[2]], enabled=("sync",))
    assert Text.from_markup(field.cell(rows[2])).plain == "⑂   ↑12 ↓0 ✓"
