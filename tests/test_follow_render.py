from jailbee.inbox.follow_render import render_event


def test_grant_changed_renders_the_diff_and_where_to_look() -> None:
    line = render_event(
        {
            "kind": "grant-changed",
            "changes": ["max_running_children: 2 -> 5", "publish.issues: false -> true"],
        }
    )
    assert line is not None
    assert "max_running_children: 2 -> 5; publish.issues: false -> true" in line
    assert "jailbee inbox grant" in line


def test_grant_changed_detail_is_cut_to_400_characters() -> None:
    line = render_event({"kind": "grant-changed", "changes": ["x" * 300, "y" * 300]})
    assert line is not None
    detail = line.split(" (", 1)[1].split(") — ", 1)[0]
    assert len(detail) == 400
    assert detail.startswith("x" * 300 + "; y")
