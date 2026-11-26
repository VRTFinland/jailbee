"""Availability and delivery use the same terminal safety checks."""

from unittest.mock import Mock

from jailbee.agent_input import availability


def test_stopped_container_has_no_availability():
    incus = Mock()
    incus.list_containers.return_value = [{"name": "box", "status": "Stopped"}]
    assert availability(Mock(), incus, "box", "id").code == "container_not_running"
    incus.exec.assert_not_called()


def test_incus_inventory_failure_returns_outcome():
    from jailbee.agent_input import send
    from jailbee.incus import IncusError

    incus = Mock()
    incus.list_containers.side_effect = IncusError("unreachable")
    assert availability(Mock(), incus, "box", "id").code == "delivery_error"
    assert send(Mock(), incus, "box", "id", "text").code == "delivery_error"
    incus.exec_with_input.assert_not_called()
