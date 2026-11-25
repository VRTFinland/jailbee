"""The public boot-decision writer uses volatile container storage."""

import pytest

from jailbee.boot_state import MARKER, record
from jailbee.incus import Incus, IncusError


@pytest.mark.parametrize("decision", ["started", "handled", "skipped"])
def test_record_writes_decision_under_run(mocker, decision):
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = []

    record(incus, "repo-work", decision)

    assert MARKER.startswith("/run/")
    incus.exec.assert_called_once()
    script = incus.exec.call_args.args[1][2]
    assert f"printf '%s' {decision}" in script
    assert '> "$temporary"' in script
    assert f'mv -f "$temporary" {MARKER}' in script
    assert script.index('chmod 644 "$temporary"') < script.index("mv -f")
    assert 'rm -f "$temporary"' in script
    if decision == "handled":
        assert f"$(cat {MARKER} 2>/dev/null)" in script
        assert "!= started" in script
        assert script.index("!= started") < script.index("printf")


@pytest.mark.parametrize("decision", ["handled", "skipped"])
def test_completed_boot_hides_resume_until_the_next_incus_start(
    mocker, make_cfg, tmp_path, decision
):
    from jailbee.dashboard.model import RepoGroup
    from jailbee.host_resume import resumable_repos
    from jailbee.lifecycle import list_containers

    cfg = make_cfg(tmp_path, container_prefix="myrepo")
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [
        {
            "name": "myrepo-work",
            "status": "Running",
            "profiles": ["myrepo-base"],
            "last_used_at": "2099-11-25T09:00:00.123456789Z",
        }
    ]
    incus.exec.return_value = "recorded"

    def candidates():
        return resumable_repos(
            [RepoGroup("myrepo", str(tmp_path), None, list_containers(cfg, incus))]
        )

    assert candidates() == ["myrepo"]
    assert record(incus, "myrepo-work", decision)
    assert candidates() == []
    # Incus starts the same instance again after an unplanned host reboot.
    incus.list_containers.return_value[0]["last_used_at"] = "2099-11-26T09:00:00.123456789Z"
    assert candidates() == ["myrepo"]


@pytest.mark.parametrize("failure", [IncusError("container stopped"), OSError("exec failed")])
def test_record_warns_once_without_failing_boot(mocker, capsys, failure):
    incus = mocker.Mock(spec=Incus)
    incus.exec.side_effect = failure
    incus.list_containers.return_value = []

    assert record(incus, "repo-work", "handled") is False

    output = capsys.readouterr().out
    assert output.count("cannot record autostart boot state") == 1
    assert str(failure) in output
