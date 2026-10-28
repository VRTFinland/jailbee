import io
import sys

import pytest
from rich.console import Console

from jailbee.publish_progress import PROGRESS_ENV, parse_progress, progress_line, reporting


def test_progress_line_and_parse_round_trip():
    assert parse_progress(progress_line(3, 12)) == (3, 12)
    assert parse_progress("jailbee-progress: 3/12\n") == (3, 12)


@pytest.mark.parametrize("line", ["error: jailbee-progress: 1/2", "jailbee-progress: x/2", "jailbee-progress: 1", "", "jailbee-progress: 1/2 extra"])
def test_parse_rejects_non_progress(line):
    assert parse_progress(line) is None


def test_reporting_lines_mode_writes_each_tick(capsys):
    with reporting(3, env={PROGRESS_ENV: "1"}) as tick:
        for _ in range(3):
            tick()
    assert capsys.readouterr().err == "jailbee-progress: 1/3\njailbee-progress: 2/3\njailbee-progress: 3/3\n"


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
