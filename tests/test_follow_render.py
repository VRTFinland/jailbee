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
