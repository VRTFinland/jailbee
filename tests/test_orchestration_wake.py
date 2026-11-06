from dataclasses import dataclass

import pytest

from jailbee.orchestration.wake import (
    DigestWake,
    MessageWake,
    QuietChildWake,
    WakeUsage,
    newest_from_child,
    select_wakeup,
)


@dataclass(frozen=True)
class Row:
    id: str
    type: str = "message"
    kind_or_state: str | None = "note"
    from_name: str | None = None
    created_at: float = 100.0
    acked_at: float | None = None


def child(name="c1", running=True, started_at=0.0, **extra):
    return {"name": name, "running": running, "started_at": started_at, **extra}


def sel(unacked=(), history=None, children=None, **kw):
    kw.setdefault("wake", None)
    kw.setdefault("quiet", 0)
    kw.setdefault("digest", 0)
    kw.setdefault("digest_at", None)
    kw.setdefault("now", 1000.0)
    hist = list(unacked) if history is None else history
    return select_wakeup(list(unacked), hist, children, **kw)


def test_no_wake_means_everything_matches_first_row():
    assert sel([Row("a"), Row("b")]) == (MessageWake("a"), None)


def test_empty_returns_none():
    assert sel([]) == (None, None)


@pytest.mark.parametrize(
    ("wake", "row", "expected"),
    [
        ({"done"}, Row("a", kind_or_state="note"), None),
        ({"done"}, Row("a", kind_or_state="done"), "a"),
        ({"msg:done"}, Row("a", kind_or_state="done"), "a"),
        ({"status:done"}, Row("a", kind_or_state="done"), None),
        ({"status:done"}, Row("a", type="status", kind_or_state="done"), "a"),
        ({"msg:done"}, Row("a", type="status", kind_or_state="done"), None),
        ({"done"}, Row("a", type="status", kind_or_state="done"), "a"),
        (frozenset(), Row("a"), None),
    ],
)
def test_filter_and_prefixes(wake, row, expected):
    got, _ = sel([row], wake=frozenset(wake))
    assert got == (MessageWake(expected) if expected else None)


def test_skips_unmatched_for_later_match():
    got, _ = sel([Row("a"), Row("b", kind_or_state="done")], wake=frozenset({"done"}))
    assert got == MessageWake("b")


def test_first_message_of_child_overrides_filter_once():
    rows = [
        Row("a", from_name="c1", created_at=50),
        Row("b", from_name="c1", created_at=60),
    ]
    got, _ = sel(rows, children=[child(started_at=10)], wake=frozenset({"done"}))
    assert got == MessageWake("a")
    got, _ = sel(rows[1:], history=rows, children=[child(started_at=10)], wake=frozenset({"done"}))
    assert got is None


def test_first_override_ignores_messages_before_start_and_unknown_children():
    rows = [Row("a", from_name="c1", created_at=5), Row("b", from_name="zz", created_at=60)]
    got, _ = sel(rows, children=[child(started_at=10)], wake=frozenset({"done"}))
    assert got is None


def test_acked_first_report_prevents_later_override():
    old = Row("a", from_name="c1", created_at=50, acked_at=51)
    new = Row("b", from_name="c1", created_at=60)
    got, _ = sel([new], history=[old, new], children=[child(started_at=10)], wake=frozenset())
    assert got is None


def test_newest_from_child_messages_only():
    rows = [
        Row("a", from_name="x", created_at=3),
        Row("b", from_name="x", created_at=9),
        Row("c", type="status", from_name="x", created_at=20),
        Row("d", from_name=None, created_at=30),
    ]
    assert newest_from_child(rows) == {"x": 9}


def test_quiet_child_fires_and_cleans_name():
    got, _ = sel([], children=[child(name="a\x1bb", started_at=0)], quiet=10, wake=frozenset())
    assert got == QuietChildWake("a?b")


def test_quiet_not_due_when_recent_message():
    hist = [Row("a", from_name="c1", created_at=995)]
    got, _ = sel([], history=hist, children=[child()], quiet=10, wake=frozenset())
    assert got is None


def test_quiet_uses_activity_at():
    c = child(status={"activity_at": 995.0})
    assert sel([], children=[c], quiet=10, wake=frozenset())[0] is None
    c = child(status={"activity_at": 900.0})
    assert sel([], children=[c], quiet=10, wake=frozenset())[0] == QuietChildWake("c1")


@pytest.mark.parametrize("bad", ["x", True, None, float("nan"), 1e12])
def test_quiet_ignores_hostile_activity_at(bad):
    c = child(status={"activity_at": bad})
    assert sel([], children=[c], quiet=10, wake=frozenset())[0] == QuietChildWake("c1")


def test_quiet_excludes_done_and_stopped():
    cs = [child("a", done=True), child("b", running=False)]
    assert sel([], children=cs, quiet=10, wake=frozenset())[0] is None


def test_quiet_oldest_first_ties_by_name():
    cs = [child("b", started_at=100), child("a", started_at=100), child("c", started_at=200)]
    assert sel([], children=cs, quiet=10, wake=frozenset())[0] == QuietChildWake("a")
    cs = [child("b", started_at=100), child("a", started_at=300)]
    assert sel([], children=cs, quiet=10, wake=frozenset())[0] == QuietChildWake("b")


def test_digest_first_call_stamps_not_due():
    assert sel([], children=[child()], digest=60, wake=frozenset()) == (None, 1000.0)


def test_digest_unusable_stamp_is_reset():
    for bad in (2000.0, float("nan")):
        got = sel([], children=[child()], digest=60, digest_at=bad, wake=frozenset())
        assert got == (None, 1000.0)


def test_digest_fires_when_due_and_restamps():
    got = sel([], children=[child()], digest=60, digest_at=900.0, wake=frozenset())
    assert got == (DigestWake(), 1000.0)


def test_digest_not_due_or_no_running_children():
    assert sel([], children=[child()], digest=60, digest_at=980.0, wake=frozenset()) == (None, None)
    got = sel([], children=[child(running=False)], digest=60, digest_at=900.0, wake=frozenset())
    assert got == (None, None)
    got = sel([], children=[child(running=False)], digest=60, wake=frozenset())
    assert got == (None, 1000.0)


def test_digest_message_and_quiet_win():
    got = sel([Row("a")], children=[child()], digest=60, digest_at=1.0)
    assert got == (MessageWake("a"), None)
    got = sel([], children=[child()], quiet=10, digest=60, digest_at=1.0, wake=frozenset())
    assert got == (QuietChildWake("c1"), None)


@pytest.mark.parametrize(
    "children",
    [
        None,
        [{"name": "a"}],
        [child(started_at="x")],
        [child(started_at=2000.0)],
        [child(running="yes")],
        [child(done="no")],
        [child(name=3)],
    ],
)
def test_invalid_children_with_quiet_or_digest_raise(children):
    with pytest.raises(WakeUsage, match="needs a fresh broker children list"):
        sel([], children=children, quiet=5, wake=frozenset())
    with pytest.raises(WakeUsage, match="--digest needs"):
        sel([], children=children, digest=5, wake=frozenset())
    with pytest.raises(WakeUsage, match="--quiet-after and --digest need "):
        sel([], children=children, quiet=5, digest=5, wake=frozenset())


def test_invalid_children_without_quiet_digest_behave_as_none():
    assert sel([Row("a")], children=None, wake=frozenset({"note"}))[0] == MessageWake("a")
    assert sel([Row("a")], children=[{"name": 1}], wake=frozenset({"done"}))[0] is None
