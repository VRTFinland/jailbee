"""Availability and delivery use the same terminal safety checks."""

from unittest.mock import Mock

from jailbee.agent_input import availability


def test_stopped_container_has_no_availability():
    incus = Mock()
    incus.list_containers.return_value = [{"name": "box", "status": "Stopped"}]
    assert availability(Mock(), incus, "box", "id").code == "container_not_running"
    incus.exec.assert_not_called()
