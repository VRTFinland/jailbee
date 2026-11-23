"""The harness must not silently leave a failed server alive."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from tests_e2e.server import E2EServer

pytest_plugins = ["pytester"]
REPO_ROOT = Path(__file__).resolve().parent.parent


def test_all_servers_are_cleaned_up_when_disposal_fails(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makeconftest("""
        pytest_plugins = ["tests_e2e.conftest"]
        import pytest
        from sqlalchemy.engine import Engine
        from tests_e2e.server import E2EServer

        @pytest.fixture(autouse=True)
        def cleanup_probe(monkeypatch):
            calls = []
            def dispose(engine):
                calls.append("dispose")
                if len(calls) == 1:
                    raise RuntimeError("disposal broke")
                print("second engine disposed")
            monkeypatch.setattr(Engine, "dispose", dispose)
            monkeypatch.setattr(E2EServer, "start", lambda self, **kwargs: None)
            monkeypatch.setattr(E2EServer, "stop", lambda self: print("server stopped"))
    """)
    pytester.makepyfile("""
        def test_servers(make_server, cleanup_probe):
            make_server()
            make_server()
    """)
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-s")

    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*second engine disposed*", "*disposal broke*"])
    assert str(result.stdout).count("server stopped") == 2


def test_front_page_restart_preserves_a_stop_failure(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makeconftest("""
        pytest_plugins = ["tests_e2e.conftest"]
        import pytest

        @pytest.fixture(autouse=True)
        def stop_failure(logged_in, server, monkeypatch):
            def fail_stop():
                raise RuntimeError("tracer stop broke")
            def fail_start():
                pytest.fail("tracer restarted after failed stop")
            monkeypatch.setattr(server, "stop", fail_stop)
            monkeypatch.setattr(server, "start", fail_start)
    """)
    pytester.makepyfile("""
        from tests_e2e.test_front_page_live import (
            test_connection_loss_shows_connecting_while_the_last_snapshot_stays_visible,
            test_server_restart_reconnects_to_current_state_without_reloading,
        )
    """)
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--tb=short")

    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*RuntimeError: tracer stop broke*"])
    assert "tracer restarted after failed stop" not in str(result.stdout)


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
