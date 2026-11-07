"""The in-container outbox commands' shared role guard and error type."""

import sys

import pytest


@pytest.fixture(autouse=True)
def isolated_role(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JAILBEE_ROLE", raising=False)


def test_common_import_does_not_load_host_modules():
    # An isolated interpreter prevents earlier host tests from hiding an import leak.
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import jailbee.container_outbox.common; "
            "assert 'jailbee.incus' not in sys.modules; "
            "assert 'jailbee.git' not in sys.modules; assert 'jailbee.tui' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("marker", [False, True])
def test_a_child_is_refused(tmp_path, marker):
    from jailbee.container_outbox.common import OutboxCommandError, require_role

    if marker:
        role = tmp_path / ".jailbee/orchestration/role"
        role.parent.mkdir(parents=True)
        role.write_text("child\n")
    with pytest.raises(OutboxCommandError, match="child container") as exc:
        require_role({} if marker else {"JAILBEE_ROLE": "child"}, home=tmp_path)
    assert exc.value.exit_code == 1
    assert "jailbee inbox send message --text" in str(exc.value)
    # No outbox directory is ever created in the container.
    assert not (tmp_path / ".jailbee/pr-outbox").exists()


def test_a_non_child_passes(tmp_path):
    from jailbee.container_outbox.common import require_role

    require_role({}, home=tmp_path)


def test_an_unreadable_role_marker_is_an_error(tmp_path):
    from jailbee.container_outbox.common import OutboxCommandError, require_role

    (tmp_path / ".jailbee/orchestration/role").mkdir(parents=True)  # a directory: unreadable
    with pytest.raises(OutboxCommandError, match="cannot read container role"):
        require_role({}, home=tmp_path)
