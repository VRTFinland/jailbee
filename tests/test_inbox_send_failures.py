"""Validation and body handling for inbox send requests."""

import io

import pytest
from typer._click.exceptions import ClickException

from jailbee.inbox import send
from jailbee.inbox.container_cmd import inbox_app
from jailbee.inbox.errors import InboxBadUsage, InboxUsage


def invoke(args):
    try:
        return inbox_app(args, prog_name="jailbee inbox", standalone_mode=False) or 0
    except ClickException as exc:
        exc.show()
        return exc.exit_code


def test_help_documents_model_suffix(capsys):
    assert invoke(["runners", "--help"]) == 0
    assert "[1m] suffix" in capsys.readouterr().out
    assert invoke(["send", "spawn", "--help"]) == 0
    assert "[1m] suffix" in capsys.readouterr().out


@pytest.mark.parametrize("model", ["opus[1m]", "sonnet[1m]"])
def test_valid_model_suffix(model):
    request = send.SendRequest(type="spawn", branch="feature", new=True, agent="claude", model=model, text="hello")
    assert send.validate(request).model == model


@pytest.mark.parametrize("model", ["OPUS[1m]", "opus[2m]", "opus[1M]", "opus [1m]"])
def test_invalid_model_suffix(model):
    request = send.SendRequest(type="spawn", branch="feature", new=True, agent="claude", model=model, text="hello")
    with pytest.raises(InboxBadUsage):
        send.validate(request)


class BoundedReader(io.BytesIO):
    def read(self, size=-1):
        assert 0 < size <= 65536
        return super().read(size)


def test_oversized_body_is_read_in_bounded_chunks(monkeypatch):
    reader = BoundedReader(b"x" * 65537)
    monkeypatch.setattr("sys.stdin", type("Input", (), {"buffer": reader})())
    with pytest.raises(InboxUsage, match="limit is 65536"):
        send.read_body(send.SendRequest(type="message", stdin=True))
