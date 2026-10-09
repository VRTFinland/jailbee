"""Tests for the dashboard details panel's content and rendering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.text import Text

from jailbee.accounts.models import ActivityEvent, AgentActivity
from jailbee.agent_status import AgentSummary
from jailbee.dashboard import details as dd
from jailbee.dashboard import model as dmodel
from jailbee.git_status import GitStatus, SubmoduleChange
from jailbee.lifecycle import ContainerInfo, ls_field_specs
from jailbee.procstat import ProcessActivity

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def _c(**kw: Any) -> ContainerInfo:
    base: dict[str, Any] = {
        "name": "alpha-feat",
        "state": "Running",
        "network": "strict",
        "ip": None,
        "memory_limit": None,
        "repo": "alpha",
    }
    base.update(kw)
    return ContainerInfo(**base)


def _value(items: list[dd.DetailItem], label: str) -> str:
    return next(i.value for i in items if i.label == label)


def _plain(markup: str) -> str:
    return Text.from_markup(markup).plain


def _text(renderable: Any, width: int) -> str:
    console = Console(record=True, width=width)
    console.print(renderable)
    return console.export_text()


def test_repo_summary(tmp_path: Path) -> None:
    g = dmodel.RepoGroup(
        "alpha",
        "/repos/alpha",
        tmp_path / "config.yaml",
        [_c(), _c(name="alpha-b", state="Stopped")],
        loose_ttl_default="30m",
        optional_mounts=("adb",),
    )
    items = dd.repo_details(g)
    assert [i.label for i in items] == ["root", "config", "containers", "loose ttl", "mounts"]
    assert _plain(_value(items, "root")) == "/repos/alpha"
    assert _plain(_value(items, "config")) == str(tmp_path / "config.yaml")
    assert _plain(_value(items, "containers")) == "1 running / 2"
    assert _plain(_value(items, "loose ttl")) == "30m"
    assert _plain(_value(items, "mounts")) == "adb"


def test_orphan_and_synthesized_repo_summaries() -> None:
    orphan = dd.repo_details(dmodel.RepoGroup("ghost", None, None, []))
    assert _plain(_value(orphan, "root")) == "orphan"
    assert _plain(_value(orphan, "config")) == "—"
    synthesized = dd.repo_details(dmodel.RepoGroup("beta", "/repos/beta", None, []))
    assert _plain(_value(synthesized, "config")) == "synthesized"
    assert _plain(_value(synthesized, "loose ttl")) == "no auto-revert"


def test_details_for_resolves_rows_and_tolerates_a_vanished_container() -> None:
    g = dmodel.RepoGroup("alpha", "/repos/alpha", None, [_c()])
    view = dd.details_for([g], dmodel.Row("container", "alpha-feat"), NOW)
    assert view is not None and view.title == "feat"
    repo_view = dd.details_for([g], dmodel.Row("repo", "alpha"), NOW)
    assert repo_view is not None and repo_view.title == "alpha"
    assert dd.details_for([g], dmodel.Row("container", "alpha-gone"), NOW) is None
    assert dd.details_for([g], dmodel.Row("repo", "gone"), NOW) is None
    assert dd.details_for([g], None, NOW) is None


def test_orphan_container_title_is_the_full_name() -> None:
    """A container with no repo or mismatched prefix shows its full name."""
    orphan = _c(name="orphan-container", repo=None)
    g = dmodel.RepoGroup("ghost", None, None, [orphan])
    view = dd.details_for([g], dmodel.Row("container", "orphan-container"), NOW)
    assert view is not None and view.title == "orphan-container"


def test_render_keeps_each_group_in_one_column_at_two_and_three_columns() -> None:
    groups = ((0, 1), (2, 3, 4, 5, 6), (7, 8, 9, 10))
    items = tuple(
        dd.DetailItem(f"k{i}:", f"v{i}", group=f"g{group_index}")
        for group_index, group in enumerate(groups)
        for i in group
    )
    view = dd.DetailsView("alpha", items)

    for width in (40, 80, 120):
        lines = _text(dd.render_details(view, None), width=width).splitlines()[1:-1]
        positions = {
            i: next(
                (row, line.index(f"k{i}:")) for row, line in enumerate(lines) if f"k{i}:" in line
            )
            for group in groups
            for i in group
        }
        for group in groups:
            assert len({positions[i][1] for i in group}) == 1
            assert [positions[i][0] for i in group] == list(
                range(positions[group[0]][0], positions[group[0]][0] + len(group))
            )


def test_capped_columns_show_ellipsis_where_that_column_overflows() -> None:
    items = tuple(dd.DetailItem(f"k{i}", f"v{i}", group=f"g{i}") for i in range(12))
    rendered = _text(dd.render_details(dd.DetailsView("alpha", items), 3), width=120)

    assert "k0" in rendered and "k1" in rendered and "k2" in rendered
    assert rendered.count("…") == 3
    assert "k6" not in rendered and "k7" not in rendered and "k8" not in rendered
    assert len(rendered.splitlines()) == 5


def test_render_flows_pairs_by_width_and_caps_rows() -> None:
    view = dd.DetailsView("alpha-feat", tuple(dd.DetailItem(f"k{i}", f"v{i}") for i in range(12)))
    wide = _text(dd.render_details(view, None), width=120).splitlines()
    narrow = _text(dd.render_details(view, None), width=40).splitlines()
    assert "alpha-feat" in wide[0]
    assert len(wide) == 4 + 2  # 3 pairs per line at 120 columns, plus the border
    assert len(narrow) == 12 + 2  # 1 pair per line at 40 columns
    capped = _text(dd.render_details(view, 4), width=40).splitlines()
    assert len(capped) == 4 + 2
    assert "…" in capped[-2]
    assert "k11" not in "\n".join(capped)


def _with_activity(**kw: Any) -> ContainerInfo:
    activity = AgentActivity("Bash  uv run pytest -x", "all [red]green[/red] <b>", 2, 1)
    summary = AgentSummary(
        "claude", "busy", NOW - timedelta(minutes=2), None, 1, activity=activity, **kw
    )
    return _c(agent_status=(summary,))


def test_activity_block_is_escaped_and_the_message_is_white() -> None:
    block = dd.activity_block(_with_activity(), NOW)

    assert [_plain(line) for line in block.lines] == [
        "busy 2m · ~2 subagents · 1 shell",
        "↳ Bash  uv run pytest -x",
    ]
    assert block.message is not None
    assert _plain(block.message) == "“all [red]green[/red] <b>”"  # shown, not interpreted
    assert block.message.startswith("[bold white]")


def test_history_is_newest_first_escaped_and_tools_dim() -> None:
    recent = (
        ActivityEvent("tool", "Read  /a.py"),
        ActivityEvent("message", "[red]x[/red] \\"),
        ActivityEvent("tool", "Bash  echo [/dim]"),
        ActivityEvent("tool", "Bash  ls"),  # the last tool: shown above, not repeated
    )
    activity = AgentActivity("Bash  ls", None, recent=recent)
    summary = AgentSummary("claude", "busy", None, None, 1, activity=activity)

    history = dd.activity_block(_c(agent_status=(summary,)), NOW).history

    assert [_plain(h) for h in history] == [
        "Bash  echo [/dim]",
        "“[red]x[/red] \\”",
        "Read  /a.py",
    ]
    assert history[0].startswith("[dim]") and history[2].startswith("[dim]")
    assert not history[1].startswith("[dim]")


def test_history_skips_the_tool_and_message_already_shown_above() -> None:
    recent = (
        ActivityEvent("message", "older"),
        ActivityEvent("tool", "Read  /a.py"),
        ActivityEvent("message", "done"),
        ActivityEvent("tool", "Bash  ls"),
    )
    busy = AgentSummary(
        "claude", "busy", None, None, 1, activity=AgentActivity("Bash  ls", "done", recent=recent)
    )
    # Idle shows no tool line, so the last tool stays in the history.
    idle = AgentSummary(
        "claude", "idle", None, None, 1, activity=AgentActivity("Bash  ls", "done", recent=recent)
    )

    on_busy = dd.activity_block(_c(agent_status=(busy,)), NOW).history
    on_idle = dd.activity_block(_c(agent_status=(idle,)), NOW).history

    assert [_plain(h) for h in on_busy] == ["Read  /a.py", "“older”"]
    assert [_plain(h) for h in on_idle] == ["Bash  ls", "Read  /a.py", "“older”"]


def test_no_activity_is_an_empty_block() -> None:
    assert dd.activity_block(_c(), NOW) == dd.ActivityBlock()
    s = AgentSummary("claude", "busy", None, None, 1)
    assert dd.activity_block(_c(agent_status=(s,)), NOW) == dd.ActivityBlock()


def test_the_first_agent_that_has_activity_speaks() -> None:
    quiet = AgentSummary("claude", "waiting", None, None, 1)
    loud = AgentSummary("codex", "busy", None, None, 1, activity=AgentActivity("Edit  x", None))
    block = dd.activity_block(_c(agent_status=(quiet, loud)), NOW)

    assert [_plain(line) for line in block.lines] == ["busy", "↳ Edit  x"]
    assert block.message is None and block.history == ()


def _group(*containers: ContainerInfo) -> dmodel.RepoGroup:
    return dmodel.RepoGroup("alpha", "/a", None, list(containers))


def test_rows_are_reserved_for_every_view_once_any_container_has_activity() -> None:
    busy, idle = _with_activity(), _c(name="alpha-idle")
    groups = [_group(busy, idle)]

    on_busy = dd.details_for(groups, dmodel.Row("container", busy.name), NOW)
    on_idle = dd.details_for(groups, dmodel.Row("container", idle.name), NOW)
    on_repo = dd.details_for(groups, dmodel.Row("repo", "alpha"), NOW)

    assert on_busy is not None and on_idle is not None and on_repo is not None
    assert (on_busy.reserve_rows, on_idle.reserve_rows, on_repo.reserve_rows) == (3, 3, 3)
    assert len(on_busy.activity) == 2 and on_busy.message is not None
    assert on_idle.activity == () and on_repo.activity == ()
    assert on_busy.base_rows == dd.DETAILS_MAX_ROWS + 3


def test_nothing_is_reserved_when_no_container_has_activity() -> None:
    plain = _c()
    view = dd.details_for([_group(plain)], dmodel.Row("container", plain.name), NOW)

    assert view is not None
    assert (view.reserve_rows, view.base_rows) == (0, dd.DETAILS_MAX_ROWS)


def _view(activity: tuple[str, ...], reserve: int = 3) -> dd.DetailsView:
    items = tuple(dd.DetailItem(f"k{i}", f"v{i}") for i in range(6))
    return dd.DetailsView("t", items, activity, reserve)


def _body(view: dd.DetailsView, max_rows: int | None, *, fixed: bool = False) -> list[str]:
    """The panel's content lines, borders removed."""
    lines = _text(dd.render_details(view, max_rows, fixed=fixed), width=120).splitlines()
    return [ln[2:-2].rstrip() for ln in lines[1:-1]]


