import io
import os
import select
import sys

import pytest
from rich.console import Console

from jailbee.publish_progress import PROGRESS_ENV, parse_progress, progress_line, reporting


def test_progress_line_and_parse_round_trip():
    assert parse_progress(progress_line(3, 12)) == (3, 12)
    assert parse_progress("jailbee-progress: 3/12\n") == (3, 12)


@pytest.mark.parametrize(
    "line",
    [
        "error: jailbee-progress: 1/2",
        "jailbee-progress: x/2",
        "jailbee-progress: 1",
        "",
        "jailbee-progress: 1/2 extra",
    ],
)
def test_parse_rejects_non_progress(line):
    assert parse_progress(line) is None


def test_reporting_lines_mode_writes_each_tick(capsys):
    with reporting(3, env={PROGRESS_ENV: "1"}) as tick:
        for _ in range(3):
            tick()
    assert (
        capsys.readouterr().err
        == "jailbee-progress: 1/3\njailbee-progress: 2/3\njailbee-progress: 3/3\n"
    )


def test_reporting_silent_when_not_a_terminal(capsys):
    output = io.StringIO()
    with reporting(1, env={}, console=Console(file=output, force_terminal=False)) as tick:
        tick()
    assert output.getvalue() == ""
    assert capsys.readouterr().err == ""


def test_reporting_tty_shows_and_clears_status():
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=80)
    with reporting(2, env={}, console=console) as tick:
        tick()
        tick()
        console.print("receipt")
    assert "receipt" in output.getvalue()
    assert "Publishing 2/2" in output.getvalue()
    assert not console._live_stack


def test_reporting_preserves_other_console_and_plain_receipts(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr("sys.stderr", output)
    console = Console(file=output, force_terminal=True, width=80)
    other = Console(stderr=True, force_terminal=True, width=80)
    with reporting(2, env={}, console=console) as tick:
        tick()
        other.print("receipt one")
        tick()
        print("receipt two", file=sys.stderr)
    text = output.getvalue()
    assert "receipt one" in text
    assert "receipt two" in text
    assert "Publishing 1/2…receipt one" not in text
    assert not console._live_stack


def test_reporting_keeps_same_terminal_receipts_clear(monkeypatch):
    master, slave = os.openpty()
    try:
        with os.fdopen(os.dup(slave), "w") as stdout, os.fdopen(os.dup(slave), "w") as stderr:
            monkeypatch.setattr(sys, "stdout", stdout)
            monkeypatch.setattr(sys, "stderr", stderr)
            console = Console(file=stderr, force_terminal=True, width=80)
            other = Console(force_terminal=True, width=80)
            with reporting(1, env={}, console=console) as tick:
                tick()
                other.print("console receipt")
                print("plain receipt")
            output = b""
            while select.select([master], [], [], 0.05)[0]:
                output += os.read(master, 65536)
            assert b"console receipt" in output
            assert b"plain receipt" in output
            assert b"Publishing 1/1\xe2\x80\xa6console receipt" not in output
    finally:
        os.close(master)
        os.close(slave)


def test_reporting_keeps_redirected_stdout_on_error(monkeypatch):
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    console = Console(file=stderr, force_terminal=True, width=80)
    other = Console(force_terminal=True, width=80)
    with pytest.raises(RuntimeError), reporting(1, env={}, console=console) as tick:
        tick()
        other.print("console receipt")
        print("plain receipt")
        raise RuntimeError("failed")
    assert sys.stdout is stdout
    assert sys.stderr is stderr
    assert "console receipt" in stdout.getvalue()
    assert "plain receipt" in stdout.getvalue()
    assert "receipt" not in stderr.getvalue()


def test_reporting_stops_status_on_exception():
    console = Console(file=io.StringIO(), force_terminal=True)
    with pytest.raises(RuntimeError), reporting(1, env={}, console=console) as tick:
        tick()
        raise RuntimeError("failed")
    assert not console._live_stack


def test_reporting_total_zero_is_silent(capsys):
    output = io.StringIO()
    console = Console(file=output, force_terminal=True)
    with reporting(0, env={PROGRESS_ENV: "1"}, console=console) as tick:
        tick()
    assert output.getvalue() == ""
    assert capsys.readouterr().err == ""
