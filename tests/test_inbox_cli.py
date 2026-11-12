"""Rendering and shared inbox parsing tests."""

import re

import pytest

BAD_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


STATUS_NOW = 100000.0


def _full_status(now: float = STATUS_NOW) -> dict[str, object]:
    return {
        "observed_at": now,
        "agent": {
            "alive": True,
            "state": "busy",
            "since": now - 600,
            "last_event_at": now - 30,
            "last_tool": "Bash",
        },
        "cpu_percent": 12.4,
        "memory_bytes": 1610612736,
        "longest_command": {"command": "pytest -q", "seconds": 4320},
        "head": {"sha": "abcdef1234567890", "time": now - 720, "subject": "fix thing"},
        "dirty": True,
        "last_busy_at": now - 60,
        "last_to_child_at": now - 3600,
        "activity_at": now - 240,
    }


def _status_child(name: str = "kid", **extra: object) -> dict[str, object]:
    item: dict[str, object] = {
        "name": name,
        "running": True,
        "done": False,
        "session": None,
        "started_at": 1,
        "status": _full_status(),
    }
    item.update(extra)
    return item


def _cells(line: str) -> list[str]:
    return re.split(r"\s{2,}", line.strip())


def test_grant_renders_project_boards_in_text_and_json(capsys):
    import json

    from jailbee.inbox.cli import render_grant
    from jailbee.orchestration.grant import Grant, PublishRights

    grant = Grant(
        publish=PublishRights.model_validate(
            {"projects": [{"board": "Org/3", "actions": ["set", "remove"]}]}
        )
    ).model_dump(mode="json")
    assert render_grant(grant, as_json=False) == 0
    assert "publish projects: org/3 (remove, set)" in capsys.readouterr().out
    assert render_grant(grant, as_json=True) == 0
    assert json.loads(capsys.readouterr().out)["publish"]["projects"] == [
        {"board": "org/3", "actions": ["remove", "set"]}
    ]
    grant["publish"]["projects"] = [{"board": "org/3\u001b", "actions": ["set\u0085"]}]
    render_grant(grant, as_json=False)
    output = capsys.readouterr().out
    assert "org/3? (set?)" in output
    assert "\u001b" not in output and "\u0085" not in output


def test_status_table_running_child():
    from jailbee.inbox.render import render_status

    out = render_status(
        [_status_child()], now=STATUS_NOW, from_child={"kid": STATUS_NOW - 720}
    ).splitlines()
    assert _cells(out[0]) == "NAME STATE AGENT ACTIVE CPU MEM HEAD DIRTY LONGEST LAST-MSG".split()
    assert _cells(out[1]) == [
        "kid",
        "running",
        "alive:busy",
        "4m",
        "12%",
        "1.5GiB",
        "abcdef1 12m fix thing",
        "yes",
        "pytest -q 1h12m",
        "↑12m ↓1h0m",
    ]


@pytest.mark.parametrize(
    ("agent", "cell"),
    [
        ({"alive": None, "state": "starting"}, "starting"),
        ({"alive": False, "state": None}, "dead"),
        ({"alive": None, "state": None}, "?"),
    ],
)
def test_status_table_agent_cell_starting_vs_dead(agent, cell):
    from jailbee.inbox.render import render_status

    status = {**_full_status(), "agent": agent}
    out = render_status([_status_child(status=status)], now=STATUS_NOW, from_child={}).splitlines()
    assert _cells(out[1])[2] == cell


def test_status_old_broker_without_status_prints_question_marks():
    from jailbee.inbox.render import render_status

    child = {"name": "old", "running": True, "session": None, "started_at": 1}
    out = render_status([child], now=STATUS_NOW, from_child={}).splitlines()
    assert _cells(out[1]) == ["old", "running", "?", "?", "?", "?", "?", "?", "?", "↑? ↓?"]