LINES = ("busy 2m", "↳ Bash  ls", "[bold white]“done”[/bold white]")


def test_activity_follows_the_grid_under_the_panel() -> None:
    body = _body(_view(LINES), None)

    assert body[-3:] == ["busy 2m", "↳ Bash  ls", "“done”"]
    assert "k0" in body[0]


def test_a_fixed_panel_is_padded_to_exactly_its_rows_even_without_activity() -> None:
    with_lines = _body(_view(LINES), 11, fixed=True)
    without = _body(_view(()), 11, fixed=True)

    assert len(with_lines) == len(without) == 11
    assert with_lines[-3:] == ["busy 2m", "↳ Bash  ls", "“done”"]  # share kept at the bottom
    assert without[-3:] == ["", "", ""]


def test_a_cramped_panel_cuts_the_message_first_then_the_tool_and_keeps_two_grid_rows() -> None:
    four = _body(_view(LINES), 4)
    assert four[-2:] == ["busy 2m", "↳ Bash  ls"]  # message cut, grid cut to 2 rows
    assert len(four) == 4

    three = _body(_view(LINES), 3)
    assert three[-1] == "busy 2m"  # only the state line survives
    assert len(three) == 3

    two = _body(_view(LINES), 2)
    assert len(two) == 2
    assert "busy 2m" not in " ".join(two)  # the grid keeps its two rows


