import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QMenu


def test_populate_action_menu_builds_native_submenus_and_dispatches_only_leaves(qtbot):
    from jailbee.qtui.action_menu import populate_action_menu

    menu = QMenu()
    qtbot.addWidget(menu)
    emitted = []
    populate_action_menu(
        menu,
        [
            ("Attach tmux", "tmux"),
            ("Launch JetBrains idea", "ide"),
            ("Launch Figma", "apps run figma --container"),
            ("Open PR", "pr open"),
            ("Create/update PR", "pr push"),
            ("Merge into…", "merge"),
            ("Update from base (git push)", "git push"),
            ("Destroy", "destroy"),
        ],
        emitted.append,
    )

    root = menu.actions()
    assert [action.text() for action in root] == [
        "Attach tmux",
        "Launch →",
        "PR →",
        "Git →",
        "Destroy",
    ]
    assert root[0].menu() is None
    assert [action.text() for action in root[1].menu().actions()] == [
        "Launch JetBrains idea",
        "Launch Figma",
    ]
    assert [action.text() for action in root[2].menu().actions()] == [
        "Open PR",
        "Create/update PR",
    ]
    assert [action.text() for action in root[3].menu().actions()] == [
        "Merge into…",
        "Update from base (git push)",
    ]

    root[1].trigger()  # a group header is not a command
    assert emitted == []
    root[0].trigger()
    root[1].menu().actions()[0].trigger()
    root[1].menu().actions()[1].trigger()
    root[2].menu().actions()[0].trigger()
    root[2].menu().actions()[1].trigger()
    root[3].menu().actions()[0].trigger()
    root[3].menu().actions()[1].trigger()
    root[4].trigger()
    assert emitted == [
        "tmux",
        "ide",
        "apps run figma --container",
        "pr open",
        "pr push",
        "merge",
        "git push",
        "destroy",
    ]


def test_populate_action_menu_omits_empty_groups(qtbot):
    from jailbee.qtui.action_menu import populate_action_menu

    menu = QMenu()
    qtbot.addWidget(menu)
    populate_action_menu(menu, [("Attach tmux", "tmux"), ("Destroy", "destroy")], lambda _: None)
    assert [action.text() for action in menu.actions()] == ["Attach tmux", "Destroy"]
    assert all(action.menu() is None for action in menu.actions())
