from datetime import datetime

from jailbee import dashboard
from jailbee.config import ColumnConfig
from jailbee.dashboard import RepoGroup
from jailbee.lifecycle import ContainerInfo
from jailbee.qtui import model as m


def _container():
    return ContainerInfo(
        name="p-foo", state="Running", network="strict", ip="10.0.0.5", memory_limit="2GB", repo="p"
    )


def _ci(name: str, repo: str, state: str = "Running") -> ContainerInfo:
    """Helper to construct a container for testing. Mirrors test_dashboard._ci."""
    return ContainerInfo(
        name=name,
        state=state,
        network="strict",
        ip=None,
        memory_limit=None,
        repo=repo,
    )


def test_container_cells_are_plain_no_rich_markup():
    c = _container()
    fields = dashboard.visible_fields(datetime.now().astimezone(), [c])
    cells = m.container_cells(c, fields)
    assert len(cells) == len(fields)
    # Plain values only — no Rich markup tags leak through.
    assert all("[" not in cell for cell in cells)
    # NAME uses display_name (prefix stripped).
    assert cells[0] == "foo"


def test_container_cells_none_renders_cell_placeholder():
    c = ContainerInfo(
        name="p-foo", state="Stopped", network=None, ip=None, memory_limit=None, repo="p"
    )
    # IP is no longer a default column in the dashboard, so select it explicitly
    # to test that fields with None values fall back to their FieldSpec.cell placeholder.
    fields = dashboard.visible_fields(
        datetime.now().astimezone(), [c], ColumnConfig(fields=["ip", "network"])
    )
    cells = m.container_cells(c, fields)
    by_name = dict(zip([f.name for f in fields], cells, strict=True))
    # These fields fall back to FieldSpec.cell's own placeholder, not "".
    assert by_name["ip"] == "-"
    assert by_name["network"] == "-"  # non-loose (None) -> plain "-"
    assert all(isinstance(cell, str) for cell in cells)


def test_container_cells_mem_is_human_text_not_dict():
    c = ContainerInfo(
        name="p-foo",
        state="Running",
        network="strict",
        ip="10.0.0.5",
        memory_limit="2GB",
        repo="p",
    )
    c.memory_usage = 500_000_000
    fields = dashboard.visible_fields(datetime.now().astimezone(), [c])
    cells = m.container_cells(c, fields)
    by_name = dict(zip([f.name for f in fields], cells, strict=True))
    mem_cell = by_name["mem"]
    assert "{" not in mem_cell
    assert "usage" not in mem_cell
    assert "/" in mem_cell  # "<used> / <limit>" style
    assert "2GB" in mem_cell


def test_group_header_normal_and_orphan():
    normal = RepoGroup("p", "/repo", None, [])
    assert m.group_header(normal) == ("p", False)
    orphan = RepoGroup("q", None, None, [])
    assert m.group_header(orphan) == ("q  (orphan)", True)


def test_column_headers():
    c = _container()
    fields = dashboard.visible_fields(datetime.now().astimezone(), [c])
    headers = m.column_headers(fields)
    assert headers[0] == "NAME"
    assert "STATE" in headers


def test_state_colors_are_hex_strings():
    from jailbee.qtui.model import STATE_COLORS

    assert STATE_COLORS["Running"].startswith("#")
    assert set(STATE_COLORS) >= {"Running", "Stopped", "Frozen"}


def test_card_content_splits_name_state_and_keeps_fields_in_order():
    from jailbee.dashboard import visible_fields
    from jailbee.qtui.model import CardField, card_content

    c = _ci("gisgro-feat", "gisgro", state="Running")
    now = datetime.now().astimezone()
    fields = visible_fields(now, [c])

    cc = card_content(c, fields)

    assert cc.name == c.display_name
    assert cc.state == "Running"
    # every visible field except name/state is present, in order, by name+header
    assert all(isinstance(f, CardField) for f in cc.fields)
    names = [f.name for f in cc.fields]
    assert "name" not in names and "state" not in names
    assert "network" in names and "base" in names


# Task 2: Pure derivation helpers