def test_sparse_and_filled_details_keep_label_and_value_columns_aligned() -> None:
    sparse = dd.DetailsView(
        "t",
        (dd.DetailItem("a", "one"), dd.DetailItem("b", "two")),
    )
    long_text = dd.DetailsView(
        "t",
        (
            dd.DetailItem("long label", "one value that needs truncation"),
            dd.DetailItem("b", "two"),
        ),
    )
    lines = [
        _text(dd.render_details(view, 6, fixed=True), width=80).splitlines()
        for view in (sparse, long_text)
    ]

    assert len(lines[0]) == len(lines[1]) == 8
    assert lines[0][1].index("one") == lines[1][1].index("one")
    assert lines[0][1].index(" b ") == lines[1][1].index(" b ")


def test_a_panel_without_a_reservation_renders_as_before() -> None:
    old = _text(dd.render_details(dd.DetailsView("t", _view(()).items), 8), width=120)
    new = _text(dd.render_details(_view((), reserve=0), 8), width=120)

    assert new == old


HEAD = ("busy 2m", "↳ Bash  ls")
MESSAGE = "[bold white]“done”[/bold white]"


def _full(history: int = 6, message: str | None = MESSAGE) -> dd.DetailsView:
    items = tuple(dd.DetailItem(f"k{i}", f"v{i}") for i in range(6))
    return dd.DetailsView("t", items, HEAD, 3, message, tuple(f"h{i}" for i in range(history)))


