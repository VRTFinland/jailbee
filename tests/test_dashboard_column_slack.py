"""Spare table width goes to capped columns, up to their widest cell."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

from rich.text import Text

from jailbee.dashboard import columns as dcolumns
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import fleet
from jailbee.procstat import ProcessActivity
from tests.dashboard_fixtures import ci

NOW = datetime(2026, 6, 8, tzinfo=UTC)
# job (cap 22) precedes doing (cap 32) in field order; name and mode are uncapped.
ENABLED = ("name", "job", "mode", "doing")


def _group(tmp_path, *, doing="x" * 60, job="stage-" + "y" * 40):
    c = dataclasses.replace(
        ci("alpha-one", "alpha", job_phase=job),
        activity=(ProcessActivity(comm=doing, percent=50.0, count=1),),
    )
    return dmodel.RepoGroup("alpha", str(tmp_path), None, [c])


def _widths(group, width):
    model = fleet.table_model(
        [group],
        now=NOW,
        enabled=ENABLED,
        folded=frozenset(),
        column_widths=None,
        shown_columns=None,
        column_offset=0,
        hidden_by_preferences=False,
        width=width,
    )
    return dict(zip(model.geometry.names, model.geometry.widths, strict=True))


def _widest(group, name):
    spec = next(
        f for f in dcolumns.visible_fields(NOW, group.containers, ENABLED) if f.name == name
    )
    return max(Text.from_markup(spec.cell(c)).cell_len for c in group.containers)


def _budgets(group):
    """Today's per-field budgets (no slack), in field order."""
    fields, widths = dcolumns._frame_columns(
        [group],
        now=NOW,
        enabled=ENABLED,
        folded=frozenset(),
        column_widths=None,
        shown_columns=None,
    )
    return {f.name: w for f, w in zip(fields, widths, strict=True)}


def _tight(group):
    """The table's cost with today's budgets: the width at which there is no slack."""
    widths = list(_budgets(group).values())
    return widths[0] + sum(w + 2 for w in widths[1:])


def test_without_slack_the_budgets_hold(tmp_path):
    group = _group(tmp_path)
    assert _widths(group, _tight(group)) == _budgets(group)


def test_a_wide_terminal_shows_capped_columns_whole(tmp_path):
    group = _group(tmp_path)
    budgets = _budgets(group)
    assert _widest(group, "job") > budgets["job"]  # premise: both are cut today
    assert _widest(group, "doing") > budgets["doing"]

    wide = _widths(group, 300)

    assert wide["job"] == _widest(group, "job")  # past the 22 cap: slack lifts it
    # DOING changes every tick; widening it would shift the columns after it.
    assert wide["doing"] == budgets["doing"]
    # Uncapped columns keep their budget (mode is empty for this container, so hidden).
    assert wide["name"] == budgets["name"]
    assert wide.keys() == budgets.keys()


def test_slack_goes_to_capped_columns_in_field_order(tmp_path):
    group = _group(tmp_path)
    budgets = _budgets(group)

    widths = _widths(group, _tight(group) + 5)

    assert (widths["job"], widths["doing"]) == (budgets["job"] + 5, budgets["doing"])


def test_slack_is_never_spent_on_padding(tmp_path):
    group = _group(tmp_path, doing="ls", job="run")
    assert _widths(group, 300) == _budgets(group)
