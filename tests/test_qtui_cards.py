import pytest

pytest.importorskip("PySide6")

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from jailbee.config import ColumnConfig
from jailbee.dashboard import RepoGroup
from jailbee.lifecycle import ContainerInfo
from jailbee.qtui.cards import CardView, _Card


def _label_texts(card):
    return [w.text() for w in card.findChildren(QLabel)]


def _groups():
    running = ContainerInfo(
        name="p-foo",
        state="Running",
        network="strict",
        ip="1.2.3.4",
        memory_limit="2GB",
        repo="p",
    )
    stopped = ContainerInfo(
        name="p-bar",
        state="Stopped",
        network=None,
        ip=None,
        memory_limit=None,
        repo="p",
    )
    return [RepoGroup("p", "/repo", Path("/repo/.gie/config.yaml"), [running, stopped])]


def _groups_with(states):
    """One repo group ``p`` holding a container per ``{name: state}`` entry,
    in insertion order (mirrors gather_rows handing CardView a fixed order)."""
    cs = [
        ContainerInfo(
            name=name,
            state=state,
            network="strict",
            ip="1.2.3.4",
            memory_limit="2GB",
            repo="p",
        )
        for name, state in states.items()
    ]
    return [RepoGroup("p", "/repo", Path("/repo/.gie/config.yaml"), cs)]


def _cards(view):
    return view.findChildren(_Card)


def _card(view, name):
    return next(c for c in _cards(view) if c._name == name)


def _group_header(view, prefix):
    from jailbee.qtui.cards import _GroupHeader

    return next(h for h in view.findChildren(_GroupHeader) if h._prefix == prefix)


def test_compact_card_hides_git_row_when_clean(qtbot):
    from jailbee.qtui.cards import _Card
    from jailbee.qtui.model import CardContent, CardField

    cc = CardContent(
        name="feat",
        state="Running",
        fields=[
            CardField("wt", "WT", "clean"),
            CardField("ahead_count", "↑", "0"),
            CardField("conflict", "MERGE", "ok"),
            CardField("network", "NETWORK", "strict"),
        ],
    )
    card = _Card("p-feat", cc, style="compact", selected=False)
    qtbot.addWidget(card)
    texts = " | ".join(_label_texts(card))
    assert "strict" in texts  # meta shown
    assert "clean" not in texts  # clean git row hidden
    assert "MERGE" not in texts  # no per-field git labels in compact


def test_compact_card_shows_git_segments_when_dirty(qtbot):
    from jailbee.qtui.cards import _Card
    from jailbee.qtui.model import CardContent, CardField

    cc = CardContent(
        name="feat",
        state="Running",
        fields=[
            CardField("ahead_count", "↑", "3"),
            CardField("wt", "WT", "+12 -3"),
            CardField("conflict", "MERGE", "ok"),
        ],
    )
    card = _Card("p-feat", cc, style="compact", selected=False)
    qtbot.addWidget(card)
    texts = " ".join(_label_texts(card))
    assert "↑3" in texts and "+12 -3" in texts


def test_compact_card_shows_pr_pill(qtbot):
    from jailbee.qtui.cards import _Card
    from jailbee.qtui.model import CardContent, CardField

    cc = CardContent(
        name="feat",
        state="Running",
        fields=[CardField("pr", "PR", "#482 ↓")],
    )
    card = _Card("p-feat", cc, style="compact", selected=False)
    qtbot.addWidget(card)
    pill = next(t for t in _label_texts(card) if "PR" in t and "#482" in t)
    assert pill  # a single visible label carries both the "PR" marker and the number


def test_clickable_label_emits_only_on_left_click(qtbot):
    from jailbee.qtui.cards import _ClickableLabel

    lbl = _ClickableLabel("x")
    qtbot.addWidget(lbl)
    lbl.show()
    qtbot.waitExposed(lbl)
    seen = []
    lbl.clicked.connect(lambda: seen.append(True))

    qtbot.mouseClick(lbl, Qt.MouseButton.RightButton)
    assert seen == []  # right-click ignored
    qtbot.mouseClick(lbl, Qt.MouseButton.LeftButton)
    assert seen == [True]  # left-click emits


def test_compact_pr_pill_click_opens_the_pr(qtbot):
    from jailbee.qtui.cards import _ClickableLabel

    running = ContainerInfo(
        name="p-foo",
        state="Running",
        network="strict",
        ip="1.2.3.4",
        memory_limit="2GB",
        repo="p",
        pr_number=482,
    )
    groups = [RepoGroup("p", "/repo", Path("/repo/.gie/config.yaml"), [running])]
    view = CardView()
    qtbot.addWidget(view)
    view.set_groups(groups, now=datetime.now().astimezone())

    card = _card(view, "p-foo")
    pill = next(w for w in card.findChildren(_ClickableLabel) if "PR" in w.text())

    with qtbot.waitSignal(view.actionRequested, timeout=1000) as blocker:
        pill.clicked.emit()
    assert blocker.args == ["pr --open", "p-foo"]