def test_uncapped_activity_runs_head_message_then_history() -> None:
    body = _body(_full(), None)

    assert body[-9:] == ["busy 2m", "↳ Bash  ls", "“done”", "h0", "h1", "h2", "h3", "h4", "h5"]


def test_details_rows_base_is_the_reservation_and_want_is_everything() -> None:
    console = Console(width=120)

    rows = dd.details_rows(_full(), console, 120)

    assert rows == dd.DetailsRows(base=dd.DETAILS_MAX_ROWS + 3, want=8 + 2 + 1 + 6)
    body = _body(_full(), rows.want, fixed=True)
    assert len(body) == rows.want
    assert body[-6:] == ["h0", "h1", "h2", "h3", "h4", "h5"]
    assert "…" not in body


def test_a_view_without_activity_wants_only_its_base() -> None:
    plain = dd.DetailsView("t", _full().items, (), 3)
    assert dd.details_rows(plain, Console(width=120), 120) == dd.DetailsRows(11, 11)
    bare = dd.DetailsView("t", _full().items)
    assert dd.details_rows(bare, Console(width=120), 120) == dd.DetailsRows(8, 8)


def test_history_that_does_not_fit_ends_in_an_ellipsis_row() -> None:
    body = _body(_full(), 8 + 2 + 1 + 3, fixed=True)

    assert body[-5:] == ["↳ Bash  ls", "“done”", "h0", "h1", "…"]


def test_a_long_message_is_wrapped_in_full() -> None:
    words = " ".join(f"w{i:02d}" for i in range(60))
    view = _full(history=0, message=f"[bold white]“{words}”[/bold white]")
    rows = dd.details_rows(view, Console(width=60), 60)

    lines = _text(dd.render_details(view, rows.want, fixed=True), width=60).splitlines()[1:-1]
    shown = " ".join(ln[2:-2] for ln in lines)

    assert rows.want > 8 + 2 + 1  # the message really wrapped
    assert len(lines) == rows.want
    assert all(f"w{i:02d}" in shown for i in range(60))
    assert "…" not in shown


