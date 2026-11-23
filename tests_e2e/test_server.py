"""The harness must not silently leave a failed server alive."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from tests_e2e.server import E2EServer


def test_stop_reports_a_thread_that_survives_forced_exit() -> None:
    thread = Mock()
    thread.is_alive.return_value = True
    uvicorn_server = Mock()
    sock = Mock()
    server = E2EServer(Mock(), Path("unused"), _server=uvicorn_server, _thread=thread, _sock=sock)

    with pytest.raises(RuntimeError, match="server thread is still alive"):
        server.stop()

    assert uvicorn_server.should_exit is True
    assert uvicorn_server.force_exit is True
    assert thread.join.call_count == 2
    sock.close.assert_called_once()
    assert server.running  # Retain the handle so cleanup can be retried.
