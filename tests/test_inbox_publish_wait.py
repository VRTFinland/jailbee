"""Publish completion waits do not consume unrelated inbox messages."""

import json

import pytest

from jailbee.inbox import socket_cli
from jailbee.inbox.errors import InboxBadUsage
from jailbee.inbox.send import SendRequest, validate
from jailbee.orchestration import broker_replies, executor, wire
from jailbee.orchestration.actions import Outcome
from tests.test_orchestration_broker import Rig
from tests.test_orchestration_executor import Env


def test_publish_wait_done_returns_one_json_object(monkeypatch, capsys):
    request = SendRequest(type="publish", issue="a.json", wait=True, until="done", json_output=True)
    monkeypatch.setattr(socket_cli, "prepare_send", lambda r: ("pub", {}, None))
    monkeypatch.setattr(socket_cli, "call_send", lambda *a, **kw: {"state": "started"})
    final = {"id": "final", "ref": "pub", "state": "done", "results": [
        {"index": 0, "kind": "create", "issue": 42, "url": "https://github.com/a/b/issues/42"}
    ]}
    def next_status(factory, args, *, timeout):
        assert args == {"ref": "pub"}
        assert 0 < timeout <= 1800
        return {"kind": "status", "item": final}
    monkeypatch.setattr(socket_cli, "call_next", next_status)
    calls = []
    monkeypatch.setattr(socket_cli, "_call", lambda *args: calls.append(args))
    assert socket_cli.cmd_send(request) == 0
    assert json.loads(capsys.readouterr().out) == final
    assert calls == [("ack", {"id": "final"})]


@pytest.mark.parametrize("state,code", [("done", 0), ("failed", 1), ("refused", 1)])
def test_immediate_final_publish_status(monkeypatch, capsys, state, code):
    monkeypatch.setattr(socket_cli, "prepare_send", lambda r: ("pub", {}, None))
    monkeypatch.setattr(socket_cli, "call_send", lambda *a, **kw: {"state": state})
    monkeypatch.setattr(socket_cli, "call_next", lambda *a, **kw: pytest.fail("already final"))
    assert socket_cli.cmd_send(SendRequest(type="publish", issue="a.json", wait=True, until="done", json_output=True)) == code
    assert json.loads(capsys.readouterr().out)["state"] == state


def test_publish_completion_timeout_does_not_ack(monkeypatch, capsys):
    monkeypatch.setattr(socket_cli, "prepare_send", lambda r: ("pub", {}, None))
    monkeypatch.setattr(socket_cli, "call_send", lambda *a, **kw: {"state": "started"})
    monkeypatch.setattr(socket_cli, "call_next", lambda *a, **kw: {"kind": "timeout"})
    monkeypatch.setattr(socket_cli, "_call", lambda *a: pytest.fail("must not ack on timeout"))
    assert socket_cli.cmd_send(SendRequest(type="publish", issue="a.json", wait=True, until="done")) == 3
    assert "pub" in capsys.readouterr().err


@pytest.mark.parametrize("kwargs", [{"until": "done"}, {"wait": True, "until": "started"}])
def test_until_requires_publish_wait_done(kwargs):
    with pytest.raises(InboxBadUsage):
        validate(SendRequest(type="publish", issue="a.json", **kwargs))


def test_ref_wait_coexists_with_ordinary_wait_and_reads_history(tmp_path, monkeypatch):
    env = Env(Rig(tmp_path, monkeypatch))
    env.ask(env.orch, "next", conn=1)
    env.ask(env.orch, "next", conn=2, ref="pub", timeout=30)
    executor.evaluate_waiters(env.memory)
    assert env.listener.responses == []
    # Started and unrelated final replies cannot finish the request-specific wait.
    for ident, ref, state in [("start", "pub", "started"), ("other", "other", "done")]:
        env.store.insert_item(env.orch, "proj-orch", {"id": ident, "type": "status", "ref": ref, "state": state}, now=1)
    executor.evaluate_waiters(env.memory)
    assert not any(conn == 2 for conn, _ in env.listener.responses)
    final = {"id": "final", "type": "status", "ref": "pub", "state": "done", "results": [{"issue": 42}]}
    env.store.insert_item(env.orch, "proj-orch", final, now=2, acked=True)
    executor.evaluate_waiters(env.memory)
    assert env.listener.responses[-1][0] == 2
    assert env.last().result == {"kind": "status", "item": final}
    assert [i.id for i in env.store.unacked(env.orch)] == ["start", "other"]