def test_a_message_cut_short_ends_in_an_ellipsis() -> None:
    words = " ".join(f"w{i:02d}" for i in range(60))
    view = _full(history=0, message=f"[bold white]“{words}”[/bold white]")
    rows = dd.details_rows(view, Console(width=60), 60)

    lines = _text(dd.render_details(view, rows.want - 1, fixed=True), width=60).splitlines()
    content = [ln[2:-2].rstrip() for ln in lines[1:-1]]

    assert len(content) == rows.want - 1
    assert content[-1].endswith("…")
    assert "w59" not in " ".join(content)


def test_hostile_transcript_text_renders_literally() -> None:
    recent = (
        ActivityEvent("tool", "Bash  echo [/dim] \\"),
        ActivityEvent("message", "[red]x[/red] \\"),
        ActivityEvent("message", "last [bold]"),
        ActivityEvent("tool", "Bash  ls"),
    )
    activity = AgentActivity("Bash  ls", "last [bold]", recent=recent)
    summary = AgentSummary("claude", "busy", None, None, 1, activity=activity)
    block = dd.activity_block(_c(agent_status=(summary,)), NOW)
    view = dd.DetailsView("t", _full().items, block.lines, 3, block.message, block.history)

    body = _body(view, None)

    assert "“last [bold]”" in body
    assert "Bash  echo [/dim] \\" in body
    assert "“[red]x[/red] \\”" in body


def _panel_plain(parts: tuple[str, ...]) -> list[str]:
    return [_plain(p) for p in parts]


def test_state_label_pairs_the_glyph_with_the_name() -> None:
    from jailbee.dashboard import format as dformat

    assert dformat.state_label("Running") == "▶ Running"
    assert dformat.state_label("Stopped") == "■ Stopped"
    assert dformat.state_label("[odd]") == r"\[odd]"


def test_summary_runs_state_network_ip_github_agent_doing_in_order() -> None:
    until = NOW + timedelta(minutes=42)
    agent = AgentSummary("claude", "busy", NOW - timedelta(minutes=3), None, 1)
    c = _c(
        network="loose",
        loose_until=until,
        ip="10.0.3.7",
        pr_number=123,
        pr_author=True,
        agent_status=(agent,),
        activity=(
            ProcessActivity(comm="pytest", percent=90.0, count=3),
            ProcessActivity(comm="node", percent=5.0, count=1),
        ),
    )
    cells = {f.name: f.cell for f in ls_field_specs(now=NOW, all_repos=False)}
    summary = _panel_plain(dd.container_panel(c, NOW).summary)

    assert summary == [
        "▶ Running",
        f"● loose {_plain(cells['ttl'](c))} →{until.astimezone():%H:%M}",
        "10.0.3.7",
        f"PR {_plain(cells['pr'](c))}",
        _plain(cells["agent"](c)),
        "pytest x3, node",
    ]


def test_summary_leaves_out_what_is_absent_and_dims_strict() -> None:
    panel = dd.container_panel(_c(), NOW)
    assert _panel_plain(panel.summary) == ["▶ Running", "strict"]
    assert panel.summary[1] == "[dim]strict[/dim]"


def test_loose_without_a_deadline_reads_infinity() -> None:
    summary = _panel_plain(dd.container_panel(_c(network="loose"), NOW).summary)
    assert summary[1] == "● loose ∞"


def test_job_and_its_first_error_line_follow_the_state() -> None:
    c = _c(job_phase="failed", job_kind="new", job_error="boom [red]\nsecond line")
    first = dd.container_panel(c, NOW).summary[0]
    assert "boom [red]" in _plain(first)
    assert "second line" not in _plain(first)
    assert r"\[red]" in first