def _cc(**named):
    """Build a CardContent from name/state kwargs + field name->value pairs."""
    from jailbee.qtui.model import CardContent, CardField

    name = named.pop("name", "foo")
    state = named.pop("state", "Running")
    fields = [CardField(n, n.upper(), v) for n, v in named.items()]
    return CardContent(name=name, state=state, fields=fields)


def test_git_segments_empty_when_clean():
    from jailbee.qtui.model import git_segments, is_git_clean

    cc = _cc(wt="clean", ahead_diff="clean", ahead_count="0", conflict="ok")
    assert git_segments(cc) == []
    assert is_git_clean(cc) is True


def test_git_segments_reports_dirty_pieces():
    from jailbee.qtui.model import git_segments, is_git_clean

    cc = _cc(wt="+12 -3", ahead_diff="+245 -18", ahead_count="3", conflict="ok")
    segs = git_segments(cc)
    kinds = [k for _, k in segs]
    assert ("↑3", "ahead") in segs
    assert "diff" in kinds
    assert is_git_clean(cc) is False


def test_git_segments_flags_conflict():
    from jailbee.qtui.model import git_segments

    cc = _cc(wt="clean", ahead_diff="clean", ahead_count="0", conflict="conflict")
    assert ("merge conflict", "conflict") in git_segments(cc)


def test_compact_meta_orders_mode_base_network_and_drops_missing():
    from jailbee.qtui.model import compact_meta

    cc = _cc(mode="clone", base="main", network="loose (12m)")
    assert compact_meta(cc) == ["clone", "main", "loose (12m)"]
    cc2 = _cc(mode="clone", network="strict")  # no base
    assert compact_meta(cc2) == ["clone", "strict"]


def test_grid_rows_fold_git_into_one_row_and_drop_placeholders():
    from jailbee.qtui.model import grid_rows

    cc = _cc(
        mode="clone",
        ip="-",
        network="strict",
        wt="clean",
        ahead_diff="clean",
        ahead_count="0",
        conflict="ok",
    )
    rows = dict(grid_rows(cc))
    assert "ip" not in {k.lower() for k in rows}  # placeholder "-" dropped
    assert rows.get("GIT") == "clean"
    assert "WT" not in rows and "MERGE" not in rows  # git folded, not per-field


def test_job_badge_none_without_a_job():
    from jailbee.qtui.model import CardContent, job_badge

    cc = CardContent(name="feat", state="Running", fields=[])
    assert job_badge(cc) is None


def test_job_badge_none_for_placeholder_values():
    from jailbee.qtui.model import CardContent, CardField, job_badge

    cc = CardContent(name="feat", state="Running", fields=[CardField("job", "JOB", "")])
    assert job_badge(cc) is None


def test_job_badge_failed_kind():
    from jailbee.qtui.model import CardContent, CardField, job_badge

    cc = CardContent(name="feat", state="Running", fields=[CardField("job", "JOB", "failed")])
    assert job_badge(cc) == ("failed", "failed")


def test_job_badge_worker_gone_is_failed_kind_despite_a_working_phase():
    from jailbee.qtui.model import CardContent, CardField, job_badge

    # background.job_label keeps the phase a dead worker stopped in, so the
    # badge must classify on the suffix — not on a "failed" prefix.
    cc = CardContent(
        name="feat",
        state="Running",
        fields=[CardField("job", "JOB", "cloning (worker gone)")],
    )
    assert job_badge(cc) == ("cloning (worker gone)", "failed")


def test_job_badge_in_flight_phase_is_running_kind():
    from jailbee.qtui.model import CardContent, CardField, job_badge

    cc = CardContent(name="feat", state="—", fields=[CardField("job", "JOB", "cloning")])
    assert job_badge(cc) == ("cloning", "running")


def test_card_content_carries_the_job_error():
    c = ContainerInfo(
        name="p-foo",
        state="Running",
        network="strict",
        ip=None,
        memory_limit=None,
        repo="p",
        job_phase="failed",
        job_pid=4242,
        job_kind="create",
        job_error="autostart step 'deps' failed",
    )
    fields = dashboard.visible_fields(datetime.now().astimezone(), [c])
    cc = m.card_content(c, fields)
    assert cc.job_error == "autostart step 'deps' failed"