def test_set_groups_creates_one_card_per_container(qtbot):
    view = CardView()
    qtbot.addWidget(view)
    view.set_groups(_groups(), now=datetime.now().astimezone())
    names = sorted(card._name for card in _cards(view))
    assert names == ["p-bar", "p-foo"]


def test_selection_preserved_across_refresh(qtbot):
    view = CardView()
    qtbot.addWidget(view)
    now = datetime.now().astimezone()
    view.set_groups(_groups(), now=now)
    view._on_clicked("p-foo")
    assert view.selected_name() == "p-foo"
    view.set_groups(_groups(), now=now)  # refresh
    assert view.selected_name() == "p-foo"


def test_left_click_applies_selection_style_to_the_card_itself(qtbot):
    """The selection highlight must live on the card widget's own stylesheet,
    not on an ancestor QSS rule toggled via ``setProperty`` +
    ``style().unpolish/polish``. That older idiom re-resolved the style but did
    not schedule an on-screen repaint on Wayland, so the highlight only showed
    up on the next ``set_groups`` rebuild (or never, in manual-refresh mode).
    Setting the card's own stylesheet reliably repaints."""
    view = CardView()
    qtbot.addWidget(view)
    view.set_groups(_groups(), now=datetime.now().astimezone())

    view._on_clicked("p-foo")

    foo = next(c for c in _cards(view) if c._name == "p-foo")
    bar = next(c for c in _cards(view) if c._name == "p-bar")
    assert "border" in foo.styleSheet()
    assert bar.styleSheet() == ""


def test_cards_are_reused_across_identical_refresh(qtbot):
    """An identical refresh must reuse the existing card widgets, not tear
    them down and rebuild — the churn the reconcile exists to avoid."""
    view = CardView()
    qtbot.addWidget(view)
    now = datetime.now().astimezone()
    view.set_groups(_groups(), now=now)
    before = {c._name: id(c) for c in _cards(view)}

    view.set_groups(_groups(), now=now)  # identical snapshot

    after = {c._name: id(c) for c in _cards(view)}
    assert after == before


def test_state_change_updates_the_same_card_in_place(qtbot):
    """A container changing state reuses its card and updates the label in
    place, rather than replacing the widget."""
    view = CardView()
    qtbot.addWidget(view)
    now = datetime.now().astimezone()
    view.set_groups(_groups_with({"p-foo": "Running", "p-bar": "Stopped"}), now=now)
    foo_before = _card(view, "p-foo")

    view.set_groups(_groups_with({"p-foo": "Stopped", "p-bar": "Stopped"}), now=now)

    foo_after = _card(view, "p-foo")
    assert foo_after is foo_before
    assert "Stopped" in " ".join(_label_texts(foo_after))


def test_new_container_adds_a_card_and_reuses_existing(qtbot):
    view = CardView()
    qtbot.addWidget(view)
    now = datetime.now().astimezone()
    view.set_groups(_groups_with({"p-foo": "Running", "p-bar": "Stopped"}), now=now)
    foo_before = _card(view, "p-foo")

    view.set_groups(
        _groups_with({"p-foo": "Running", "p-bar": "Stopped", "p-baz": "Running"}),
        now=now,
    )

    cards = {c._name: c for c in _cards(view)}
    assert set(cards) == {"p-foo", "p-bar", "p-baz"}
    assert cards["p-foo"] is foo_before


def test_removed_container_drops_its_card_and_reuses_others(qtbot):
    view = CardView()
    qtbot.addWidget(view)
    now = datetime.now().astimezone()
    view.set_groups(_groups_with({"p-foo": "Running", "p-bar": "Stopped"}), now=now)
    bar_before = _card(view, "p-bar")

    view.set_groups(_groups_with({"p-bar": "Stopped"}), now=now)  # p-foo gone

    cards = {c._name: c for c in _cards(view)}
    assert set(cards) == {"p-bar"}
    assert cards["p-bar"] is bar_before


def test_selection_dropped_when_container_gone(qtbot):
    view = CardView()
    qtbot.addWidget(view)
    now = datetime.now().astimezone()
    view.set_groups(_groups(), now=now)
    view._on_clicked("p-foo")
    view.set_groups([], now=now)  # container vanished
    assert view.selected_name() is None