def test_ref_wait_timeout_and_disconnect(tmp_path, monkeypatch):
    env = Env(Rig(tmp_path, monkeypatch))
    env.ask(env.orch, "next", conn=2, ref="pub", timeout=0)
    executor.evaluate_waiters(env.memory)
    assert env.last().result == {"kind": "timeout"}
    env.ask(env.orch, "next", conn=3, ref="pub")
    env.memory.waiters.drop(env.orch, 3)
    assert len(env.memory.waiters) == 0


def test_publish_completion_unlimited_wait(monkeypatch, capsys):
    monkeypatch.setattr(socket_cli, "prepare_send", lambda r: ("pub", {}, None))
    def send(*args, **kwargs):
        assert kwargs["timeout"] is None
        return {"state": "started"}
    def wait(*args, **kwargs):
        assert kwargs["timeout"] is None
        return {"kind": "status", "item": {"id": "final", "state": "failed"}}
    monkeypatch.setattr(socket_cli, "call_send", send)
    monkeypatch.setattr(socket_cli, "call_next", wait)
    monkeypatch.setattr(socket_cli, "_call", lambda *a: None)
    assert socket_cli.cmd_send(SendRequest(type="publish", issue="a.json", wait=True, until="done", timeout="none", json_output=True)) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "failed"


def test_old_broker_completion_reply_gives_upgrade_advice(monkeypatch):
    from jailbee.inbox.transport import BrokerUnreachable
    monkeypatch.setattr(socket_cli, "prepare_send", lambda r: ("pub", {}, None))
    monkeypatch.setattr(socket_cli, "call_send", lambda *a, **kw: {"state": "started"})
    monkeypatch.setattr(socket_cli, "call_next", lambda *a, **kw: {"kind": "message", "id": "unrelated"})
    with pytest.raises(BrokerUnreachable, match="upgrade/restart"):
        socket_cli.cmd_send(SendRequest(type="publish", issue="a.json", wait=True, until="done"))


def test_publish_does_not_ack_when_output_fails(monkeypatch):
    monkeypatch.setattr(socket_cli, "prepare_send", lambda r: ("pub", {}, None))
    monkeypatch.setattr(socket_cli, "call_send", lambda *a, **kw: {"state": "started"})
    monkeypatch.setattr(socket_cli, "call_next", lambda *a, **kw: {"kind": "status", "item": {"id": "final", "state": "done"}})
    monkeypatch.setattr(socket_cli, "_call", lambda *a: pytest.fail("must print before ack"))
    real_print = print
    def fail(*args, **kwargs):
        if "state" in str(args):
            raise BrokenPipeError
        return real_print(*args, **kwargs)
    monkeypatch.setattr("builtins.print", fail)
    with pytest.raises(BrokenPipeError):
        socket_cli.cmd_send(SendRequest(type="publish", issue="a.json", wait=True, until="done", json_output=True))


def test_structured_results_survive_status_serialization():
    results = [{"index": 0, "kind": "create", "issue": 42, "url": "https://example.test/42"}]
    status = broker_replies.status("pub", Outcome("done", "published", results))
    assert json.loads(status.model_dump_json())["results"] == results


@pytest.mark.parametrize("args", [{"ref": ""}, {"ref": 1}, {"ref": "pub", "wake_on": []}])
def test_invalid_ref_wait_is_rejected(tmp_path, monkeypatch, args):
    env = Env(Rig(tmp_path, monkeypatch))
    env.ask(env.orch, "next", **args)
    assert env.last().error_code == wire.BAD_REQUEST
