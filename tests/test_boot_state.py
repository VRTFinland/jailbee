"""The public boot-decision writer uses volatile container storage."""

import pytest

from jailbee.boot_state import MARKER, record
from jailbee.incus import Incus, IncusError


@pytest.mark.parametrize("decision", ["started", "handled", "skipped"])
def test_record_writes_decision_under_run(mocker, decision):
    incus = mocker.Mock(spec=Incus)

    record(incus, "repo-work", decision)

    assert MARKER.startswith("/run/")
    incus.exec.assert_called_once()
    script = incus.exec.call_args.args[1][2]
    assert f"printf '%s' {decision}" in script
    assert '> "$temporary"' in script
    assert f'mv -f "$temporary" {MARKER}' in script
    assert script.index('chmod 644 "$temporary"') < script.index('mv -f')
    assert 'rm -f "$temporary"' in script
    if decision == "handled":
        assert f'$(cat {MARKER} 2>/dev/null)' in script
        assert '!= started' in script
        assert script.index('!= started') < script.index("printf")


@pytest.mark.parametrize("failure", [IncusError("container stopped"), OSError("exec failed")])
def test_record_warns_once_without_failing_boot(mocker, capsys, failure):
    incus = mocker.Mock(spec=Incus)
    incus.exec.side_effect = failure

    assert record(incus, "repo-work", "handled") is False

    output = capsys.readouterr().out
    assert output.count("cannot record autostart boot state") == 1
    assert str(failure) in output