def test_context_menu_emits_action_requested(qtbot):
    from PySide6.QtCore import QPoint, QTimer
    from PySide6.QtWidgets import QApplication, QMenu

    view = CardView()
    qtbot.addWidget(view)
    view.set_groups(_groups(), now=datetime.now().astimezone())

    # QMenu.exec() opens a real (blocking) local event loop; monkeypatching
    # QMenu.exec at the class level does not intercept it (PySide6/Shiboken
    # resolves virtual/base-class Qt methods like `exec` through the C++
    # vtable, bypassing a plain Python attribute override on the class for
    # a non-subclassed instance). Instead, schedule a zero-delay QTimer to
    # fire once the nested event loop starts, grab the live popup via
    # QApplication.activePopupWidget(), trigger its first action, and close
    # it so exec() returns.
    def interact():
        popup = QApplication.activePopupWidget()
        assert isinstance(popup, QMenu)
        actions = popup.actions()
        if actions:
            actions[0].trigger()
        popup.close()

    QTimer.singleShot(0, interact)
    with qtbot.waitSignal(view.actionRequested, timeout=1000) as blocker:
        view._on_context("p-foo", QPoint(0, 0))
    verb, name = blocker.args
    assert name == "p-foo"
    assert isinstance(verb, str) and verb


def test_grid_style_shows_labels_and_folded_git(qtbot):
    view = CardView()
    qtbot.addWidget(view)
    view.set_card_style("grid")
    view.set_groups(_groups(), now=datetime.now().astimezone())
    card = _card(view, "p-foo")
    texts = " | ".join(_label_texts(card))
    assert "GIT" in texts  # folded git row label present in grid


def test_set_card_style_rebuilds_into_new_style(qtbot):
    view = CardView()
    qtbot.addWidget(view)
    now = datetime.now().astimezone()
    view.set_groups(_groups(), now=now)
    assert view.card_style() == "compact"
    foo_before = _card(view, "p-foo")

    view.set_card_style("grid")

    assert view.card_style() == "grid"
    foo_after = _card(view, "p-foo")
    assert foo_after is not foo_before  # style switch rebuilds
    assert foo_after._style == "grid"


def test_group_header_label_refreshes_when_orphan_status_flips(qtbot):
    """Regression: the fast-path reconcile structure key must include the
    header label, not just (prefix, names). A repo can flip
    registered<->orphan (e.g. its .gie/config.yaml goes missing) while its
    prefix and container set stay unchanged; the group header text must
    still be rebuilt to reflect the new label rather than going stale."""
    view = CardView()
    qtbot.addWidget(view)
    now = datetime.now().astimezone()
    registered = _groups()  # RepoGroup("p", "/repo", Path(...), [foo, bar])
    view.set_groups(registered, now=now)
    header_before = _group_header(view, "p").text()
    assert "orphan" not in header_before

    # Same prefix, same container names — only repo_root/config_path flip to
    # orphan, so (prefix, names) alone is unchanged.
    orphaned = [RepoGroup("p", None, None, g.containers) for g in registered]
    view.set_groups(orphaned, now=now)
    qtbot.wait(50)  # flush the old header's deleteLater() before re-querying

    header_after = _group_header(view, "p").text()
    assert "orphan" in header_after


def test_set_groups_forwards_columns_to_hide_a_field(qtbot):
    """`set_groups`'s `columns` argument must reach `visible_fields` — a
    hidden field must not appear as a card row, and must reappear once the
    hide is lifted."""
    view = CardView()
    qtbot.addWidget(view)
    view.set_card_style("grid")
    now = datetime.now().astimezone()

    view.set_groups(_groups(), now=now, columns=ColumnConfig(hide=["network"]))
    hidden_texts = " | ".join(_label_texts(_card(view, "p-foo")))
    assert "NETWORK" not in hidden_texts

    view.set_groups(_groups(), now=now, columns=None)
    shown_texts = " | ".join(_label_texts(_card(view, "p-foo")))
    assert "NETWORK" in shown_texts


def test_set_card_style_rerender_keeps_the_active_columns(qtbot):
    """Regression for the style-switch re-render (cards.py's
    `set_card_style`): it must reuse the columns from the last `set_groups`
    call (`self._columns`), not fall back to the built-in default set."""
    view = CardView()
    qtbot.addWidget(view)
    now = datetime.now().astimezone()
    view.set_groups(_groups(), now=now, columns=ColumnConfig(hide=["network"]))

    view.set_card_style("grid")

    texts = " | ".join(_label_texts(_card(view, "p-foo")))
    assert "NETWORK" not in texts