def test_git_root_row_reuses_the_table_cells() -> None:
    gs = GitStatus(
        wt="+12 -3",
        ahead_diff="+245 -18",
        ahead_count="7",
        conflict="ok",
        target_diff="+245 -18",
        behind_count="9",
        local_diff="+1 -0",
        local_count="5",
    )
    c = _c(git_status=gs, base_branch="dev")
    cells = {f.name: f.cell for f in ls_field_specs(now=NOW, all_repos=False)}
    panel = dd.container_panel(c, NOW)

    assert panel.base == "dev"
    assert panel.git is not None and len(panel.git) == 1
    root = panel.git[0]
    assert _plain(root.name) == "feat"
    # Distinct numbers, so a swapped field cannot pass on a coincidence.
    assert (root.ahead, root.behind) == (cells["ahead_count"](c), cells["behind_count"](c))
    assert (root.target, root.working) == (cells["target_diff"](c), cells["wt"](c))
    assert _plain(root.note) == f"merge {_plain(cells['conflict'](c))}"
    assert _plain(root.extra) == "host HEAD ↑5 +1 -0"


def test_submodule_rows_follow_the_root_in_order_and_keep_unknowns() -> None:
    status = GitStatus(
        wt="clean",
        ahead_diff="clean",
        ahead_count="0",
        conflict="ok",
        submodules=(
            SubmoduleChange(
                path="deps/[bold]widget",
                status="modified",
                ahead_commits=2,
                behind_commits=1,
                target_ins=7,
                target_del=3,
                wt_ins=4,
                wt_del=0,
            ),
            SubmoduleChange(
                path="second",
                status="new",
                ahead_commits=None,
                behind_commits=None,
                target_ins=None,
                target_del=None,
                wt_ins=None,
                wt_del=None,
            ),
            SubmoduleChange(path="third", status="removed"),
        ),
    )
    git = dd.container_panel(_c(git_status=status), NOW).git
    assert git is not None
    subs = git[1:]

    assert [_plain(r.name) for r in subs] == ["deps/[bold]widget", "second", "third"]
    assert r"\[bold]" in subs[0].name
    assert [(_plain(r.ahead), _plain(r.behind)) for r in subs] == [
        ("2", "1"),
        ("?", "?"),
        ("0", "0"),
    ]
    assert [_plain(r.target) for r in subs] == ["+7 -3", "?", "clean"]
    assert [_plain(r.working) for r in subs] == ["+4 -0", "?", "clean"]
    assert [_plain(r.note) for r in subs] == ["modified", "new", "removed"]
    assert all(r.extra == "" for r in subs)


def test_footer_carries_base_mode_group_created_mounts_and_resources() -> None:
    created = NOW - timedelta(days=2)
    c = _c(
        base_branch="dev",
        credential_group="[work]",
        created_at=created,
        optional_mounts=("ssh", "gpg"),
        memory_usage=1_200_000_000,
        memory_limit="4GiB",
        cpu_percent=34.0,
    )
    cells = {f.name: f.cell for f in ls_field_specs(now=NOW, all_repos=False)}
    footer = _panel_plain(dd.container_panel(c, NOW).footer)

    assert footer == [
        f"base {_plain(cells['base'](c))}",
        "clone",
        "group [work]",
        f"created 48h ago ({_plain(cells['created'](c))})",
        "mounts ssh, gpg",
        f"mem {_plain(cells['mem'](c))}",
        f"cpu {_plain(cells['cpu'](c))}",
    ]


def test_a_bare_container_panel() -> None:
    panel = dd.container_panel(_c(state="Stopped", network=None), NOW)

    assert _panel_plain(panel.summary) == ["■ Stopped"]
    assert panel.git is None
    assert panel.base == "base"
    footer = _panel_plain(panel.footer)
    assert "group inherits repo" in footer
    assert not any(part.startswith("mounts") for part in footer)