def test_status_done_wins_over_running():
    from jailbee.inbox.render import render_status

    out = render_status([_status_child(done=True)], now=STATUS_NOW, from_child={})
    assert _cells(out.splitlines()[1])[1] == "done"
    out = render_status([_status_child(running=False)], now=STATUS_NOW, from_child={})
    assert _cells(out.splitlines()[1])[1] == "stopped"


def test_status_memory_rounds_up_at_gib_boundary():
    from jailbee.inbox.render import render_status

    def mem(value: int) -> str:
        doc = _full_status()
        doc["memory_bytes"] = value
        out = render_status([_status_child(status=doc)], now=STATUS_NOW, from_child={})
        return _cells(out.splitlines()[1])[5]

    assert mem(1024**3 - 1) == "1.0GiB"
    assert mem(1024**3 - 400_000) == "1.0GiB"
    assert mem(1023 * 1024**2) == "1023MiB"


def test_status_hostile_values_do_not_raise():
    from jailbee.inbox.render import render_status

    hostile: list[object] = [
        None,
        42,
        "str",
        {},
        {"name": 5, "running": "yes", "status": "x"},
        {"name": "a", "status": None},
        {"name": "b", "status": []},
        {
            "name": "c",
            "running": 1,
            "done": "no",
            "status": {
                "agent": [],
                "cpu_percent": True,
                "memory_bytes": "9",
                "longest_command": {"command": 5, "seconds": [1]},
                "head": {"sha": 7, "time": "x", "subject": None},
                "dirty": "maybe",
                "activity_at": {},
                "last_to_child_at": True,
            },
        },
        {
            "name": "d",
            "status": {"agent": {"alive": "yes", "state": 3}, "head": "abc", "longest_command": 5},
        },
    ]
    out = render_status(hostile, now=STATUS_NOW, from_child={"a": float("nan")})  # type: ignore[arg-type]
    lines = out.splitlines()
    assert len(lines) == 1 + len(hostile)
    assert not BAD_CHARS.search(out.replace("\n", ""))
    assert all(len(line) < 300 for line in lines)
    assert _cells(lines[4])[:2] == ["?", "?"]  # {"name": 5, "running": "yes"}
    assert _cells(lines[8])[0] == "c"


@pytest.mark.parametrize("cpu", [float("nan"), float("inf"), float("-inf"), True, -1.0, "5", None])
def test_status_non_finite_cpu_prints_question_mark(cpu):
    from jailbee.inbox.render import render_status

    doc = _full_status()
    doc["cpu_percent"] = cpu
    out = render_status([_status_child(status=doc)], now=STATUS_NOW, from_child={})
    assert _cells(out.splitlines()[1])[4] == "?"


@pytest.mark.parametrize(
    ("value", "expected"), [("0", 0), ("2s", 2), ("3m", 180), ("1h", 3600), ("none", None)]
)
def test_parse_timeout(value, expected):
    from jailbee.inbox.cli import parse_timeout

    assert parse_timeout(value) == expected


@pytest.mark.parametrize("value", ["", "-1", "nonee", "1d", "1.5"])
def test_parse_timeout_rejects_invalid(value):
    from jailbee.inbox.cli import parse_timeout
    from jailbee.inbox.errors import InboxUsage

    with pytest.raises(InboxUsage):
        parse_timeout(value)


@pytest.mark.parametrize("value", ["done", "msg:done", "status:failed", "note,started"])
def test_parse_wait_accepts_wake_tokens(value):
    from jailbee.inbox.cli import parse_wait_args

    assert parse_wait_args("30m", value, None, None)[3] == frozenset(value.split(","))


@pytest.mark.parametrize("value", ["", "msg:failed", "status:note", "done,", "other"])
def test_parse_wait_rejects_invalid_tokens(value):
    from jailbee.inbox.cli import parse_wait_args
    from jailbee.inbox.errors import InboxUsage

    with pytest.raises(InboxUsage):
        parse_wait_args("30m", value, None, None)
