"""Tests for the dashboard details panel's content and rendering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from rich.console import Console
from rich.text import Text

from jailbee import dashboard
from jailbee import dashboard_details as dd
from jailbee.agent_status import AgentSummary
from jailbee.git_status import GitStatus
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
        ahead_count="3",
        conflict="ok",
        target_diff="+245 -18",
        behind_count="1",
        local_diff="+1 -0",
        local_count="2",
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


def test_absent_values_render_as_a_dash() -> None:
    items = dd.container_details(_c(), NOW)
    for label in ("git", "agent", "doing", "github", "mounts"):
        assert _plain(_value(items, label)) == "—", label


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


def test_repo_summary(tmp_path) -> None:
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
    assert view is not None and view.title == "alpha-feat"
    repo_view = dd.details_for([g], dashboard.Row("repo", "alpha"), NOW)
    assert repo_view is not None and repo_view.title == "alpha"
    assert dd.details_for([g], dashboard.Row("container", "alpha-gone"), NOW) is None
    assert dd.details_for([g], dashboard.Row("repo", "gone"), NOW) is None
    assert dd.details_for([g], None, NOW) is None


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