def _rich_container(subs: int = 1) -> ContainerInfo:
    status = GitStatus(
        wt="+12 -3",
        ahead_diff="+245 -18",
        ahead_count="3",
        conflict="ok",
        target_diff="+245 -18",
        behind_count="4",
        local_diff="+10 -2",
        local_count="1",
        submodules=tuple(
            SubmoduleChange(
                path=f"deps/module-{i}",
                status="modified",
                ahead_commits=2,
                behind_commits=0,
                target_ins=40,
                target_del=2,
                wt_ins=5,
                wt_del=0,
            )
            for i in range(subs)
        ),
    )
    return _c(
        git_status=status,
        base_branch="dev",
        network="loose",
        loose_until=NOW + timedelta(minutes=42),
        ip="10.0.3.7",
        pr_number=123,
        pr_author=True,
    )


def _panel_view(c: ContainerInfo, **kw: Any) -> dd.DetailsView:
    return dd.DetailsView(c.display_name, (), panel=dd.container_panel(c, NOW), **kw)


def _panel_body(
    view: dd.DetailsView, max_rows: int | None, width: int, *, fixed: bool = False
) -> list[str]:
    lines = _text(dd.render_details(view, max_rows, fixed=fixed), width=width).splitlines()
    assert all(len(ln) == width for ln in lines), "every panel line is one terminal row"
    return [ln[2:-2].rstrip() for ln in lines[1:-1]]


def _fit(subs: int | None) -> dd.ContainerPanel:
    """A panel with no git status (None) or a root row plus ``subs`` submodule rows."""
    row = dd.GitRow("r", "0", "0", "clean", "clean")
    git = None if subs is None else tuple(row for _ in range(subs + 1))
    return dd.ContainerPanel(("s",), "dev", git, ("f",))


def test_panel_fit_drops_blanks_then_submodules_then_the_footer() -> None:
    fit = dd.PanelFit
    assert dd.panel_fit(_fit(None), None) == fit(True, True, 0, 0, True)
    assert dd.panel_fit(_fit(2), 8) == fit(True, True, 2, 0, True)  # 1+2+2+1 rows + 2 blanks
    assert dd.panel_fit(_fit(2), 7) == fit(False, True, 2, 0, True)
    assert dd.panel_fit(_fit(10), 8) == fit(False, True, 3, 7, True)  # 3 shown + "+7 more"
    assert dd.panel_fit(_fit(1), 4) == fit(False, True, 0, 0, True)
    assert dd.panel_fit(_fit(None), 2) == fit(False, True, 0, 0, False)
    assert dd.panel_fit(_fit(2), 2) == fit(False, False, 0, 0, True)
    assert dd.panel_fit(_fit(2), 1) == fit(False, False, 0, 0, False)


def test_the_panel_reads_summary_git_table_footer() -> None:
    body = _panel_body(_panel_view(_rich_container()), None, 120)

    assert "▶ Running" in body[0] and "10.0.3.7" in body[0] and "PR #123" in body[0]
    assert body[1] == ""
    assert body[2].startswith("git") and "vs dev" in body[2] and "working" in body[2]
    assert body[3].startswith("feat") and "merge" in body[3] and "host HEAD" in body[3]
    assert "deps/module-0" in body[4] and "modified" in body[4]
    assert body[5] == ""
    assert body[6].startswith("base dev") and "group inherits repo" in body[6]
    # One column per fact: the diffs sit under "vs dev", the working trees under "working".
    assert body[3].index("+245 -18") == body[2].index("vs dev") == body[4].index("+40 -2")
    assert body[3].index("+12 -3") == body[2].index("working") == body[4].index("+5 -0")


def test_many_submodules_collapse_into_a_more_row_and_keep_the_footer() -> None:
    body = _panel_body(_panel_view(_rich_container(subs=10)), 8, 120, fixed=True)

    assert len(body) == 8
    assert "deps/module-2" in "\n".join(body) and "deps/module-3" not in "\n".join(body)
    assert "… +7 more submodules" in body[6]
    assert body[7].startswith("base dev")