def test_set_card_style_before_any_set_groups_does_not_raise(qtbot):
    """set_card_style() called before set_groups() must not raise — guards
    the `self._now is None` early-return in set_card_style, which skips the
    render-with-new-style call until a first set_groups has established
    `now`."""
    view = CardView()
    qtbot.addWidget(view)

    view.set_card_style("grid")

    assert view.card_style() == "grid"


def test_collapsing_a_group_hides_its_cards_and_records_prefix(qtbot):
    view = CardView()
    qtbot.addWidget(view)
    view.show()  # isVisible() reflects effective on-screen visibility, which
    # requires a shown top-level ancestor — not just setVisible(True) on the host.
    view.set_groups(_groups(), now=datetime.now().astimezone())  # repo prefix "p"
    host = view._grid_hosts["p"]
    qtbot.waitUntil(lambda: host.isVisible(), timeout=1000)  # newly-inserted
    # widgets only become effectively visible once Qt processes the pending show

    with qtbot.waitSignal(view.collapsedChanged, timeout=1000):
        _group_header(view, "p").clicked.emit("p")

    assert "p" in view.collapsed()
    assert not host.isVisible()

    _group_header(view, "p").clicked.emit("p")  # expand again
    assert "p" not in view.collapsed()
    assert host.isVisible()


def test_set_collapsed_is_applied_on_next_render(qtbot):
    view = CardView()
    qtbot.addWidget(view)
    view.set_collapsed({"p"})
    view.set_groups(_groups(), now=datetime.now().astimezone())
    assert not view._grid_hosts["p"].isVisible()


def test_context_menu_highlights_immediately(qtbot):
    """Right-clicking a card should update its visual selection right away,
    not wait for the next set_groups (matches left-click behavior)."""
    from PySide6.QtCore import QPoint, QTimer
    from PySide6.QtWidgets import QApplication, QMenu

    view = CardView()
    qtbot.addWidget(view)
    view.set_groups(_groups(), now=datetime.now().astimezone())

    card = next(c for c in _cards(view) if c._name == "p-foo")
    seen_style: list[str] = []

    def interact():
        # By the time the popup's event loop is pumping, _apply_selection
        # has already run (it happens before menu.exec in _on_context).
        seen_style.append(card.styleSheet())
        popup = QApplication.activePopupWidget()
        assert isinstance(popup, QMenu)
        popup.close()

    QTimer.singleShot(0, interact)
    view._on_context("p-foo", QPoint(0, 0))  # blocks in menu.exec() until interact() closes it

    assert view.selected_name() == "p-foo"
    assert len(seen_style) == 1 and "border" in seen_style[0]


def test_compact_card_shows_failed_job_badge_with_error_tooltip(qtbot):
    from jailbee.qtui.cards import _Card
    from jailbee.qtui.model import CardContent, CardField

    cc = CardContent(
        name="feat",
        state="Running",
        fields=[CardField("job", "JOB", "failed"), CardField("network", "NETWORK", "strict")],
        job_error="autostart step 'deps' failed",
    )
    card = _Card("p-feat", cc, style="compact", selected=False)
    qtbot.addWidget(card)
    pill = next(w for w in card.findChildren(QLabel) if w.text() == "failed")
    assert "autostart step 'deps' failed" in pill.toolTip()


def test_grid_card_also_shows_the_job_badge(qtbot):
    from jailbee.qtui.cards import _JOB_BADGE_COLORS, _Card
    from jailbee.qtui.model import CardContent, CardField

    cc = CardContent(
        name="feat",
        state="Running",
        fields=[CardField("job", "JOB", "cloning")],
    )
    card = _Card("p-feat", cc, style="grid", selected=False)
    qtbot.addWidget(card)
    # grid_rows() renders its own JOB row with this same text, so asserting
    # merely that *a* label reads "cloning" would pass even if the header
    # badge were never built. Require both: the badge (identified by its
    # background colour) and the grid row, as two distinct labels.
    cloning_labels = [w for w in card.findChildren(QLabel) if w.text() == "cloning"]
    assert len(cloning_labels) == 2
    badge_color = _JOB_BADGE_COLORS["running"]
    assert any(badge_color in w.styleSheet() for w in cloning_labels)


def test_compact_card_has_no_job_badge_without_a_job(qtbot):
    from jailbee.qtui.cards import _Card
    from jailbee.qtui.model import CardContent, CardField

    cc = CardContent(
        name="feat",
        state="Running",
        fields=[CardField("network", "NETWORK", "strict")],
    )
    card = _Card("p-feat", cc, style="compact", selected=False)
    qtbot.addWidget(card)
    texts = [w.text() for w in card.findChildren(QLabel)]
    assert "failed" not in texts and "cloning" not in texts
