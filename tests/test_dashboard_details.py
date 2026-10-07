"""Tests for the dashboard details panel's content and rendering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.text import Text

from jailbee import dashboard
from jailbee import dashboard_details as dd
from jailbee.accounts.models import AgentActivity
from jailbee.agent_status import AgentSummary
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


def test_container_rows_are_one_per_concept_in_priority_order() -> None:
    items = dd.container_details(_c(), NOW)
    assert [i.label for i in items] == [
        "state",
        "network",
        "git",
        "agent",
        "doing",
        "resources",
        "mode / base",
        "github",
        "group",
        "created",
        "mounts",
    ]


def test_values_reuse_the_table_cells() -> None:
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
    c = _c(
        git_status=gs,
        pr_number=142,
        pr_author=True,
        memory_usage=1_200_000_000,
        memory_limit="4GiB",
        base_branch="dev",
        cpu_percent=34.0,
    )
    cells = {f.name: f.cell for f in ls_field_specs(now=NOW, all_repos=False)}
    items = dd.container_details(c, NOW)
    for name, label in [
        ("wt", "git"),
        ("ahead_count", "git"),
        ("behind_count", "git"),
        ("target_diff", "git"),
        ("conflict", "git"),
        ("local_diff", "git"),
        ("local_count", "git"),
        ("mem", "resources"),
        ("cpu", "resources"),
        ("pr", "github"),
        ("base", "mode / base"),
        ("state", "state"),
    ]:
        assert cells[name](c) in _value(items, label), (name, label)
    # Distinct counts, so a swapped arrow cannot pass on a coincidence.
    git = _plain(_value(items, "git"))
    assert "↑7" in git and "↓9" in git and "local ↑5" in git


def test_absent_values_render_as_a_dash() -> None:
    items = dd.container_details(_c(), NOW)
    for label in ("git", "agent", "doing", "github", "mounts"):
        assert _plain(_value(items, label)) == "—", label


def test_changed_submodules_follow_git_with_escaped_paths_and_stats() -> None:
    status = GitStatus(
        wt="clean",
        ahead_diff="clean",
        ahead_count="0",
        conflict="ok",
        submodules=(
            SubmoduleChange(
                path="deps/" + "segment/" * 16 + "[bold]widget",
                status="modified",
                ahead_commits=2,
                behind_commits=1,
                target_ins=7,
                target_del=3,
                wt_ins=4,
                wt_del=0,
            ),
        ),
    )
    items = dd.container_details(_c(git_status=status), NOW)

    assert [item.label for item in items][2:7] == [
        "git",
        "submodule",
        "commits",
        "target +/-",
        "working +/-",
    ]
    assert _plain(items[3].value) == "deps/" + "segment/" * 16 + "[bold]widget"
    assert r"\[bold]widget" in items[3].value
    assert [_plain(item.value) for item in items[4:7]] == ["modified · ↑2 ↓1", "+7 -3", "+4 -0"]
    rendered = _text(dd.render_details(dd.DetailsView("alpha", tuple(items)), None), width=120)
    assert "modified" in rendered and "↑2 ↓1" in rendered
    assert "+7 -3" in rendered and "+4 -0" in rendered


def test_submodule_missing_status_and_empty_changes_add_no_rows() -> None:
    assert [i.label for i in dd.container_details(_c(), NOW)].count("submodule") == 0
    empty = GitStatus(wt="clean", ahead_diff="clean", ahead_count="0", conflict="ok")
    assert [i.label for i in dd.container_details(_c(git_status=empty), NOW)].count(
        "submodule"
    ) == 0


def test_multiple_submodules_preserve_order_and_unknown_stats() -> None:
    status = GitStatus(
        wt="clean",
        ahead_diff="clean",
        ahead_count="0",
        conflict="ok",
        submodules=(
            SubmoduleChange(
                path="first",
                status="new",
                ahead_commits=None,
                behind_commits=None,
                target_ins=None,
                target_del=None,
                wt_ins=None,
                wt_del=None,
            ),
            SubmoduleChange(
                path="second",
                status="removed",
                ahead_commits=0,
                behind_commits=0,
                target_ins=0,
                target_del=0,
                wt_ins=0,
                wt_del=0,
            ),
        ),
    )
    items = dd.container_details(_c(git_status=status), NOW)
    rows = [i for i in items if i.label == "submodule"]

    assert [_plain(row.value) for row in rows] == ["first", "second"]
    assert [_plain(i.value) for i in items if i.label == "commits"] == [
        "new · ↑? ↓?",
        "removed · ↑0 ↓0",
    ]
    assert [_plain(i.value) for i in items if i.label == "target +/-"] == ["?", "clean"]
    assert [_plain(i.value) for i in items if i.label == "working +/-"] == ["?", "clean"]


def test_submodule_rows_obey_narrow_capped_grid_rendering() -> None:
    status = GitStatus(
        wt="clean",
        ahead_diff="clean",
        ahead_count="0",
        conflict="ok",
        submodules=tuple(
            SubmoduleChange(
                path=f"deps/module-{index}",
                status="modified",
                ahead_commits=index,
                behind_commits=0,
                target_ins=2,
                target_del=1,
                wt_ins=3,
                wt_del=0,
            )
            for index in range(5)
        ),
    )
    view = dd.DetailsView("alpha-feat", tuple(dd.container_details(_c(git_status=status), NOW)))
    rendered = _text(dd.render_details(view, 8), width=120)
    capped = _text(dd.render_details(view, 8), width=60)

    assert "deps/module-0" in rendered and "↑0 ↓0" in rendered
    assert "+2 -1" in rendered and "+3 -0" in rendered
    assert len(capped.splitlines()) == 10
    assert "…" in capped
    assert "deps/module-4" not in capped


def test_loose_network_carries_ttl_until_and_ip() -> None:
    until = NOW + timedelta(minutes=12)
    c = _c(network="loose", loose_until=until, ip="10.0.3.7")
    ttl = {f.name: f.cell for f in ls_field_specs(now=NOW, all_repos=False)}["ttl"](c)
    value = _plain(_value(dd.container_details(c, NOW), "network"))
    assert value == f"loose ({ttl}, until {until.astimezone():%H:%M}) · 10.0.3.7"


def test_doing_lists_every_process_and_escapes_markup() -> None:
    names = ("node", "[bold]x", "pytest", "git")
    acts = tuple(ProcessActivity(comm=n, percent=10.0, count=1) for n in names)
    value = _value(dd.container_details(_c(activity=acts), NOW), "doing")
    assert _plain(value) == "node, [bold]x, pytest, git"


def test_failed_job_shows_its_escaped_first_error_line() -> None:
    c = _c(job_phase="failed", job_kind="new", job_error="boom [red]\nsecond line")
    value = _plain(_value(dd.container_details(c, NOW), "state"))
    assert "boom [red]" in value
    assert "second line" not in value


def test_agent_row_is_the_full_form_not_the_compact_one() -> None:
    s = AgentSummary(agent="claude", state="waiting", since=None, waiting_for=None, count=1)
    value = _plain(_value(dd.container_details(_c(agent_status=(s,)), NOW), "agent"))
    assert value == "claude: waiting"


def test_group_says_when_it_inherits_and_escapes_a_name() -> None:
    assert _plain(_value(dd.container_details(_c(), NOW), "group")) == "inherits repo"
    named = _c(credential_group="[work]")
    assert _plain(_value(dd.container_details(named, NOW), "group")) == "[work]"


def test_repo_summary(tmp_path: Path) -> None:
    g = dashboard.RepoGroup(
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
    orphan = dd.repo_details(dashboard.RepoGroup("ghost", None, None, []))
    assert _plain(_value(orphan, "root")) == "orphan"
    assert _plain(_value(orphan, "config")) == "—"
    synthesized = dd.repo_details(dashboard.RepoGroup("beta", "/repos/beta", None, []))
    assert _plain(_value(synthesized, "config")) == "synthesized"
    assert _plain(_value(synthesized, "loose ttl")) == "no auto-revert"


def test_details_for_resolves_rows_and_tolerates_a_vanished_container() -> None:
    g = dashboard.RepoGroup("alpha", "/repos/alpha", None, [_c()])
    view = dd.details_for([g], dashboard.Row("container", "alpha-feat"), NOW)
    assert view is not None and view.title == "feat"
    repo_view = dd.details_for([g], dashboard.Row("repo", "alpha"), NOW)
    assert repo_view is not None and repo_view.title == "alpha"
    assert dd.details_for([g], dashboard.Row("container", "alpha-gone"), NOW) is None
    assert dd.details_for([g], dashboard.Row("repo", "gone"), NOW) is None
    assert dd.details_for([g], None, NOW) is None


def test_orphan_container_title_is_the_full_name() -> None:
    """A container with no repo or mismatched prefix shows its full name."""
    orphan = _c(name="orphan-container", repo=None)
    g = dashboard.RepoGroup("ghost", None, None, [orphan])
    view = dd.details_for([g], dashboard.Row("container", "orphan-container"), NOW)
    assert view is not None and view.title == "orphan-container"


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


def test_activity_lines_are_escaped_and_the_message_is_dim() -> None:
    lines = dd.activity_lines(_with_activity(), NOW)

    assert [_plain(line) for line in lines] == [
        "busy 2m · ~2 subagents · 1 shell",
        "↳ Bash  uv run pytest -x",
        "“all [red]green[/red] <b>”",  # markup is shown, not interpreted
    ]
    assert lines[2].startswith("[dim]")


def test_no_activity_is_no_lines() -> None:
    assert dd.activity_lines(_c(), NOW) == ()
    s = AgentSummary("claude", "busy", None, None, 1)
    assert dd.activity_lines(_c(agent_status=(s,)), NOW) == ()


def test_the_first_agent_that_has_activity_speaks() -> None:
    quiet = AgentSummary("claude", "waiting", None, None, 1)
    loud = AgentSummary("codex", "busy", None, None, 1, activity=AgentActivity("Edit  x", None))
    lines = dd.activity_lines(_c(agent_status=(quiet, loud)), NOW)

    assert [_plain(line) for line in lines] == ["busy", "↳ Edit  x"]


def _group(*containers: ContainerInfo) -> dashboard.RepoGroup:
    return dashboard.RepoGroup("alpha", "/a", None, list(containers))


def test_rows_are_reserved_for_every_view_once_any_container_has_activity() -> None:
    busy, idle = _with_activity(), _c(name="alpha-idle")
    groups = [_group(busy, idle)]

    on_busy = dd.details_for(groups, dashboard.Row("container", busy.name), NOW)
    on_idle = dd.details_for(groups, dashboard.Row("container", idle.name), NOW)
    on_repo = dd.details_for(groups, dashboard.Row("repo", "alpha"), NOW)

    assert on_busy is not None and on_idle is not None and on_repo is not None
    assert (on_busy.reserve_rows, on_idle.reserve_rows, on_repo.reserve_rows) == (3, 3, 3)
    assert len(on_busy.activity) == 3
    assert on_idle.activity == () and on_repo.activity == ()
    assert on_busy.max_rows == dd.DETAILS_MAX_ROWS + 3


def test_nothing_is_reserved_when_no_container_has_activity() -> None:
    plain = _c()
    view = dd.details_for([_group(plain)], dashboard.Row("container", plain.name), NOW)

    assert view is not None
    assert (view.reserve_rows, view.max_rows) == (0, dd.DETAILS_MAX_ROWS)


def _view(activity: tuple[str, ...], reserve: int = 3) -> dd.DetailsView:
    items = tuple(dd.DetailItem(f"k{i}", f"v{i}") for i in range(6))
    return dd.DetailsView("t", items, activity, reserve)


def _body(view: dd.DetailsView, max_rows: int | None, *, fixed: bool = False) -> list[str]:
    """The panel's content lines, borders removed."""
    lines = _text(dd.render_details(view, max_rows, fixed=fixed), width=120).splitlines()
    return [ln[2:-2].rstrip() for ln in lines[1:-1]]


LINES = ("busy 2m", "↳ Bash  ls", "[dim]“done”[/dim]")


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