def test_a_fixed_panel_pins_the_footer_to_the_last_grid_row() -> None:
    body = _panel_body(_panel_view(_rich_container(subs=0)), 8, 120, fixed=True)

    assert len(body) == 8
    assert body[0].startswith("▶ Running")
    assert body[7].startswith("base dev")
    assert body[4:7] == ["", "", ""]


def test_a_narrow_panel_keeps_one_row_per_line_and_drops_host_head() -> None:
    for width in (dd.DETAILS_PAIR_WIDTH, 59):
        body = _panel_body(_panel_view(_rich_container(subs=2)), 8, width, fixed=True)
        assert len(body) == 8, width
        assert "host HEAD" not in "\n".join(body), width
        assert body[-1].startswith("base"), width
    wide = _panel_body(_panel_view(_rich_container()), 8, 100, fixed=True)
    assert "host HEAD" in "\n".join(wide)


def test_the_git_table_keeps_every_column_at_every_width() -> None:
    for subs in (2, 10):
        for width in range(36, 121):
            where = f"{subs} submodules, width {width}"
            body = _panel_body(_panel_view(_rich_container(subs=subs)), 8, width, fixed=True)
            at = next(i for i, ln in enumerate(body) if ln.startswith("git"))
            header = body[at]
            assert "↑" in header and "↓" in header and "vs dev" in header, where
            assert "working" in header, where
            root = body[at + 1]
            # Distinct ahead/behind, so a swapped or squeezed column cannot pass.
            assert root[header.index("↑")] == "3", where
            assert root[header.index("↓")] == "4", where
            assert "+245 -18" in root and "+12 -3" in root, where
            assert "…" not in root[header.index("↑") :].split("merge")[0], where
            rows = [ln for ln in body[at + 2 : -1] if ln and not ln.startswith("… +")]
            assert rows, where
            for ln in rows:
                assert "+40 -2" in ln and "+5 -0" in ln, where
                assert ln[header.index("↑")] == "2", where


def test_a_hidden_submodule_block_is_hinted_in_the_header() -> None:
    body = _panel_body(_panel_view(_rich_container(subs=3)), 4, 100, fixed=True)
    assert "+3 submodules" in body[1]


def test_no_git_status_renders_one_git_line() -> None:
    body = _panel_body(_panel_view(_c()), None, 80)
    assert body == [body[0], "", "git —", "", body[-1]]
    assert body[0].startswith("▶ Running") and body[-1].startswith("base")


def test_container_written_text_renders_literally() -> None:
    status = GitStatus(
        wt="clean",
        ahead_diff="clean",
        ahead_count="0",
        conflict="ok",
        submodules=(SubmoduleChange(path="deps/[bold]x", status="modified", target_ins=1),),
    )
    c = _c(
        git_status=status,
        job_phase="failed",
        job_kind="new",
        job_error="boom [red]",
        credential_group="[work]",
        activity=(ProcessActivity(comm="[/dim]", percent=10.0, count=1),),
    )
    text = "\n".join(_panel_body(_panel_view(c), None, 160))

    for literal in ("deps/[bold]x", "boom [red]", "group [work]", "[/dim]"):
        assert literal in text, literal


def test_a_cramped_panel_keeps_the_summary_and_the_activity() -> None:
    view = _panel_view(_rich_container(), activity=LINES[:2], reserve_rows=3)
    body = _panel_body(view, 4, 120, fixed=True)

    assert len(body) == 4
    assert body[0].startswith("▶ Running")
    assert body[-2:] == ["busy 2m", "↳ Bash  ls"]


def test_details_for_a_container_carries_its_panel() -> None:
    c = _rich_container()
    view = dd.details_for(
        [dmodel.RepoGroup("alpha", "/a", None, [c])], dmodel.Row("container", c.name), NOW
    )

    assert view is not None and view.items == ()
    assert view.panel == dd.container_panel(c, NOW)
