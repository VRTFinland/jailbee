"""The dashboard state service: one gatherer shared by every dashboard.

Every `jb dashboard`/`jb gui` used to poll incus on its own, so incusd's load
grew with the number of open dashboards. The service gathers once per host
user and pushes each snapshot to every connected dashboard over a unix
socket; the dashboards only render. It is spawned on demand by the first
dashboard (`client.StateClient`) and exits when none has been connected for a
while (`server.IDLE_TIMEOUT_SECONDS`).
"""

from __future__ import annotations


class StateServiceError(RuntimeError):
    """The state service could not be reached or used."""


class StateServiceUnavailable(StateServiceError):  # noqa: N818
    """No snapshot arrived from the state service in time."""


class StaleDashboard(StateServiceError):  # noqa: N818
    """This dashboard is older than the running state service: restart it."""
