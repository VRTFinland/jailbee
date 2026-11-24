"""The public boot-decision writer uses volatile container storage."""

import pytest

from jailbee.boot_state import MARKER, record
from jailbee.incus import Incus, IncusError


@pytest.mark.parametrize("decision", ["handled", "skipped"])
def test_record_writes_decision_under_run(mocker, decision):
    incus = mocker.Mock(spec=Incus)

    record(incus, "repo-work", decision)

    assert MARKER.startswith("/run/")
    incus.exec.assert_called_once_with(
        "repo-work",
        ["bash", "-c", f"printf '%s' {decision} > {MARKER} && chmod 644 {MARKER}"],
    )


def test_record_does_not_hide_failed_persistence(mocker):
    incus = mocker.Mock(spec=Incus)
    incus.exec.side_effect = IncusError("container stopped")

    with pytest.raises(IncusError, match="container stopped"):
        record(incus, "repo-work", "handled")
