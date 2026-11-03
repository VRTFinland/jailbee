import fcntl
import json
import os
from pathlib import Path

import pytest

from jailbee.inbox.cli import InboxUsage, Settings


def put(path: Path, data: dict, stamp: float = 1000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    os.utime(path, (stamp, stamp))
    return path


def test_select_respects_filter_and_first_report(tmp_path):
    from jailbee.inbox.waiting import select_wakeup

    put(
        tmp_path / "children.json",
        {
            "version": 1,
            "updated_at": 1000,
            "children": [{"name": "kid", "running": True, "started_at": 900}],
        },
    )
    put(tmp_path / "inbox/read/old.json", {"type": "message", "from": "kid", "kind": "note"}, 950)
    put(tmp_path / "inbox/a.json", {"type": "message", "from": "kid", "kind": "note"}, 1000)
    selected = put(
        tmp_path / "inbox/b.json", {"type": "message", "from": "other", "kind": "question"}, 1001
    )
    assert select_wakeup(tmp_path, frozenset({"question"}), 0, 1001) == str(selected)
    (tmp_path / "inbox/read/old.json").unlink()
    assert select_wakeup(tmp_path, frozenset({"question"}), 0, 1001) == str(
        tmp_path / "inbox/a.json"
    )


def test_select_skips_live_reserved_status(tmp_path):
    from jailbee.inbox.waiting import find_status, select_wakeup

    status = put(tmp_path / "inbox/a.json", {"type": "status", "state": "done", "ref": "abc"})
    lock = tmp_path / "status-waits/abc.lock"
    lock.parent.mkdir()
    with lock.open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert select_wakeup(tmp_path, None, 0, 1000) is None
        assert find_status(tmp_path / "inbox", "abc") == status
    assert select_wakeup(tmp_path, None, 0, 1000) == str(status)
    assert find_status(tmp_path / "inbox", "missing") is None


def test_select_quiet_child_cleans_name(tmp_path):
    from jailbee.inbox.waiting import select_wakeup

    put(
        tmp_path / "children.json",
        {
            "version": 1,
            "updated_at": 1000,
            "children": [{"name": "k\x9bid", "running": True, "started_at": 900}],
        },
    )
    assert select_wakeup(tmp_path, frozenset(), 30, 1000) == "quiet-child k?id"
    with pytest.raises(InboxUsage, match=r"needs a fresh broker children\.json"):
        select_wakeup(tmp_path, None, 1, 2000)


def test_select_ignores_partially_invalid_snapshot(tmp_path):
    from jailbee.inbox.waiting import select_wakeup

    put(
        tmp_path / "children.json",
        {
            "version": 1,
            "updated_at": 1000,
            "children": [{"name": "kid", "running": True, "started_at": 900}, {}],
        },
    )
    message = put(tmp_path / "inbox/a.json", {"type": "message", "kind": "note"})
    assert select_wakeup(tmp_path, None, 0, 1000) == str(message)
    with pytest.raises(InboxUsage, match=r"needs a fresh broker children\.json"):
        select_wakeup(tmp_path, None, 1, 1000)


@pytest.mark.parametrize(
    ("arrival", "deadline", "expected"),
    [(None, 100, 1), (0.6, 100, 1.6), (None, 0.5, 0.5)],
)
def test_settle_quiet_and_deadline(tmp_path, arrival, deadline, expected):
    from jailbee.inbox.waiting import settle

    settings = Settings(tmp_path, tmp_path / "inbox", tmp_path / "outbox", 1, 1)
    settings.inbox.mkdir()
    now = 0.0

    def clock():
        return now

    def sleep(seconds):
        nonlocal now, arrival
        now += seconds
        if arrival is not None and now + 1e-9 >= arrival:
            put(settings.inbox / "arrival.json", {})
            arrival = None

    settle(settings, deadline, clock=clock, sleep=sleep)
    assert now == pytest.approx(expected)


def test_settle_extends_on_new_arrival_and_caps_at_5s(tmp_path):
    from jailbee.inbox.waiting import settle

    settings = Settings(tmp_path, tmp_path / "inbox", tmp_path / "outbox", 1, 1)
    settings.inbox.mkdir()
    now = 0.0
    arrivals = iter([0.8, 1.6, 2.4, 3.2, 4.0, 4.8])
    next_arrival = next(arrivals, None)

    def clock():
        return now

    def sleep(seconds):
        nonlocal now, next_arrival
        now += seconds
        if next_arrival is not None and now + 1e-9 >= next_arrival:
            put(settings.inbox / f"{next_arrival}.json", {})
            next_arrival = next(arrivals, None)

    settle(settings, 100, clock=clock, sleep=sleep)
    assert now == pytest.approx(5)
    assert next_arrival is None


# --- #64: wait --digest and activity-based --quiet-after ---------------------

NOW = 100_000.0


def snapshot(base: Path, children: list[dict], updated_at: float = NOW) -> None:
    put(base / "children.json", {"version": 1, "updated_at": updated_at, "children": children})


def kid(name: str = "kid", *, running: bool = True, done: bool = False, **extra) -> dict:
    return {
        "name": name,
        "running": running,
        "done": done,
        "started_at": NOW - 7200,
        **extra,
    }


def test_quiet_after_uses_activity_at(tmp_path):
    from jailbee.inbox.waiting import select_wakeup

    snapshot(tmp_path, [kid(status={"activity_at": NOW - 300})])
    # started 2 h ago, no messages: only activity_at keeps it alive
    assert select_wakeup(tmp_path, frozenset(), 3600, NOW) is None
    assert select_wakeup(tmp_path, frozenset(), 200, NOW) == "quiet-child kid"


def test_quiet_after_without_status_uses_messages(tmp_path):
    from jailbee.inbox.waiting import select_wakeup

    snapshot(tmp_path, [kid(), kid("other", status=None), kid("third", status="junk")])
    put(tmp_path / "inbox/read/a.json", {"type": "message", "from": "kid"}, NOW - 100)
    put(tmp_path / "inbox/read/b.json", {"type": "message", "from": "other"}, NOW - 100)
    put(tmp_path / "inbox/read/c.json", {"type": "message", "from": "third"}, NOW - 100)
    assert select_wakeup(tmp_path, frozenset(), 3600, NOW) is None
    assert select_wakeup(tmp_path, frozenset(), 50, NOW) == "quiet-child kid"


def test_quiet_after_activity_at_done_child_stays_excluded(tmp_path):
    from jailbee.inbox.waiting import select_wakeup

    snapshot(tmp_path, [kid(done=True, status={"activity_at": NOW - 5000})])
    assert select_wakeup(tmp_path, frozenset(), 60, NOW) is None


@pytest.mark.parametrize(
    "bad",
    [float("nan"), float("inf"), "now", True, 1e300, None, [1], NOW + 3600],
)
def test_quiet_after_ignores_hostile_activity_at(tmp_path, bad):
    from jailbee.inbox.waiting import select_wakeup

    snapshot(tmp_path, [kid(status={"activity_at": bad})])
    # The hostile value must neither crash nor keep the child alive.
    assert select_wakeup(tmp_path, frozenset(), 3600, NOW) == "quiet-child kid"


def test_digest_first_call_creates_stamp_not_due(tmp_path):
    from jailbee.inbox.waiting import select_wakeup

    snapshot(tmp_path, [kid()])
    assert select_wakeup(tmp_path, frozenset(), 0, NOW, 60) is None
    assert float((tmp_path / "digest-at").read_text()) == NOW
    assert select_wakeup(tmp_path, frozenset(), 0, NOW + 59, 60) is None
    assert select_wakeup(tmp_path, frozenset(), 0, NOW + 60, 60) == "digest"


@pytest.mark.parametrize("content", ["garbage", "nan", "inf", str(NOW + 99999), ""])
def test_digest_unusable_stamp_is_reset_not_due(tmp_path, content):
    from jailbee.inbox.waiting import select_wakeup

    snapshot(tmp_path, [kid()])
    (tmp_path / "digest-at").write_text(content)
    assert select_wakeup(tmp_path, frozenset(), 0, NOW, 60) is None
    assert float((tmp_path / "digest-at").read_text()) == NOW


def test_digest_not_emitted_without_running_children_and_stamp_unchanged(tmp_path):
    from jailbee.inbox.waiting import select_wakeup

    snapshot(tmp_path, [kid(running=False), kid("fin", done=True)])
    (tmp_path / "digest-at").write_text(str(NOW - 9999))
    assert select_wakeup(tmp_path, frozenset(), 0, NOW, 60) is None
    assert float((tmp_path / "digest-at").read_text()) == NOW - 9999


def test_digest_message_wins(tmp_path):
    from jailbee.inbox.waiting import select_wakeup

    snapshot(tmp_path, [kid()])
    (tmp_path / "digest-at").write_text(str(NOW - 9999))
    message = put(tmp_path / "inbox/a.json", {"type": "message", "from": "kid", "kind": "note"})
    assert select_wakeup(tmp_path, frozenset({"note"}), 0, NOW, 60) == str(message)


def test_digest_quiet_child_wins(tmp_path):
    from jailbee.inbox.waiting import select_wakeup

    snapshot(tmp_path, [kid()])
    (tmp_path / "digest-at").write_text(str(NOW - 9999))
    assert select_wakeup(tmp_path, frozenset(), 60, NOW, 60) == "quiet-child kid"


def test_digest_needs_fresh_snapshot_exit_2(tmp_path):
    from jailbee.inbox.waiting import select_wakeup

    snapshot(tmp_path, [kid()], updated_at=NOW - 2000)
    with pytest.raises(InboxUsage, match=r"--digest needs a fresh broker children\.json"):
        select_wakeup(tmp_path, frozenset(), 0, NOW, 60)
    assert not (tmp_path / "digest-at").exists()


def _settings(tmp_path: Path):
    from jailbee.inbox import cli

    return cli.load_settings({"JAILBEE_ORCHESTRATION_DIR": str(tmp_path)})


def fresh_snapshot(base: Path, children: list[dict]) -> None:
    import time

    now = time.time()
    snapshot(
        base,
        [{**c, "started_at": now - 7200} for c in children],
        updated_at=now,
    )


def test_cmd_wait_digest_rejects_bad_values(tmp_path):
    from jailbee.inbox import cli

    settings = _settings(tmp_path)
    for value in ("0", "-5", "abc"):
        with pytest.raises(InboxUsage):
            cli.cmd_wait(settings, ["--digest", value])
    with pytest.raises(InboxUsage, match="--digest needs a value"):
        cli.cmd_wait(settings, ["--digest"])


def test_cmd_wait_digest_stale_snapshot_names_digest(tmp_path):
    from jailbee.inbox import cli

    settings = _settings(tmp_path)
    snapshot(tmp_path, [kid()], updated_at=1.0)
    with pytest.raises(InboxUsage, match="--digest"):
        cli.cmd_wait(settings, ["--digest", "1m", "--timeout", "0"])


def test_cmd_wait_digest_first_call_not_due_then_due_prints_table_and_restamps(tmp_path, capsys):
    import time

    from jailbee.inbox import cli

    settings = _settings(tmp_path)
    fresh_snapshot(tmp_path, [kid("alpha")])
    assert cli.cmd_wait(settings, ["--digest", "1m", "--timeout", "0"]) == 1
    assert capsys.readouterr().out == ""
    stamp = tmp_path / "digest-at"
    assert stamp.is_file()
    # Age the stamp: the period has elapsed.
    stamp.write_text(str(time.time() - 120))
    assert cli.cmd_wait(settings, ["--digest", "1m", "--timeout", "0"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "digest"
    assert "alpha" in "\n".join(lines[1:])
    assert time.time() - float(stamp.read_text()) < 5
    assert not list(tmp_path.glob("digest-at.*")), "temp file left behind"
    # Repeated wait inside the same period: nothing.
    assert cli.cmd_wait(settings, ["--digest", "1m", "--timeout", "0"]) == 1
    assert capsys.readouterr().out == ""


def test_cmd_wait_digest_ignores_wake_on(tmp_path, capsys):
    import time

    from jailbee.inbox import cli

    settings = _settings(tmp_path)
    fresh_snapshot(tmp_path, [kid("alpha")])
    (tmp_path / "digest-at").write_text(str(time.time() - 120))
    assert cli.cmd_wait(settings, ["--digest", "1m", "--wake-on", "failed", "--timeout", "0"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "digest"


def test_cmd_wait_digest_message_wins_and_digest_follows(tmp_path, capsys):
    import time

    from jailbee.inbox import cli

    settings = _settings(tmp_path)
    fresh_snapshot(tmp_path, [kid("alpha")])
    stamp = tmp_path / "digest-at"
    old = time.time() - 120
    stamp.write_text(str(old))
    message = put(tmp_path / "inbox/a.json", {"type": "message", "from": "alpha", "kind": "note"})
    assert cli.cmd_wait(settings, ["--digest", "1m", "--timeout", "0"]) == 0
    assert capsys.readouterr().out.strip() == str(message)
    assert float(stamp.read_text()) == old  # a message wakeup does not consume the digest
    message.unlink()
    assert cli.cmd_wait(settings, ["--digest", "1m", "--timeout", "0"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "digest"
